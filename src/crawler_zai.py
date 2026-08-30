import logging
import json
import asyncio
import re
from typing import List, Dict, Any, Tuple
from .config import load_config
from .database import db_manager
from .llm_client import LLMClient
from .github_client import github_client
from .intent_detector import clean_readme_content

logger = logging.getLogger(__name__)

# 无效摘要与占位符黑名单
PLACEHOLDER_KEYWORDS = [
    "提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多",
    "Ask anything about the Repository", "Ask anything about this", "Responsed by AI", "May contain mistakes",
    "Private Repos", "Subscription", "Zread Discover Trending",
    "尚未收录", "未找到该仓库", "正在生成中", "Repository not found", "No overview available",
    "Toggle theme", "Chat with codebase", "登录以获取更多信息", "请登录后查看",
    # Cloudflare 错误与网关超时 (504/524/502) 拦截特征
    "Cloudflare Ray ID", "Visit cloudflare.com", "gateway time-out", "gateway timeout",
    "Bad gateway", "Web server is down", "Error 524", "Error 504", "Error 502", "Error 520",
    "Performance & security by", "Checking your browser", "Just a moment...",
    "504 Gateway Time-out", "502 Bad Gateway", "524 A timeout occurred",
    "The web server reported a gateway time-out error", "暂无解析"
]

def parse_llm_json_response(content: Any) -> Dict[str, str]:
    """鲁棒解析 LLM 返回的 JSON 对象、JSON 数组或纯文本字典"""
    if not content:
        return {}
    
    if isinstance(content, dict):
        if "repos" in content and isinstance(content["repos"], list):
            content = content["repos"]
        elif "results" in content and isinstance(content["results"], dict):
            content = content["results"]
        elif "projects" in content and isinstance(content["projects"], list):
            content = content["projects"]
        elif "items" in content and isinstance(content["items"], list):
            content = content["items"]
        else:
            return {k.strip(): str(v).strip() for k, v in content.items() if v}

    if isinstance(content, list):
        res_dict = {}
        for item in content:
            if isinstance(item, dict):
                name = item.get("name") or item.get("full_name") or item.get("repo")
                summary = item.get("summary") or item.get("overview") or item.get("description")
                if name and summary:
                    res_dict[str(name).strip()] = str(summary).strip()
        return res_dict

    if not isinstance(content, str):
        return {}

    cleaned = content.strip()
    if "```json" in cleaned:
        match = re.search(r'```json\s*([\s\S]*?)\s*```', cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    elif "```" in cleaned:
        match = re.search(r'```\s*([\s\S]*?)\s*```', cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)

    try:
        data = json.loads(cleaned)
        return parse_llm_json_response(data)
    except Exception:
        pass

    # 正则容错提取 key-value 结构
    res_dict = {}
    try:
        matches = re.findall(r'"([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)"\s*:\s*"([^"]+)"', cleaned)
        for name, summary in matches:
            res_dict[name.strip()] = summary.strip()
    except Exception:
        pass

    return res_dict


class RepoAnalyzer:
    """
    基于大模型长上下文批量聚合生成 GitHub 开源项目中文深度架构与技术解读。
    默认采用长上下文大模型批量聚合（最多 20 个/批），替代易卡顿的外部爬虫。
    """
    def __init__(self):
        self.settings = load_config()
        self.llm_batch_size = 20  # 默认每批聚合最多 20 个项目
        self._llm_client = None

    @property
    def llm_client(self):
        if self._llm_client is None:
            llm_conf = self.settings.get("llm", {})
            self._llm_client = LLMClient(
                api_key=llm_conf.get("api_key"),
                base_url=llm_conf.get("base_url"),
                model_names=llm_conf.get("model_names")
            )
        return self._llm_client

    def _get_cached_summaries(self, full_names: List[str]) -> Tuple[Dict[str, str], List[str]]:
        """
        批量获取缓存中的有效摘要，并严格识别占位符或低质量摘要以触发重新生成。
        """
        if not full_names:
            return {}, []

        names_str = ",".join([f"'{n}'" for n in full_names])
        res = db_manager.execute_query(
            f"SELECT repo_full_name, summary FROM ai_summaries WHERE repo_full_name IN ({names_str})", 
            db_type="insight"
        )
        cache_map = {}
        for r in res:
            name = r['repo_full_name']
            summary = r['summary'] or ""
            # 清理历史旧数据中的 [自动托底] 前缀
            if "[自动托底]" in summary:
                summary = re.sub(r'^\s*\[自动托底\]\s*', '', summary).strip()
            cache_map[name] = summary

        valid_cache = {}
        needs_generation = []

        for name in full_names:
            if name not in cache_map:
                needs_generation.append(name)
                continue

            summary = cache_map[name]
            is_placeholder = any(kw.lower() in summary.lower() for kw in PLACEHOLDER_KEYWORDS)
            if not summary or len(summary) < 60 or is_placeholder:
                needs_generation.append(name)
            else:
                valid_cache[name] = summary

        return valid_cache, needs_generation

    def _save_summaries_to_cache(self, summary_map: Dict[str, str]):
        """批量持久化保存高质量摘要至数据库"""
        if not summary_map:
            return
        records = [(name, summary) for name, summary in summary_map.items() if summary and len(summary.strip()) >= 30]
        if not records:
            return

        logger.info(f"正在保存 {len(records)} 条高质量项目技术解读至数据库...")
        sql = """
        INSERT INTO ai_summaries (repo_full_name, summary) 
        VALUES (%s, %s) 
        ON DUPLICATE KEY UPDATE summary=VALUES(summary)
        """
        db_manager.execute_batch(sql, records, db_type="insight")

    async def fetch_repo_context(self, full_name: str) -> Tuple[str, str, str, str, list]:
        """异步获取单个项目的 Description、语言、Topics 标签及清洗后的 README 片段"""
        # 1. 查询数据库已有元数据
        repo_info = db_manager.execute_query(
            f"SELECT description, language, topics FROM repos WHERE full_name = '{full_name}'", 
            db_type="source"
        )
        desc = ""
        lang = ""
        topics = []
        if repo_info:
            desc = repo_info[0].get('description') or ""
            lang = repo_info[0].get('language') or ""
            topics_raw = repo_info[0].get('topics')
            if isinstance(topics_raw, list):
                topics = topics_raw
            elif isinstance(topics_raw, str):
                try:
                    topics = json.loads(topics_raw)
                except Exception:
                    pass

        # 2. 抓取并深度清洗 README (截取前 2000 字高信噪比纯净文本)
        raw_readme = await asyncio.to_thread(github_client.get_readme, full_name)
        cleaned_readme = clean_readme_content(raw_readme, max_chars=2000)

        # 3. 组装结构化上下文
        topic_str = "、".join(topics[:5]) if topics else "无"
        context_parts = [
            f"项目全名: {full_name}",
            f"主语言: {lang or '未知'} | 标签: {topic_str}",
            f"项目定位: {desc or '暂无描述'}",
            f"README核心内容:\n{cleaned_readme if cleaned_readme else '（该项目未提供详细 README，请根据项目定位与技术栈标签进行技术解读与架构推演）'}"
        ]
        context = "\n".join(context_parts)
        return full_name, context, desc, lang, topics

    async def get_summaries_from_llm_batch(self, batch_items: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        核心长上下文批量生成：将最多 20 个项目聚合为单次 Prompt 提交给大模型
        """
        if not batch_items:
            return {}

        chunk_names = [item['full_name'] for item in batch_items]
        prompt_items = []
        for idx, item in enumerate(batch_items, 1):
            prompt_items.append(f"【项目 {idx}/{len(batch_items)}】: {item['full_name']}\n{item['context']}")

        system_prompt = (
            "你是一位资深的开源软件架构师与资深技术专家。请阅读用户提供的批量 GitHub 开源项目资料，为其中每个项目分别用 180~250 字提炼其中文深度技术解读。\n"
            "解读需涵盖：1. 核心定位与解决的关键痛点；2. 架构设计与工程技术亮点；3. 典型应用场景与生态价值。\n\n"
            "【输出格式要求】:\n"
            "请务必输出标准的 JSON 字典格式，Key 为项目的 full_name（如 'owner/repo'），Value 为对应的中文技术解读段落。\n"
            "示例:\n"
            "{\n"
            '  "vllm-project/vllm": "vLLM 是一个高吞吐、低延迟的 LLM 推理与服务引擎。其核心亮点在于创新性的 PagedAttention 内存管理算法，有效解决了注意力机制中的显存碎片问题，推理并发性能相比传统实现提升了数倍，广泛应用于大模型企业级部署与云端推理服务场景。",\n'
            '  "astral-sh/uv": "uv 是基于 Rust 构建的高性能 Python 包管理与解析工具。它通过无锁并发设计和本地缓存优化，将依赖解析和安装速度提升了 10~100 倍，完全兼容 pip 和 virtualenv 命令体系，极大加速了 CI/CD 构建与本地开发流程。"\n'
            "}"
        )

        user_prompt = "\n\n====================\n\n".join(prompt_items)

        data_raw = await self.llm_client.chat(
            system_prompt=system_prompt,
            user_prompt=f"请对以下 {len(batch_items)} 个开源项目进行深度技术解读并输出 JSON 字典：\n\n{user_prompt}",
            temperature=0.2,
            json_mode=True
        )

        parsed_dict = parse_llm_json_response(data_raw)
        results = {}

        if isinstance(parsed_dict, dict):
            for name, summary in parsed_dict.items():
                if not summary or not isinstance(summary, str):
                    continue
                # 智能名称对齐（精确、大小写、后缀匹配）
                matched_name = None
                for cn in chunk_names:
                    if name == cn or name.lower() == cn.lower():
                        matched_name = cn
                        break
                    elif name.lower() == cn.split('/')[-1].lower():
                        matched_name = cn
                        break
                    elif cn.lower().endswith(name.lower()):
                        matched_name = cn
                        break

                if matched_name and len(summary.strip()) >= 40:
                    results[matched_name] = summary.strip()

        return results

    def _build_synthetic_summary(self, full_name: str, desc: str = "", lang: str = "", topics: list = None) -> str:
        """当模型调用异常时的智能合成技术摘要"""
        topics_str = "、".join(topics[:3]) if topics else (lang or "开源开发者工具")
        desc_text = desc.strip() if desc else "开源技术方案"
        return (
            f"{full_name} 是基于 {lang or '现代技术栈'} 构建的 {topics_str} 项目。其核心定位为：{desc_text}。"
            f"项目设计注重轻量化与开箱即用体验，针对常见开发痛点提供了高效的工程实现，具备良好的集成扩展性与应用价值。"
        )

    async def analyze_batch(self, full_names: List[str]) -> Dict[str, str]:
        """
        全量批量分析入口：
        1. 查缓存并跳过已有的高质量解析
        2. 并行抓取缺失项目的 README 与元数据
        3. 聚合为最多 20 个一组的长上下文批次请求大模型
        4. 自动兜底并持久化更新缓存
        """
        if not full_names:
            return {}

        # 1. 查询有效缓存
        valid_cache, needs_generation = self._get_cached_summaries(full_names)

        if not needs_generation:
            return valid_cache

        logger.info(f"正在为 {len(needs_generation)} 个项目使用大模型长上下文批量生成技术解读（聚合批次 <= 20）...")

        # 2. 并行获取 Context 与元数据
        context_tasks = [self.fetch_repo_context(name) for name in needs_generation]
        fetched = await asyncio.gather(*context_tasks, return_exceptions=True)

        project_items = []
        repo_meta = {}
        for item in fetched:
            if isinstance(item, tuple) and len(item) == 5:
                fname, ctx, desc, lang, topics = item
                project_items.append({
                    "full_name": fname,
                    "context": ctx,
                    "desc": desc,
                    "lang": lang,
                    "topics": topics
                })
                repo_meta[fname] = (desc, lang, topics)
            elif isinstance(item, Exception):
                logger.error(f"获取项目上下文失败: {item}")

        # 3. 分批调用大模型 (每批聚合最多 20 个项目)
        new_summaries = {}
        batch_size = min(self.llm_batch_size, 20)

        for i in range(0, len(project_items), batch_size):
            chunk = project_items[i:i + batch_size]
            logger.info(f"正在提交第 {i//batch_size + 1}/{(len(project_items)-1)//batch_size + 1} 批大模型技术解读 ({len(chunk)} 个项目)...")

            try:
                batch_res = await self.get_summaries_from_llm_batch(chunk)
                new_summaries.update(batch_res)
            except Exception as e:
                logger.error(f"批处理大模型解读异常: {e}")

            # 针对当前批次中若有未成功匹配或返回过短的项目，进行智能合成兜底
            for p in chunk:
                fn = p['full_name']
                if fn not in new_summaries or len(new_summaries[fn]) < 40:
                    desc, lang, topics = repo_meta.get(fn, ("", "", []))
                    new_summaries[fn] = self._build_synthetic_summary(fn, desc, lang, topics)

        # 4. 持久化存入数据库
        if new_summaries:
            self._save_summaries_to_cache(new_summaries)

        # 5. 合并返回全量结果
        final_results = {**valid_cache, **new_summaries}
        for name in full_names:
            if name not in final_results:
                final_results[name] = "该项目暂无深度解析。"

        return final_results


repo_analyzer = RepoAnalyzer()
