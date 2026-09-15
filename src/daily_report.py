import logging
import os
import asyncio
import re
import json
from datetime import datetime
from .database import db_manager
from .crawler_zai import repo_analyzer
from .notion_client import notion_client
from .config import load_config

logger = logging.getLogger(__name__)

def sanitize_ai_summary(text):
    """
    清理 AI 摘要中的干扰项，处理 zread.ai 爬取的冗余信息。
    """
    if not text: return "暂无解析。"
    
    # 如果发现旧数据依然是完全没清理掉的占位符或 Cloudflare 错误页面，直接返回托底信息
    placeholder_keywords = [
        "提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多",
        "Ask anything about the Repository", "Ask anything about this", "Responsed by AI", "May contain mistakes",
        "Private Repos", "Subscription", "Zread Discover Trending",
        "尚未收录", "未找到该仓库", "正在生成中", "Repository not found", "No overview available",
        "Toggle theme", "Chat with codebase",
        "Cloudflare Ray ID", "Visit cloudflare.com", "gateway time-out", "gateway timeout",
        "Bad gateway", "Web server is down", "Error 524", "Error 504", "Error 502", "Error 520",
        "Performance & security by", "Checking your browser", "Just a moment...",
        "504 Gateway Time-out", "502 Bad Gateway", "524 A timeout occurred",
        "The web server reported a gateway time-out error"
    ]
    if any(kw.lower() in text.lower() for kw in placeholder_keywords):
        return "暂无解析。"

    # 1. 移除 Markdown 标题层级，保留加粗标题
    text = re.sub(r'^#+\s+.*?\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'#+\s+(.*?)\n', r'**\1**\n', text)
    
    # 2. 移除图片
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    
    # 3. 移除 zread.ai 特有的“X 分钟入门”或“阅读时间”字样 (增强版)
    text = re.sub(r'(?:\d+|约\d+|[一二三四五六七八九十]+)?\s*分钟\s*[:：]?\s*(?:入门|阅读|学习|解读)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'阅读时间\s*[:：]?\s*(?:\d+|约\d+|[一二三四五六七八九十]+)?\s*分钟?', '', text, flags=re.IGNORECASE)
    
    # 4. 移除“来源：[...]”或“Source: [...]”字样及链接
    text = re.sub(r'(?:来源|Source|参考)\s*[:：]\s*\[.*?\]\(.*?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[[a-zA-Z0-9_\-\.]+\.(?:py|md|js|go|rs|cpp|h|ts|txt)\](?:\(https?://.*?\))?', '', text)
    
    # 5. 移除孤立的 zread.ai 链接
    text = re.sub(r'https?://zread\.ai/[^\s\)]+', '', text)

    # 6. 移除末尾可能的逗号和脚本/文件链接 (递归移除末尾垃圾)
    while True:
        temp = text.strip()
        new_text = re.sub(r'[,，\s]*\[.*?\]\(.*?\)\s*$', '', temp)
        if new_text == temp:
            break
        text = new_text
    
    # 7. 移除开头可能的“上次索引:[...]” 及其之前的全部导航内容，以及紧跟的独立短导航词
    # 先处理带有“上次索引:”的情况，往往前面都是网站噪音
    text = re.sub(r'^.*?上次索引\s*[:：]\s*.*?\n', '', text.strip(), flags=re.IGNORECASE | re.DOTALL)
    # 移除紧跟的简短导航词汇和空行
    text = re.sub(r'^(\s*(快速入门|最新动态|资讯|深入解析|Overview|Quick\s*Start)\s*\n)+', '', text, flags=re.IGNORECASE)

    # 8. 移除末尾多余的标点符号（逗号、顿号）和空白字符，确保结束干净
    text = re.sub(r'[,，、\s]+$', '', text.strip())
    
    return text.strip()

def clean_summary_for_table(text):
    """针对纯文本展示进行的深度清洗和截断。"""
    if not text: return ""
    
    # 如果发现旧数据依然是完全没清理掉的占位符或 Cloudflare 错误页面，直接返回托底信息
    placeholder_keywords = [
        "提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多",
        "Ask anything about the Repository", "Ask anything about this", "Responsed by AI", "May contain mistakes",
        "Private Repos", "Subscription", "Zread Discover Trending",
        "尚未收录", "未找到该仓库", "正在生成中", "Repository not found", "No overview available",
        "Toggle theme", "Chat with codebase",
        "Cloudflare Ray ID", "Visit cloudflare.com", "gateway time-out", "gateway timeout",
        "Bad gateway", "Web server is down", "Error 524", "Error 504", "Error 502", "Error 520",
        "Performance & security by", "Checking your browser", "Just a moment...",
        "504 Gateway Time-out", "502 Bad Gateway", "524 A timeout occurred",
        "The web server reported a gateway time-out error"
    ]
    if any(kw.lower() in text.lower() for kw in placeholder_keywords):
        return "暂无解析。"

    # 0. 移除前置网页噪音内容 (与 sanitize_ai_summary 保持一致)
    text = re.sub(r'^.*?上次索引\s*[:：]\s*.*?\n', '', text.strip(), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'^(\s*(快速入门|最新动态|资讯|深入解析|Overview|Quick\s*Start)\s*\n)+', '', text, flags=re.IGNORECASE)

    # 1. 移除 Markdown 标记（标题、图片、链接）
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text) # 移除图片
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text) # 仅保留链接文本
    text = re.sub(r'#+\s+', '', text) # 移除标题符号
    text = re.sub(r'\*\*|\*', '', text) # 移除加粗/斜体
    text = re.sub(r'`', '', text) # 移除代码块标记
    text = re.sub(r'<.*?>', '', text) # 移除 HTML 标签
    
    # 2. 移除多余空白和换行
    text = text.replace("\n", " ").replace("\r", " ")
    text = text.replace("|", " ")
    text = re.sub(r'\s+', ' ', text)
    
    # 3. 移除末尾多余的标点符号（逗号、顿号）和空白字符
    text = re.sub(r'[,，、\s]+$', '', text.strip())

    # 4. 增加截断长度至 300 字
    if len(text) > 300:
        text = text[:297] + "..."
    return text.strip()

def create_callout_block(content, emoji="🚀", color="gray_background", children=None):
    """创建 Notion Callout 模块，支持子 Block。"""
    block = {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": notion_client._parse_rich_text(content),
            "icon": {"emoji": emoji},
            "color": color
        }
    }
    if children:
        block["callout"]["children"] = children
    return block

def create_toggle_block(title, children_blocks):
    """
    创建 Notion 折叠模块。
    注意：为了防止嵌套过深导致报错，这里会对子 Block 进行合法性过滤。
    """
    valid_children = [c for c in children_blocks if notion_client._is_valid_block(c)]
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": notion_client._parse_rich_text(title),
            "children": valid_children
        }
    }

def write_github_step_summary(title: str, model_name: str, report_type: str = "daily"):
    """
    仅在 GitHub Actions 中记录脱敏的任务状态与模型元信息，
    绝不输出具体技术与商业洞察正文，确保敏感商业情报私密性。
    """
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        md = (
            f"## {title}\n"
            f"- ⏰ **任务完成时间**: {now_str} (CST)\n"
            f"- 🤖 **总结分析模型**: `{model_name}`\n"
            f"- 🔒 **隐私保护**: 完整研判内容已私密推送到专属 Notion，不在公开 CI/CD 日志中展示。\n"
        )
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md + "\n\n")
        logger.info(f"已写入 GitHub Actions 运行状态摘要 (模型: {model_name})")
    except Exception as e:
        logger.warning(f"写入 GITHUB_STEP_SUMMARY 失败: {e}")

from .llm_client import LLMClient
from .user_analyzer import user_analyzer

async def generate_global_insight(rising_stars, hidden_gems, user_bursts, return_model: bool = False):
    """
    使用独立的 report_llm 模型列表生成每日综合洞察。
    增加对 User 异动的关注。
    """
    settings = load_config()
    conf = settings.get("report_llm", {})
    if not conf.get("api_key") or not conf.get("model_names"):
        conf = settings.get("llm", {})

    if not conf.get("api_key") or not conf.get("model_names"):
        msg = "今日暂无全局洞察总结。"
        return (msg, "无配置模型") if return_model else msg

    llm_client = LLMClient(
        api_key=conf.get("api_key"),
        base_url=conf.get("base_url"),
        model_names=conf.get("model_names")
    )

    # 准备上下文
    context = "今日核心发现的项目列表：\n"
    for r in rising_stars[:15]:
        context += f"- [黑马] {r['full_name']}: {r.get('description', '')} (新增 Stars: {r.get('star_velocity_24h')})\n"
    for g in hidden_gems[:10]:
        context += f"- [潜力股] {g['full_name']}: {g.get('description', '')}\n"
    
    if user_bursts:
        context += "\n今日开发者异动：\n"
        for u in user_bursts[:5]:
            context += f"- {u['login']} 活跃度爆发，比平时高 {u['ratio']:.1f} 倍。\n"

    system_prompt = (
        "你是一位顶级技术投资人和商业战略专家。请根据今日 GitHub 发现的项目动态，产出一段深刻的“每日github洞察综述”（中文，800字左右）。\n"
        "你的目标是：识别今日最值得关注的技术范式迁移、可能的商业机会或开发者社区的集体意图转变以及其他洞察，结构清晰，排版优美。"
    )

    insight, used_model = await llm_client.chat(
        system_prompt=system_prompt,
        user_prompt=context,
        temperature=0.5,
        return_model=True
    )

    final_insight = insight or "今日技术动态活跃，建议重点关注上述黑马项目的技术选型。"
    model_name = used_model or (conf.get("model_names", ["未知模型"])[0] if conf.get("model_names") else "未知模型")
    if not insight:
        model_name = f"{model_name} (调用失败，使用托底文本)"

    if return_model:
        return final_insight, model_name
    return final_insight

def get_zread_link(full_name):
    """生成 zread.ai 的深度讲解链接。"""
    return f"https://zread.ai/{full_name}"

async def generate_daily_report_blocks(rising_limit=15, hot_limit=50, hidden_limit=10):
    """
    生成结构化的 Notion Blocks 报告。
    """
    logger.info("正在生成精美的每日 Alpha 报告模块...")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 数据准备
    from .velocity_calc import get_rising_stars, collect_fine_grained_signals
    # 在生成报告前，先为 Rising Stars 采集细粒度信号
    collect_fine_grained_signals(limit=rising_limit)
    rising_stars = get_rising_stars(limit=rising_limit)

    # 采集开发者异动信号
    user_bursts = user_analyzer.analyze_kp_activity_bursts()
    insider_clusters = user_analyzer.detect_insider_clusters()
    hireable_kps = db_manager.execute_query(
        "SELECT login, bio, followers FROM users WHERE is_key_person = 1 AND hireable = 1 ORDER BY followers DESC LIMIT 10",
        db_type="source"
    )

    hot_repos = db_manager.execute_query(
        f"SELECT full_name, description, stargazers_count, seed_source FROM repos WHERE seed_source LIKE '%Trending%' OR seed_source LIKE '%Top100%' ORDER BY stargazers_count DESC LIMIT {hot_limit}",
        db_type="source"
    )

    hidden_gems = db_manager.execute_query(
        f"SELECT id, full_name, description, stargazers_count FROM repos WHERE super_seed = 1 ORDER BY stargazers_count ASC LIMIT {hidden_limit}",
        db_type="source"
    )

    # 2. 生成全局洞察 (使用独立模型)
    global_insight, insight_model = await generate_global_insight(
        rising_stars, hidden_gems, user_bursts, return_model=True
    )
    logger.info(f"每日全局洞察生成完成，使用模型: {insight_model}")

    gem_ids = [str(g['id']) for g in hidden_gems]
    counts_res = db_manager.execute_query(
        f"SELECT repo_id, COUNT(user_id) as cnt FROM repo_user_relations WHERE repo_id IN ({', '.join(gem_ids)}) GROUP BY repo_id",
        db_type="relation"
    )
    repo_counts = {r['repo_id']: r['cnt'] for r in counts_res}

    # 3. 并发调用 AI 进行解析
    # 扩大解析范围，确保 Top 50 和 Hidden Gems 都有 AI 摘要
    repos_to_analyze = list(dict.fromkeys(
        [r['full_name'] for r in rising_stars] + 
        [r['full_name'] for r in hot_repos] + 
        [r['full_name'] for r in hidden_gems]
    ))
    ai_summaries = await repo_analyzer.analyze_batch(repos_to_analyze)

    from .intent_detector import intent_detector
    # 批量进行深度意图分析 (Rising Stars 和 Hidden Gems 必须有)
    intent_targets = list(dict.fromkeys(
        [r['full_name'] for r in rising_stars] + 
        [r['full_name'] for r in hidden_gems]
    ))
    intent_data = await intent_detector.analyze_intent_batch(intent_targets)

    # 4. 构建 Blocks
    blocks = []
    blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": [{"type": "text", "text": {"content": f"🚀 GitHub Daily Alpha Radar | {today_str}"}}]}})

    # 插入全局洞察 (解析 Markdown 为子 Block 以保持排版)
    insight_blocks = notion_client.markdown_to_blocks(global_insight)
    if insight_blocks:
        first_block = insight_blocks[0]
        title_text = "今日github洞察"
        insight_children = list(insight_blocks)
        
        # 尝试从第一块提取标题
        if first_block["type"] == "paragraph":
            rich_text = first_block["paragraph"]["rich_text"]
            if rich_text and len(rich_text[0]["text"]["content"]) < 100:
                title_text = rich_text[0]["text"]["content"]
                insight_children = list(insight_blocks[1:])
        elif first_block["type"].startswith("heading"):
            h_type = first_block["type"]
            rich_text = first_block[h_type]["rich_text"]
            if rich_text:
                title_text = rich_text[0]["text"]["content"]
                insight_children = list(insight_blocks[1:])
            
        # 在 Callout 内部末尾明确附带总结生成模型
        insight_children.append({"object": "block", "type": "divider", "divider": {}})
        insight_children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": notion_client._parse_rich_text(f"🤖 **总结生成模型**: `{insight_model}`")
            }
        })

        display_title = f"{title_text} | 模型: {insight_model}"
        blocks.append(create_callout_block(display_title, emoji="🎯", color="blue_background", children=insight_children))
    else:
        fallback_children = [
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": notion_client._parse_rich_text(global_insight)}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": notion_client._parse_rich_text(f"🤖 **总结生成模型**: `{insight_model}`")}}
        ]
        blocks.append(create_callout_block(f"今日github洞察 | 模型: {insight_model}", emoji="🎯", color="blue_background", children=fallback_children))

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # --- 第一部分: Rising Stars ---
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🔥 今日增长黑马 (Top 15)"}, "annotations": {"color": "orange", "bold": True}}]}})

    # 为了减少滑动长度，将 Top 15 分为 3 组，每组 5 个，放入折叠块
    for g in range(0, len(rising_stars), 5):
        group_repos = rising_stars[g:g+5]
        group_title = f"🚀 增长黑马项目 #{g+1} - #{g+len(group_repos)}"
        group_blocks = []
        
        for i, repo in enumerate(group_repos, g + 1):
            fn = repo['full_name']
            summary = sanitize_ai_summary(ai_summaries.get(fn, "暂无深度解析。"))
            z_link = get_zread_link(fn)
            
            # 补充细粒度信号
            fine_signals = []
            if repo.get('latest_release_tag'):
                fine_signals.append(f"📦 最新版本: `{repo['latest_release_tag']}`")
            if repo.get('issue_heat_score') and repo['issue_heat_score'] > 0:
                fine_signals.append(f"💬 议题热度: `{int(repo['issue_heat_score'])}` 条评论")
            
            signal_text = " | ".join(fine_signals) if fine_signals else "暂无细粒度信号"

            group_blocks.append(create_callout_block(f"**{i}. {fn}**", emoji="🔥", color="orange_background"))
            group_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": notion_client._parse_rich_text(f"[🔗 GitHub](https://github.com/{fn}) | [📖 zread.ai]({z_link})\n{signal_text}")}})

            idat = intent_data.get(fn, {})
            market = idat.get('market_gaps') or "针对该细分领域提供轻量级开源方案，填补了开箱即用工具生态的空白。"
            pain = idat.get('pain_points') or "降低同类方案的配置复杂度与学习成本，解决核心性能及环境适配痛点。"
            signal = idat.get('commercial_signals') or "具备发展为云端托管服务 (Hosted SaaS)、企业私有化支持与增值插件生态的商业潜力。"
            
            intent_text = (
                f"**💡 市场空白**\n{market}\n\n"
                f"**⚠️ 核心痛点**\n{pain}\n\n"
                f"**💰 商业信号**\n{signal}"
            )
            group_blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": notion_client._parse_rich_text(intent_text)}})

            summary_blocks = notion_client.markdown_to_blocks(summary)
            group_blocks.append(create_toggle_block("✨ AI 技术洞察", summary_blocks))
            group_blocks.append({"object": "block", "type": "divider", "divider": {}})
        
        blocks.append(create_toggle_block(group_title, group_blocks))

    # --- 第二部分: User Radar ---
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "👤 开发者雷达 (User Radar)"}, "annotations": {"color": "purple", "bold": True}}]}})

    radar_md_lines = []
    if user_bursts:
        radar_md_lines.append("### 💥 活跃爆发大牛")
        for u in user_bursts[:5]:
            radar_md_lines.append(f"- [**@{u['login']}**](https://github.com/{u['login']}): 活跃度较平时增长了 **{u['ratio']:.1f}** 倍")
    
    if insider_clusters:
        if radar_md_lines: radar_md_lines.append("") 
        radar_md_lines.append("### 🕸️ 圈子协同背书")
        for c in insider_clusters:
            kp_links = [f"[**@{l}**](https://github.com/{l})" for l in c['kp_logins']]
            fn = c['full_name']
            z_link = get_zread_link(fn)
            radar_md_lines.append(f"- 项目 [**{fn}**](https://github.com/{fn}) ([📖 zread]({z_link})) 获得了 {'、'.join(kp_links)} 的共同背书 (Star: {c['stargazers']})")
    
    if radar_md_lines:
        radar_blocks = notion_client.markdown_to_blocks("\n".join(radar_md_lines))
        # 包装在一个下拉 Toggle 中
        radar_callout = create_callout_block("点击展开今日开发者异动监控详情", emoji="📡", color="purple_background", children=radar_blocks)
        blocks.append(create_toggle_block("📡 查看开发者动态雷达详情", [radar_callout]))
    else:
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "今日开发者动态平稳，暂无爆发性异动。"}, "annotations": {"italic": True}}]}})

    # --- 第三部分: Talent Alpha ---
    if hireable_kps:
        blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "👨‍💻 人才合作机会 (Talent Alpha)"}, "annotations": {"color": "green", "bold": True}}]}})
        talent_md = []
        for t in hireable_kps:
            bio_clean = clean_summary_for_table(t['bio']) if t['bio'] else "暂无简介"
            talent_md.append(f"- [**@{t['login']}**](https://github.com/{t['login']}) ({t['followers']} followers): {bio_clean}")
        
        talent_blocks = notion_client.markdown_to_blocks("\n".join(talent_md))
        blocks.append(create_toggle_block("🌟 正在寻找机会的高影响力开发者", talent_blocks))

    # --- 第四部分: Hot 50 ---
    # ... (保持原有的 Hot 50 逻辑，它已经有分组折叠了)
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🌟 今日全网热门 Top 50"}, "annotations": {"color": "blue"}}]}})

    group_size = 10
    for g in range(0, len(hot_repos), group_size):
        group_repos = hot_repos[g:g+group_size]
        group_title = f"📦 热门项目排行 #{g+1} - #{g+len(group_repos)}"
        group_blocks = []
        for i, repo in enumerate(group_repos, g + 1):
            fn = repo['full_name']
            z_link = get_zread_link(fn)
            summary = ai_summaries.get(fn)
            if not summary or summary == "暂无解析。":
                summary = repo.get('description') or "No description"
            
            safe_summary = sanitize_ai_summary(summary)
            if not safe_summary or safe_summary == "暂无解析。":
                safe_summary = clean_summary_for_table(summary)
            
            group_blocks.append({
                "object": "block", "type": "paragraph", 
                "paragraph": {"rich_text": notion_client._parse_rich_text(f"**{i}. {fn}** [🔗 GitHub](https://github.com/{fn}) | [📖 zread]({z_link})")}
            })
            group_blocks.append({
                "object": "block", "type": "quote", 
                "quote": {"rich_text": notion_client._parse_rich_text(safe_summary)}
            })
        
        blocks.append(create_toggle_block(group_title, group_blocks))

    # --- 第五部分: Hidden Gems ---
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "💎 开发者关联潜力股"}, "annotations": {"color": "purple"}}]}})

    if hidden_gems:
        # 潜力股也进行 5 个一组的分组，防止过长
        for g in range(0, len(hidden_gems), 5):
            group_gems = hidden_gems[g:g+5]
            group_title = f"💎 潜力项目展示 #{g+1} - #{g+len(group_gems)}"
            group_blocks = []
            
            for i, gem in enumerate(group_gems, g + 1):
                fn = gem['full_name']
                summary = sanitize_ai_summary(ai_summaries.get(fn, "暂无解析。"))
                z_link = get_zread_link(fn)
                
                group_blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": notion_client._parse_rich_text(f"{i}. {fn}")}})
                group_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": notion_client._parse_rich_text(f"[🔗 GitHub](https://github.com/{fn}) | [📖 zread.ai]({z_link})")}})

                idat = intent_data.get(fn, {})
                market = idat.get('market_gaps') or "针对该细分领域提供轻量级开源方案，填补了开箱即用工具生态的空白。"
                pain = idat.get('pain_points') or "降低同类方案的配置复杂度与学习成本，解决核心性能及环境适配痛点。"
                signal = idat.get('commercial_signals') or "具备发展为云端托管服务 (Hosted SaaS)、企业私有化支持与增值插件生态的商业潜力。"
                
                intent_text = (
                    f"**💡 市场空白**\n{market}\n\n"
                    f"**⚠️ 核心痛点**\n{pain}\n\n"
                    f"**💰 商业信号**\n{signal}"
                )
                group_blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": notion_client._parse_rich_text(intent_text)}})
                
                group_blocks.append(create_callout_block(f"高手背书: `{repo_counts.get(gem['id'], 2)}` 人关注", emoji="✨", color="purple_background"))
                group_blocks.append(create_toggle_block("🔍 为什么值得关注？", notion_client.markdown_to_blocks(summary)))
                group_blocks.append({"object": "block", "type": "divider", "divider": {}})
            
            blocks.append(create_toggle_block(group_title, group_blocks))
    else:
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "今日暂未发现显著的新项目关联信号。"}, "annotations": {"italic": True}}]}})


    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (CST) | 总结生成模型: {insight_model}"
                    },
                    "annotations": {"color": "gray"}
                }
            ]
        }
    })

    # 写入 GitHub Actions 运行摘要 (Step Summary) - 仅元信息，不含敏感洞察正文
    write_github_step_summary(
        title=f"🚀 GitHub Daily Alpha Radar | {today_str}",
        model_name=insight_model,
        report_type="daily"
    )

    return blocks

def generate_daily_report(rising_limit=15, hot_limit=50, hidden_limit=10):
    """
    托底方法，保持与旧 API 兼容。
    """
    blocks = asyncio.run(generate_daily_report_blocks(rising_limit, hot_limit, hidden_limit))
    title = f"GitHub每日洞察"
    success = notion_client.push_blocks(blocks, title)
    return "Report generated and pushed to Notion." if success else "Failed to push report."

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(generate_daily_report())

