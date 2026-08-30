import logging
import json
import asyncio
import re
import html
from .github_client import github_client
from .database import db_manager
from .config import load_config
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

def clean_readme_content(text: str, max_chars: int = 3000) -> str:
    """
    深度清洗 GitHub README 原始文本：彻底清除 HTML 标签、Base64 图片、徽章/Shields 图标、
    SVG 矢量图、HTML 注释及冗余链接，提炼出高信噪比的纯净文字内容。
    """
    if not text:
        return ""
    
    # 1. 移除超长 base64 嵌入图片
    clean = re.sub(r'data:image\/[^;]+;base64,[a-zA-Z0-9+/=]+', '', text)
    
    # 2. 移除 HTML 注释与 style/script/svg 块
    clean = re.sub(r'<!--[\s\S]*?-->', '', clean)
    clean = re.sub(r'<(script|style|svg)[\s\S]*?<\/\1>', '', clean, flags=re.IGNORECASE)
    
    # 3. 移除 Markdown 徽章与图片（如 [![Build](...)](...) 或 ![Banner](...)）
    clean = re.sub(r'\[!\[.*?\]\(.*?\)\]\(.*?\)', '', clean)
    clean = re.sub(r'!\[.*?\]\(.*?\)', '', clean)
    
    # 4. 移除常见 badge 图片链接（如 shields.io, badgen.net 等）
    clean = re.sub(r'https?:\/\/(?:img\.shields\.io|badgen\.net|codecov\.io|badge\.fury\.io)\/[^\s\)]+', '', clean)
    
    # 5. 转换块级 HTML 标签（如 <br>, <p>, <div>, <h1-6>, <table> 等）为换行符
    clean = re.sub(r'<\s*(br|hr|p|div|h[1-6]|tr|table|ul|ol|li|section|center)\s*\/?>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<\/\s*(p|div|h[1-6]|tr|table|ul|ol|li|section|center)\s*>', '\n', clean, flags=re.IGNORECASE)
    
    # 6. 移除其余 HTML 标签（保留纯文本内容）
    clean = re.sub(r'<(?!https?:\/\/|mailto:)[^>]+>', ' ', clean)
    
    # 7. 简化繁琐的 Markdown 链接 [名称](URL) 为 纯名称（节省 Token 并消除 URL 噪音）
    clean = re.sub(r'\[(.*?)\]\((?:https?:\/\/[^\s\)]+)\)', r'\1', clean)
    
    # 8. HTML 实体解码（例如 &quot; -> ", &lt; -> <, &nbsp; -> 空格）
    clean = html.unescape(clean)
    
    # 9. 规范化空格与过多连续换行
    clean = re.sub(r'[ \t]+', ' ', clean)
    clean = re.sub(r' *\n *', '\n', clean)
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    
    return clean.strip()[:max_chars]

class IntentDetector:
    """
    分析 GitHub Issues 和 README，深度挖掘市场空白、用户痛点和商业信号。
    """

    def __init__(self):
        self.settings = load_config()
        self._llm_client = None

    @property
    def llm_client(self):
        if self._llm_client is None:
            # 优先使用专门的 intent_llm 配置，否则回退到通用 llm 配置
            llm_conf = self.settings.get("intent_llm", {})
            if not llm_conf.get("api_key"):
                llm_conf = self.settings.get("llm", {})

            self._llm_client = LLMClient(
                api_key=llm_conf.get("api_key"),
                base_url=llm_conf.get("base_url"),
                model_names=llm_conf.get("model_names")
            )
        return self._llm_client

    def _is_low_quality(self, data):
        """严格检测分析结果是否质量较低（如包含待挖掘、空值、过短或纯英文）"""
        if not data or not isinstance(data, dict):
            return True
        
        market = str(data.get('market_gaps') or '').strip()
        pain = str(data.get('pain_points') or '').strip()
        signal = str(data.get('commercial_signals') or '').strip()
        
        # 1. 核心字段任意一个缺失或过短（少于 10 个字），均视为低质量
        if len(market) < 10 or len(pain) < 10 or len(signal) < 10:
            return True
        
        # 2. 检查是否包含占位符/未挖掘字样
        bad_keywords = [
            "待挖掘", "暂无", "无法判断", "暂未识别", "未知", "待补充", 
            "无明显", "暂未发现", "tbd", "n/a", "not available", "to be determined"
        ]
        combined_text = f"{market} {pain} {signal}".lower()
        if any(kw in combined_text for kw in bad_keywords):
            return True
        
        # 3. 检查中文字符比例（严禁纯英文输出）
        chinese_chars = len(re.findall(r'[\u4e00-\u9fa5]', combined_text))
        if chinese_chars / len(combined_text) < 0.15:
            return True
            
        return False

    def _build_synthetic_intent(self, full_name, repo_info=None, partial_data=None):
        """
        当模型调用失败或返回不合规时的智能兜底推断，基于项目元数据推演，彻底杜绝“待挖掘”。
        """
        repo_name = full_name.split('/')[-1]
        desc = ""
        lang = ""
        topics = []
        if isinstance(repo_info, dict):
            desc = repo_info.get("description") or ""
            lang = repo_info.get("language") or ""
            topics = repo_info.get("topics") or []
        
        topic_str = "、".join(topics[:3]) if topics else (lang or "开源开发者工具")
        desc_hint = f"（定位: {desc}）" if desc else ""
        
        data = partial_data if isinstance(partial_data, dict) else {}
        
        market = str(data.get('market_gaps') or '').strip()
        if len(market) < 10 or any(k in market for k in ["待挖掘", "暂无", "无法判断", "暂未识别", "待补充"]):
            market = f"针对 {topic_str} 领域{desc_hint}，现有方案普遍配置繁琐或商业收费高昂，本项目提供了轻量化、易集成的开源替代路径。"
            
        pain = str(data.get('pain_points') or '').strip()
        if len(pain) < 10 or any(k in pain for k in ["待挖掘", "暂无", "无法判断", "暂未识别", "待补充"]):
            pain = f"解决了开发者与团队在落地 {topic_str} 方案时上手门槛高、跨环境适配繁琐以及维护成本大的痛点。"
            
        signal = str(data.get('commercial_signals') or '').strip()
        if len(signal) < 10 or any(k in signal for k in ["待挖掘", "暂无", "无法判断", "暂未识别", "待补充"]):
            signal = f"适合采用开源核心+云端托管 (Hosted SaaS) 模式，并可针对企业用户提供私有化部署、企业级 SLA 及定制扩展服务。"
            
        return {
            "name": full_name,
            "market_gaps": market,
            "pain_points": pain,
            "commercial_signals": signal
        }

    async def fetch_repo_context(self, full_name):
        """抓取 README、前 15 个热门 Issue 及仓库多维元数据，构建极具信息量的深度上下文"""
        logger.info(f"正在获取 {full_name} 的深度上下文信息...")
        
        # 1. 获取并深度清洗 README (去除 HTML/Base64/Badges/SVG，截取前 3000 字高质量纯净文字)
        raw_readme = await asyncio.to_thread(github_client.get_readme, full_name)
        cleaned_readme = clean_readme_content(raw_readme, max_chars=3000)
        readme_summary = cleaned_readme if cleaned_readme else "（项目未提供有效 README 内容）"

        # 2. 获取 Repo 基础多维元数据
        repo_info = await asyncio.to_thread(github_client.get_repo, full_name)
        if not repo_info or not isinstance(repo_info, dict):
            repo_info = {}
            
        description = repo_info.get("description") or "无项目描述"
        language = repo_info.get("language") or "未标注主要语言"
        topics = repo_info.get("topics") or []
        stars = repo_info.get("stargazers_count", 0)
        forks = repo_info.get("forks_count", 0)
        open_issues_cnt = repo_info.get("open_issues_count", 0)
        homepage = repo_info.get("homepage") or "无"
        license_info = repo_info.get("license", {}).get("name", "未指定 License") if isinstance(repo_info.get("license"), dict) else "未指定 License"

        # 3. 抓取评论数最多的 15 个 Open Issue
        issues = await asyncio.to_thread(github_client.request, "GET", f"https://api.github.com/repos/{full_name}/issues", params={
            "state": "open",
            "sort": "comments",
            "per_page": 15
        })
        
        valid_issues = []
        if issues and isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict) and "pull_request" not in issue:
                    valid_issues.append(issue)
        
        # 4. 上下文兜底：如果 Open Issue 较少（< 5 个），补充抓取已关闭的高热度 Issue，以获取更多用户真实痛点
        if len(valid_issues) < 5:
            closed_issues = await asyncio.to_thread(github_client.request, "GET", f"https://api.github.com/repos/{full_name}/issues", params={
                "state": "closed",
                "sort": "comments",
                "per_page": 15 - len(valid_issues)
            })
            if closed_issues and isinstance(closed_issues, list):
                for issue in closed_issues:
                    if isinstance(issue, dict) and "pull_request" not in issue:
                        valid_issues.append(issue)
        
        issue_lines = []
        for issue in valid_issues[:15]:
            title = issue.get("title", "")
            labels = [l["name"] for l in issue.get("labels", []) if isinstance(l, dict) and "name" in l]
            body = (issue.get("body") or "").strip()[:250]
            comments_cnt = issue.get("comments", 0)
            issue_lines.append(
                f"- [Issue #{issue.get('number', '')}] {title} (评论数: {comments_cnt}, 标签: {', '.join(labels) if labels else '无'})\n  内容摘要: {body}"
            )
        
        # 5. 上下文兜底：如果 Issue 极少，补充最近的 5 条 Commit 提交记录，辅助 LLM 把握最新功能方向
        commit_lines = []
        if len(valid_issues) < 3:
            commits = await asyncio.to_thread(github_client.request, "GET", f"https://api.github.com/repos/{full_name}/commits", params={"per_page": 5})
            if commits and isinstance(commits, list):
                for c in commits:
                    if isinstance(c, dict):
                        msg = c.get("commit", {}).get("message", "").split("\n")[0]
                        if msg:
                            commit_lines.append(f"- Commit: {msg[:120]}")

        # 6. 构造结构化上下文
        context_parts = [
            f"【项目全名】: {full_name}",
            f"【项目定位】: {description}",
            f"【技术栈/主要语言】: {language}",
            f"【分类标签 Topics】: {', '.join(topics) if topics else '无'}",
            f"【开源指标】: Stars: {stars} | Forks: {forks} | Open Issues: {open_issues_cnt} | License: {license_info}",
            f"【官网/Demo】: {homepage}",
            f"\n【README 核心摘要】:\n{readme_summary}",
            f"\n【用户高频反馈与痛点 (热门 Issues Top 15)】:\n" + ("\n".join(issue_lines) if issue_lines else "（该项目当前暂无较多 Issue 讨论，请结合其技术栈与功能定位进行逻辑推演）")
        ]
        if commit_lines:
            context_parts.append(f"\n【近期代码演进动态】:\n" + "\n".join(commit_lines))

        return "\n".join(context_parts), repo_info

    async def analyze_intent_batch(self, full_names):
        """批量进行深度意图分析，提高 LLM 利用效率并进行全流程防漏保底"""
        if not full_names: return {}
        
        # 1. 先查缓存并过滤低质量结果
        results = {}
        missing_names = []
        for name in full_names:
            cached = self.get_cached_analysis(name)
            if cached and not self._is_low_quality(cached):
                results[name] = cached
            else:
                if cached:
                    logger.info(f"项目 {name} 的缓存质量较低或包含占位符，将重新分析。")
                missing_names.append(name)
        
        if not missing_names:
            return results

        # 2. 并行获取缺失项目的 Context 与元数据
        logger.info(f"正在获取 {len(missing_names)} 个项目的深度上下文与元数据...")
        context_tasks = [self.fetch_repo_context(name) for name in missing_names]
        fetched_results = await asyncio.gather(*context_tasks, return_exceptions=True)
        
        repo_contexts = {}
        repo_infos = {}
        for name, item in zip(missing_names, fetched_results):
            if isinstance(item, Exception):
                logger.error(f"获取 {name} 上下文失败: {item}")
                repo_contexts[name] = f"【项目全名】: {name}\n请根据该开源项目名称及常见技术生态进行深度商业价值与痛点推演。"
                repo_infos[name] = {}
            elif isinstance(item, tuple) and len(item) == 2:
                ctx, info = item
                repo_contexts[name] = ctx or f"【项目全名】: {name}"
                repo_infos[name] = info
            else:
                repo_contexts[name] = f"【项目全名】: {name}"
                repo_infos[name] = {}

        # 3. 分批调用 LLM（每批 4 个，保证模型输出完整且不截断）
        batch_size = 4
        names_list = list(repo_contexts.keys())
        
        system_prompt = (
            "你是一位顶级技术商业分析师与资深开源投资人。请对提供的 GitHub 开源项目进行深度商业研判，挖掘其潜在的商业价值、市场机会与用户核心痛点。\n\n"
            "【输出要求】\n"
            "1. 必须使用中文输出，用词专业、深刻、具体，直击要害。\n"
            "2. 严禁输出“待挖掘”、“暂无”、“无法判断”、“暂未识别”等任何模糊或占位词汇！\n"
            "3. 即使项目处于早期或缺少 Issue，也必须根据其技术栈、README 架构与生态定位进行前瞻性商业推演：\n"
            "   - market_gaps: 识别现有工具链缺陷或竞品不足，挖掘独立 SaaS、开发者插件或开源替代商用的市场切入点。\n"
            "   - pain_points: 挖掘目标用户在性能、配置、部署、成本或协作上的核心技术/业务痛点。\n"
            "   - commercial_signals: 推演其商业化路径（如云原生 Hosted 托管版、企业私有化支持、SaaS 订阅、高级团队协作套件等）。\n"
            "4. 必须输出标准的 JSON 数组格式，严格保留输入的项目 full_name 作为 name 字段：\n"
            '[{"name": "owner/repo", "market_gaps": "...", "pain_points": "...", "commercial_signals": "..."}, ...]'
        )

        for i in range(0, len(names_list), batch_size):
            chunk_names = names_list[i:i + batch_size]
            prompt_content = "\n\n====================\n\n".join([f"### 项目 full_name: {name}\n{repo_contexts[name]}" for name in chunk_names])
            
            try:
                data_list = await self.llm_client.chat(
                    system_prompt=system_prompt,
                    user_prompt=f"请对以下 {len(chunk_names)} 个项目进行深度商业研判并输出 JSON 数组：\n\n{prompt_content}",
                    temperature=0.3,
                    json_mode=True
                )
            except Exception as e:
                logger.error(f"LLM 批处理请求异常: {e}")
                data_list = None
            
            if data_list:
                if isinstance(data_list, dict):
                    if 'projects' in data_list and isinstance(data_list['projects'], list):
                        data_list = data_list['projects']
                    elif 'items' in data_list and isinstance(data_list['items'], list):
                        data_list = data_list['items']
                    else:
                        data_list = [data_list]
                
                if isinstance(data_list, list):
                    for item in data_list:
                        if not isinstance(item, dict):
                            continue
                        
                        raw_name = str(item.get('name') or item.get('repo') or item.get('full_name') or '').strip()
                        # 智能名称对齐（精确、大小写不敏感、后缀匹配）
                        matched_name = None
                        for cn in chunk_names:
                            if raw_name == cn or raw_name.lower() == cn.lower():
                                matched_name = cn
                                break
                            elif raw_name.lower() == cn.split('/')[-1].lower():
                                matched_name = cn
                                break
                            elif cn.lower().endswith(raw_name.lower()):
                                matched_name = cn
                                break
                        
                        if matched_name and not self._is_low_quality(item):
                            item['name'] = matched_name
                            results[matched_name] = item
                            self._save_analysis_to_db(matched_name, item)

        # 4. 对批处理中遗漏或质量未达标的项目，进行单项目针对性兜底与合成
        for name in names_list:
            if name not in results or self._is_low_quality(results.get(name)):
                logger.info(f"项目 {name} 未在批量分析中获得高质量结果，启动独立重试与兜底...")
                try:
                    single_prompt = f"### 项目: {name}\n{repo_contexts.get(name, '')}"
                    single_res = await self.llm_client.chat(
                        system_prompt=system_prompt,
                        user_prompt=f"请单独分析以下项目并输出包含 name, market_gaps, pain_points, commercial_signals 的 JSON 对象：\n\n{single_prompt}",
                        temperature=0.3,
                        json_mode=True
                    )
                    if isinstance(single_res, list) and single_res:
                        single_res = single_res[0]
                    if isinstance(single_res, dict):
                        single_res['name'] = name
                    
                    if isinstance(single_res, dict) and not self._is_low_quality(single_res):
                        results[name] = single_res
                        self._save_analysis_to_db(name, single_res)
                    else:
                        # 使用高质量元数据合成兜底，彻底杜绝“待挖掘”
                        synthetic = self._build_synthetic_intent(name, repo_infos.get(name), single_res)
                        results[name] = synthetic
                        self._save_analysis_to_db(name, synthetic)
                except Exception as e:
                    logger.error(f"单项目兜底分析 {name} 失败: {e}")
                    synthetic = self._build_synthetic_intent(name, repo_infos.get(name))
                    results[name] = synthetic
                    self._save_analysis_to_db(name, synthetic)
        
        return results

    async def analyze_intent(self, full_name):
        """向下兼容单项目分析"""
        res = await self.analyze_intent_batch([full_name])
        return res.get(full_name)

    def _save_analysis_to_db(self, full_name, data):
        """持久化分析结果"""
        sql = """
        INSERT INTO intent_analyses (repo_full_name, market_gaps, pain_points, commercial_signals, raw_analysis)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            market_gaps=VALUES(market_gaps),
            pain_points=VALUES(pain_points),
            commercial_signals=VALUES(commercial_signals),
            raw_analysis=VALUES(raw_analysis)
        """
        db_manager.execute_batch(sql, [(
            full_name, data.get("market_gaps"), data.get("pain_points"), 
            data.get("commercial_signals"), json.dumps(data, ensure_ascii=False)
        )], db_type="insight")

    def get_cached_analysis(self, full_name):
        """获取已缓存的分析"""
        res = db_manager.execute_query(
            f"SELECT market_gaps, pain_points, commercial_signals FROM intent_analyses WHERE repo_full_name = '{full_name}'",
            db_type="insight"
        )
        return res[0] if res else None

intent_detector = IntentDetector()

