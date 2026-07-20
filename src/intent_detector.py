import logging
import json
import asyncio
import re
from .github_client import github_client
from .database import db_manager
from .config import load_config
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

class IntentDetector:
    """
    分析 GitHub Issues 和 README，深度挖掘市场空白、用户痛点和商业信号。
    """

    def __init__(self):
        self._llm_client = None

    @property
    def llm_client(self):
        if self._llm_client is None:
            settings = load_config()
            # 优先使用专门的 intent_llm 配置，否则回退到通用 llm 配置
            llm_conf = settings.get("intent_llm", {})
            if not llm_conf.get("api_key"):
                llm_conf = settings.get("llm", {})

            self._llm_client = LLMClient(
                api_key=llm_conf.get("api_key"),
                base_url=llm_conf.get("base_url"),
                model_names=llm_conf.get("model_names")
            )
        return self._llm_client

    def _is_low_quality(self, data):
        """检测分析结果是否质量较低（如英文过多或全是待挖掘）"""
        if not data: return True
        
        # 1. 检查是否包含“待挖掘”字样
        text_content = f"{data.get('market_gaps', '')} {data.get('pain_points', '')} {data.get('commercial_signals', '')}"
        if text_content.count("待挖掘") >= 2:
            return True
        
        # 2. 检查中文字符比例。如果极低（如 < 10%），说明可能是纯英文输出
        chinese_chars = len(re.findall(r'[\u4e00-\u9fa5]', text_content))
        if len(text_content.strip()) > 20 and chinese_chars / len(text_content) < 0.1:
            return True
            
        return False

    async def fetch_repo_context(self, full_name):
        """抓取 README 和高信号 Issue 文本作为分析背景"""
        logger.info(f"正在获取 {full_name} 的深度上下文信息...")
        
        # 1. 获取 README (取前 1500 字，通常包含核心介绍)
        readme = await asyncio.to_thread(github_client.get_readme, full_name)
        readme_summary = readme[:1500] if readme else "无 README 内容"

        # 2. 获取 Repo 基础信息
        repo_info = await asyncio.to_thread(github_client.get_repo, full_name)
        description = repo_info.get("description", "无项目描述") if repo_info else "无项目描述"

        # 3. 抓取评论数最多的 10 个 Open Issue (略微减少数量以平衡 Context 长度)
        issues = await asyncio.to_thread(github_client.request, "GET", f"https://api.github.com/repos/{full_name}/issues", params={
            "state": "open",
            "sort": "comments",
            "per_page": 10
        })
        
        issue_lines = []
        if issues and isinstance(issues, list):
            for issue in issues:
                if "pull_request" in issue: continue
                title = issue.get("title", "")
                labels = [l["name"] for l in issue.get("labels", [])]
                body = issue.get("body", "") or ""
                issue_lines.append(f"- [Issue] {title} (标签: {', '.join(labels)})\n  内容: {body[:300]}")
        
        issues_summary = "\n".join(issue_lines)
        # 限制 issues 总结部分的长度，避免过长的 payload 被反向代理/WAF 阻断 (403/413)
        issues_summary = issues_summary[:3000]

        context = f"项目描述: {description}\n\nREADME 摘要:\n{readme_summary}\n\n最近热门 Issue 摘要:\n{issues_summary}"
        return context

    async def analyze_intent_batch(self, full_names):
        """批量进行深度意图分析，提高 LLM 利用效率"""
        if not full_names: return {}
        
        # 1. 先查缓存
        results = {}
        missing_names = []
        for name in full_names:
            cached = self.get_cached_analysis(name)
            if cached and not self._is_low_quality(cached):
                results[name] = cached
            else:
                if cached:
                    logger.info(f"项目 {name} 的缓存质量较低或为英文，将重新分析。")
                missing_names.append(name)
        
        if not missing_names:
            return results

        # 2. 并行获取缺失项目的 Context
        logger.info(f"正在获取 {len(missing_names)} 个项目的深度上下文...")
        context_tasks = [self.fetch_repo_context(name) for name in missing_names]
        contexts = await asyncio.gather(*context_tasks)
        
        repo_contexts = {name: ctx for name, ctx in zip(missing_names, contexts) if ctx}
        if not repo_contexts:
            return results

        # 3. 分批调用 LLM
        batch_size = 6
        names_list = list(repo_contexts.keys())
        
        system_prompt = (
            "你是一位资深的商业分析师。请对提供的 GitHub 项目进行深度研判，挖掘其潜在的商业价值与市场机会。\n"
            "对于每个项目，请从以下维度给出洞察（必须使用中文输出，严禁输出英文）：\n"
            "1. market_gaps: 识别用户高频请求但现有工具缺失的功能，或现有竞品的不足之处，寻找独立 SaaS 的切入点。\n"
            "2. pain_points: 识别让用户最感到挫败、耗时或难以解决的技术/业务痛点，解决这些问题意味着极高的用户粘性。\n"
            "3. commercial_signals: 捕捉项目背后可能的商业化路径，如付费咨询、SaaS 化潜力、企业级赞助、闭环生态、或是对现有商业软件的颠覆性影响。\n\n"
            "要求：\n"
            "- 洞察必须深刻且具体，避免泛泛而谈。\n"
            "- 如果信号不明显，请根据项目功能推断其潜在的应用场景和商业价值，而非直接返回“待挖掘”。\n"
            "- 必须输出标准的 JSON 数组格式：\n"
            '[{"name": "owner/repo", "market_gaps": "...", "pain_points": "...", "commercial_signals": "..."}, ...]'
        )

        for i in range(0, len(names_list), batch_size):
            chunk_names = names_list[i:i + batch_size]
            prompt_content = "\n\n".join([f"### 项目: {name}\n{repo_contexts[name]}" for name in chunk_names])
            
            data_list = await self.llm_client.chat(
                system_prompt=system_prompt,
                user_prompt=f"请分析以下项目：\n\n{prompt_content}",
                temperature=0.3,
                json_mode=True
            )
            
            if data_list:
                if not isinstance(data_list, list): 
                    data_list = [data_list]
                
                for item in data_list:
                    if isinstance(item, dict) and 'name' in item:
                        results[item['name']] = item
                        self._save_analysis_to_db(item['name'], item)
        
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
            data.get("commercial_signals"), json.dumps(data)
        )], db_type="insight")

    def get_cached_analysis(self, full_name):
        """获取已缓存的分析"""
        res = db_manager.execute_query(
            f"SELECT market_gaps, pain_points, commercial_signals FROM intent_analyses WHERE repo_full_name = '{full_name}'",
            db_type="insight"
        )
        return res[0] if res else None

intent_detector = IntentDetector()
