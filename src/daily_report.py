import logging
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
    
    # 如果发现旧数据依然是完全没清理掉的占位符（例如英文无索引页面），直接返回托底信息
    placeholder_keywords = [
        "提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多",
        "Ask anything about the Repository", "Responsed by AI", "May contain mistakes",
        "Private Repos", "Subscription", "Zread Discover Trending"
    ]
    if any(kw in text for kw in placeholder_keywords):
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
    
    # 7. 移除开头可能的“上次索引:[...]”
    text = re.sub(r'^上次索引\s*[:：]\s*\[.*?\]\(.*?\)\s*', '', text.strip(), flags=re.IGNORECASE)
    
    return text.strip()

def clean_summary_for_table(text):
    """针对纯文本展示进行的深度清洗和截断。"""
    if not text: return ""
    
    # 如果发现旧数据依然是完全没清理掉的占位符（例如英文无索引页面），直接返回托底信息
    placeholder_keywords = [
        "提问任何有关此仓库的问题", "回答由AI生成", "私有仓库", "收藏夹", "登录以查看更多",
        "Ask anything about the Repository", "Responsed by AI", "May contain mistakes",
        "Private Repos", "Subscription", "Zread Discover Trending"
    ]
    if any(kw in text for kw in placeholder_keywords):
        return "暂无解析。"

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
    
    # 3. 增加截断长度至 300 字
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

from .llm_client import LLMClient
from .user_analyzer import user_analyzer

async def generate_global_insight(rising_stars, hidden_gems, user_bursts):
    """
    使用独立的 report_llm 模型列表生成每日综合洞察。
    增加对 User 异动的关注。
    """
    settings = load_config()
    conf = settings.get("report_llm", {})
    if not conf.get("api_key") or not conf.get("model_names"):
        return "今日暂无全局洞察总结。"

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

    insight = await llm_client.chat(
        system_prompt=system_prompt,
        user_prompt=context,
        temperature=0.5
    )

    return insight or "今日技术动态活跃，建议重点关注上述黑马项目的技术选型。"

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
    global_insight = await generate_global_insight(rising_stars, hidden_gems, user_bursts)

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
        # 使用第一行作为 Callout 的标题，其余作为子 Block
        first_block = insight_blocks[0]
        title_text = "今日github洞察"
        insight_children = insight_blocks
        
        # 尝试从第一块提取标题
        if first_block["type"] == "paragraph":
            rich_text = first_block["paragraph"]["rich_text"]
            if rich_text and len(rich_text[0]["text"]["content"]) < 100:
                title_text = rich_text[0]["text"]["content"]
                insight_children = insight_blocks[1:]
        elif first_block["type"].startswith("heading"):
            h_type = first_block["type"]
            rich_text = first_block[h_type]["rich_text"]
            if rich_text:
                title_text = rich_text[0]["text"]["content"]
                insight_children = insight_blocks[1:]
            
        blocks.append(create_callout_block(title_text, emoji="🎯", color="blue_background", children=insight_children))
    else:
        blocks.append(create_callout_block(global_insight, emoji="🎯", color="blue_background"))

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

            if fn in intent_data:
                idat = intent_data[fn]
                market = idat.get('market_gaps') or "待挖掘"
                pain = idat.get('pain_points') or "待挖掘"
                signal = idat.get('commercial_signals') or "待挖掘"
                
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
            if not summary or "[自动托底]" in summary:
                summary = repo['description'] or "No description"
            
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

                if fn in intent_data:
                    idat = intent_data[fn]
                    market = idat.get('market_gaps') or "待挖掘"
                    pain = idat.get('pain_points') or "待挖掘"
                    signal = idat.get('commercial_signals') or "待挖掘"
                    
                    intent_text = (
                        f"**💡 市场空白**\n{market}\n\n"
                        f"**⚠️ 核心痛点**\n{pain}\n\n"
                        f"**💰 商业信号**\n{signal}"
                    )
                    group_blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": notion_client._parse_rich_text(intent_text)}})
                else:
                    group_blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": notion_client._parse_rich_text("**💎 商业潜力**\n暂未识别显著商业信号")}})
                
                group_blocks.append(create_callout_block(f"高手背书: `{repo_counts.get(gem['id'], 2)}` 人关注", emoji="✨", color="purple_background"))
                group_blocks.append(create_toggle_block("🔍 为什么值得关注？", notion_client.markdown_to_blocks(summary)))
                group_blocks.append({"object": "block", "type": "divider", "divider": {}})
            
            blocks.append(create_toggle_block(group_title, group_blocks))
    else:
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "今日暂未发现显著的新项目关联信号。"}, "annotations": {"italic": True}}]}})


    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (CST)"}, "annotations": {"color": "gray"}}]}})

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

