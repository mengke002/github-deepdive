import asyncio
import logging
import requests
import json
import re
from crawl4ai import AsyncWebCrawler
from .config import load_config
from .database import db_manager

logger = logging.getLogger(__name__)

class RepoAnalyzer:
    def __init__(self):
        self.settings = load_config()
        self.api_key = self.settings["llm"]["api_key"]
        self.base_url = self.settings["llm"]["base_url"]
        self.models = self.settings["llm"]["model_names"]
        self.llm_batch_size = 6  # 默认每批处理 6 个项目
        
        self.crawl_semaphore = asyncio.Semaphore(2)
        self.llm_semaphore = asyncio.Semaphore(3) # 批处理占用更多 token，降低并发

    def _get_cached_summaries(self, full_names):
        """
        批量获取缓存的摘要，并识别低质量摘要以备升级。
        """
        if not full_names: return {}, []
        names_str = ",".join([f"'{n}'" for n in full_names])
        res = db_manager.execute_query(
            f"SELECT repo_full_name, summary FROM ai_summaries WHERE repo_full_name IN ({names_str})", 
            db_type="insight"
        )
        cache_map = {r['repo_full_name']: r['summary'] for r in res}
        
        # 识别需要升级的项目：
        # 1. 包含托底标记 [自动托底]
        # 2. 长度过短（<120字）
        # 3. 包含 zread 占位符特征的内容
        needs_upgrade = []
        placeholder_keywords = ["提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多"]
        
        for name, summary in cache_map.items():
            is_placeholder = any(kw in summary for kw in placeholder_keywords)
            if "[自动托底]" in summary or len(summary) < 120 or is_placeholder:
                needs_upgrade.append(name)
        
        return cache_map, needs_upgrade

    def _save_summaries_to_cache(self, summary_map):
        """批量保存摘要到缓存"""
        if not summary_map: return
        records = [(name, summary) for name, summary in summary_map.items() if summary]
        if not records: return
        # 增加日志：哪些是带 [自动托底] 的
        fallback_count = sum(1 for s in summary_map.values() if "[自动托底]" in s)
        logger.info(f"正在更新 {len(records)} 条摘要缓存 (其中 {fallback_count} 条为临时托底)...")
        
        sql = "INSERT INTO ai_summaries (repo_full_name, summary) VALUES (%s, %s) ON DUPLICATE KEY UPDATE summary=VALUES(summary)"
        db_manager.execute_batch(sql, records, db_type="insight")

    async def fetch_zread_content(self, crawler, full_name):
        async with self.crawl_semaphore:
            # 优先尝试概述页面
            urls = [f"https://zread.ai/{full_name}/1-overview", f"https://zread.ai/{full_name}"]
            for url in urls:
                try:
                    result = await crawler.arun(url=url, bypass_cache=True)
                    if result.success and result.markdown:
                        content = result.markdown
                        
                        # 核心改进：检测是否为 zread 的“未索引”占位页面
                        placeholder_keywords = ["提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多"]
                        if any(kw in content for kw in placeholder_keywords):
                            logger.info(f"检测到 zread 占位页面，跳过: {url}")
                            continue 
                        
                        # 1. 尝试精细提取“概述”或“快速入门”部分
                        pattern = r'(?:^|\n)(?:#|##)\s*(?:概述|Overview|快速入门|Quick\s*Start).*?\n(.*?)(?=\n(?:#|##)|\Z)'
                        summary_match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
                        
                        raw_extracted = ""
                        if summary_match:
                            raw_extracted = summary_match.group(1).strip()
                        else:
                            # 托底处理
                            raw_extracted = content
                            
                        # 2. 深度清洗 zread 噪音
                        cleaned = raw_extracted
                        cleaned = re.sub(r'^\s*\* \[趋势\].*?\n', '', cleaned, flags=re.MULTILINE)
                        cleaned = re.sub(r'^\s*\* \[订阅\].*?\n', '', cleaned, flags=re.MULTILINE)
                        cleaned = re.sub(r'\[反馈\]\(https://zhipu-ai\.feishu\.cn/.*?\)', '', cleaned)
                        cleaned = re.sub(r'\[\]\(https://(?:x\.com|discord\.gg)/.*?\)', '', cleaned)
                        cleaned = re.sub(r'⌘K\s*Ask AI.*?(?=上次索引|快速入门|资讯|深入解析|$)', '', cleaned, flags=re.DOTALL)
                        cleaned = re.sub(r'\* (?:Toggle theme|分享|登录|快速入门|资讯|深入解析)\s*', '', cleaned)
                        cleaned = re.sub(r'上次索引:\[.*?\]\(.*?\)', '', cleaned)
                        cleaned = re.sub(r'(?:来源|Source|参考)\s*[:：]\s*\[.*?\]\(.*?\)', '', cleaned, flags=re.IGNORECASE)
                        cleaned = re.sub(r'(?:来源|Source|参考)\s*[:：]\s*、?\s*', '', cleaned, flags=re.IGNORECASE)
                        cleaned = re.sub(r'(?:\d+|约\d+|[一二三四五六七八九十]+)?\s*分钟\s*[:：]?\s*(?:入门|阅读|学习|解读)?', '', cleaned, flags=re.IGNORECASE)
                        
                        # 3. 最终校验
                        final_text = cleaned.strip()
                        # 再次检查清洗后的文本是否还包含占位符特征
                        if len(final_text) > 150 and not any(kw in final_text for kw in placeholder_keywords):
                            return final_text[:5000]
                except Exception as e:
                    logger.warning(f"无法抓取 {url}: {e}")
                    continue
            return None

    async def get_summaries_from_llm_batch(self, repo_contents: dict):
        """
        核心批处理方法：一次性解析多个仓库
        """
        names = list(repo_contents.keys())
        prompt_content = []
        for name, content in repo_contents.items():
            truncated_content = content[:3000] if content else "No description"
            prompt_content.append(f"### REPO: {name}\nCONTENT:\n{truncated_content}\n---")

        system_prompt = (
            "你是一位资深软件架构师。请分析以下 GitHub 项目，并为每个项目产出一段深度技术摘要（中文，150-200字）。\n"
            "摘要应包含：核心价值、工程亮点、应用前景。请务必输出 JSON 数组格式，结构如下：\n"
            '[{"name": "owner/repo", "summary": "摘要内容"}, ...]'
        )

        async with self.llm_semaphore:
            for attempt in range(3):
                for model in self.models:
                    try:
                        logger.info(f"正在使用 {model} 进行批处理 LLM 分析 ({len(names)} 个项目)...")
                        payload = {
                            "model": model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": "\n".join(prompt_content)}
                            ],
                            "temperature": 0.2
                        }
                        resp = await asyncio.to_thread(
                            requests.post, f"{self.base_url}/chat/completions", 
                            json=payload, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=120
                        )
                        
                        if resp.status_code == 200:
                            raw_content = resp.json()['choices'][0]['message']['content'].strip()
                            try:
                                if "```json" in raw_content:
                                    raw_content = re.search(r'```json\s*(.*?)\s*```', raw_content, re.DOTALL).group(1)
                                
                                data = json.loads(raw_content)
                                if isinstance(data, dict) and "repos" in data: data = data["repos"]
                                if not isinstance(data, list): data = [data]
                                
                                # 重要：LLM 生成的摘要统一带上 [自动托底] 标记，确保未来有机会升级为 zread
                                results = {item['name']: f"[自动托底] {item['summary']}" for item in data if 'name' in item and 'summary' in item}
                                self._save_summaries_to_cache(results)
                                return results
                            except Exception as parse_err:
                                logger.error(f"解析 LLM JSON 响应失败: {parse_err}")
                                continue
                        else:
                            logger.warning(f"LLM {model} 请求失败: {resp.status_code}")
                    except Exception as e:
                        logger.warning(f"LLM {model} 异常: {e}")
                        continue
                if attempt < 2: await asyncio.sleep(2 ** (attempt + 1))
            return {}

    async def analyze_batch(self, full_names):
        if not full_names: return {}
        
        # 1. 查询缓存并识别需要升级的项目
        cached_results, upgrade_names = self._get_cached_summaries(full_names)
        
        # 待处理项目 = 缓存中没有的项目 + 需要升级的项目
        missing_names = [n for n in full_names if n not in cached_results]
        pending_names = list(set(missing_names + upgrade_names))
        
        if not pending_names:
            return cached_results

        logger.info(f"正在处理 {len(pending_names)} 个项目的摘要（含 {len(upgrade_names)} 个待升级项目）...")
        
        # 2. 尝试抓取 zread 内容
        zread_contents = {}
        no_zread_names = []
        
        async with AsyncWebCrawler() as crawler:
            crawl_tasks = [self.fetch_zread_content(crawler, name) for name in pending_names]
            crawled_list = await asyncio.gather(*crawl_tasks)
            
            for name, content in zip(pending_names, crawled_list):
                if content:
                    zread_contents[name] = content
                else:
                    no_zread_names.append(name)

        # 3. 对 zread 缺失的项目，构建高质量托底上下文 (README + Description)
        from .github_client import github_client
        fallback_contexts = {}
        
        if no_zread_names:
            logger.info(f"有 {len(no_zread_names)} 个项目无法从 zread 获取，正在采集 GitHub README 进行深度托底...")
            for name in no_zread_names:
                # 获取 README (前 3000 字)
                readme = await asyncio.to_thread(github_client.get_readme, name)
                # 获取 Description
                repo_info = db_manager.execute_query(f"SELECT description FROM repos WHERE full_name = '{name}'", db_type="source")
                desc = repo_info[0]['description'] if repo_info and repo_info[0]['description'] else ""
                
                # 组装 LLM 分析用的上下文
                combined_context = f"GitHub Description: {desc}\n\nREADME Snippet:\n{readme[:3500]}"
                fallback_contexts[name] = combined_context

        # 4. 汇总结果并执行 LLM 解析
        final_results = {**cached_results}
        
        # zread 抓到的直接存入并更新缓存 (高质量)
        if zread_contents:
            logger.info(f"成功获取 {len(zread_contents)} 个项目的 zread 深度解析。")
            final_results.update(zread_contents)
            self._save_summaries_to_cache(zread_contents)

        # zread 没抓到的，通过 README 调 LLM 生成 (带 [自动托底] 标记)
        if fallback_contexts:
            logger.info(f"为 {len(fallback_contexts)} 个项目启动基于 README 的 LLM 托底分析...")
            pending_list = list(fallback_contexts.keys())
            for i in range(0, len(pending_list), self.llm_batch_size):
                chunk_names = pending_list[i:i + self.llm_batch_size]
                chunk_data = {n: fallback_contexts[n] for n in chunk_names}
                batch_res = await self.get_summaries_from_llm_batch(chunk_data)
                final_results.update(batch_res)
        
        # 5. 兜底填充
        for name in full_names:
            if name not in final_results:
                final_results[name] = "该项目暂无深度解析。"
                
        return final_results

repo_analyzer = RepoAnalyzer()
