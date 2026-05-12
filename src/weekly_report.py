import logging
import asyncio
import re
from datetime import datetime
from .config import load_config
from .database import db_manager
from .graph_analyzer import GraphAnalyzer
from .crawler_zai import repo_analyzer
from .notion_client import notion_client
from .daily_report import sanitize_ai_summary, create_toggle_block, create_callout_block, clean_summary_for_table, get_zread_link
from .llm_client import LLMClient
from .user_analyzer import user_analyzer

logger = logging.getLogger(__name__)

async def analyze_all_tracks_with_llm(tracks_list):
    """
    使用 LLM 综合分析所有的赛道内容，生成全局洞察和各个赛道的名称与对比洞察。
    """
    settings = load_config()
    conf = settings.get("report_llm", {})
    if not conf.get("api_key"): 
        return "暂无深度洞察。", [{"id": str(t['id']), "name": f"赛道 #{t['id']}", "insight": "自动分析失败。"} for t in tracks_list]

    llm_client = LLMClient(
        api_key=conf.get("api_key"),
        base_url=conf.get("base_url"),
        model_names=conf.get("model_names")
    )

    # 准备上下文
    context_lines = []
    for track in tracks_list:
        context_lines.append(f"### 赛道 ID: {track['id']} (规模: {track['count']} 个项目关联)")
        for r in track['repos'][:15]:
            context_lines.append(f"- {r['full_name']}: {r.get('description', '')} (Topics: {r.get('topics', [])})")
        context_lines.append("")
    
    context = "\n".join(context_lines)
    
    system_prompt = (
        "你是一位顶级技术分析师。我会给你 5 个在 GitHub 关联网络中高度聚类的技术赛道及其头部项目。\n"
        "请执行以下任务：\n"
        "1. 对比这 5 个赛道，产出一份全局的技术趋势或范式迁移洞察。说明当前开源圈的核心注意力流向。\n"
        "2. 为每个赛道起一个精准、响亮的中文名称。\n"
        "3. 为每个赛道产出一份深度洞察，说明该赛道的独特价值、解决的问题或商业机会。\n"
        "请严格按以下 JSON 格式输出，不要包含多余文本：\n"
        '{\n'
        '  "global_insight": "全局洞察内容",\n'
        '  "tracks": [\n'
        '    {"id": "原始赛道ID", "name": "赛道名称", "insight": "赛道洞察"}\n'
        '  ]\n'
        '}'
    )

    try:
        res = await llm_client.chat(system_prompt=system_prompt, user_prompt=context, temperature=0.3)
        if res:
            import json
            if "```json" in res:
                res = re.search(r'```json\s*(.*?)\s*```', res, re.DOTALL).group(1)
            data = json.loads(res)
            return data.get("global_insight", "分析完成。"), data.get("tracks", [])
    except Exception as e:
        logger.error(f"LLM 综合分析赛道失败: {e}")
    
    return "自动分析失败。", [{"id": str(t['id']), "name": f"赛道 #{t['id']}", "insight": "自动分析失败。"} for t in tracks_list]

async def generate_weekly_comprehensive_insight(rising_stars, gems, tracks, user_bursts):
    """
    聚合全周所有核心信号，产出跨维度的深度技术与商业综述。
    """
    settings = load_config()
    conf = settings.get("report_llm", {})
    if not conf.get("api_key"): return "暂无每周深度总结。"

    llm_client = LLMClient(
        api_key=conf.get("api_key"),
        base_url=conf.get("base_url"),
        model_names=conf.get("model_names")
    )

    # --- 构建庞大的上下文 ---
    context_sections = []
    context_sections.append("## [信号 1] 本周增长黑马 (Top 15 Rising Stars)")
    for r in rising_stars[:15]:
        context_sections.append(f"- {r['full_name']}: {r.get('description', '')} (增速: {r.get('star_velocity_24h')} stars/24h)")
    context_sections.append("\n## [信号 2] 图算法挖掘的隐藏宝藏 (PageRank Hidden Gems)")
    for g in gems[:15]:
        context_sections.append(f"- {g['full_name']}: {g.get('description', '')} (PR能量值: {g['pr_score']:.6f})")
    context_sections.append("\n## [信号 3] 技术赛道全景 (Sector Clusters)")
    for t in tracks:
        context_sections.append(f"### 赛道 {t['id']} (规模: {t['count']}个项目)")
        for r in t['repos'][:10]:
            context_sections.append(f"  - {r['full_name']}: {r.get('description', '')}")
    if user_bursts:
        context_sections.append("\n## [信号 4] 核心开发者活跃度爆发 (Key Person Bursts)")
        for u in user_bursts[:8]:
            context_sections.append(f"- {u['login']} 活跃度较历史均值增长 {u['ratio']:.1f} 倍")

    context = "\n".join(context_sections)

    system_prompt = (
        "你是一位顶级的开源生态战略顾问和技术架构专家。\n"
        "我会为你提供本周 GitHub 关联网络挖掘出的全维度原始信号（含增长速度、图能量值、社区聚类和开发者异动）。\n"
        "请撰写一份《GitHub 周度全球技术范式迁移深度报告》，要求包含以下四大核心板块（请使用优美的 Markdown 排版）：\n\n"
        "1. **🌍 全球开源注意力宏观流向**：分析当前开源社区的资源和开发精力正在从哪些传统领域撤出，正疯狂涌向哪些新高地。给出至少 3 个核心趋势洞察。\n"
        "2. **🔄 范式分化与技术迁移观察**：观察不同赛道之间的关联或竞争。例如：是否存在某种技术栈（如 Rust, Mojo）对旧生态的“大重构”？是否存在从单点工具向系统级方案的整合？\n"
        "3. **💎 隐藏Alpha与冷门洞察**：分析那些 PageRank 高但 Star 数低的项目，解释为什么顶级开发者在秘密关注它们，预示了什么未来 3-6 个月的技术爆发点。\n"
        "4. **💰 商业机会与个人行动建议**：基于上述信号，识别出 2 个最具商业化潜力（SaaS化或工具化）的方向，并为想要建立影响力的开发者提供一个精准的“入局切入点”。\n\n"
        "文字要求：犀利、深刻、专业、具备商业前瞻性，总字数 1200 字左右。"
    )

    insight = await llm_client.chat(system_prompt=system_prompt, user_prompt=context, temperature=0.6)
    return insight or "每周洞察生成失败。"

async def generate_weekly_report_blocks():
    """
    生成每周深度挖掘报告 (Weekly Deep Dive)。
    """
    logger.info("正在生成每周深度挖掘报告...")
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    analyzer = GraphAnalyzer()
    analyzer.build_network(limit_days=30)
    
    # 1. 运行图算法
    pr_results = analyzer.run_personalized_pagerank()
    communities = analyzer.detect_communities()
    analyzer.store_results(pr_results, communities)
    
    # 2. 准备核心数据
    from .velocity_calc import get_rising_stars
    rising_stars = get_rising_stars(limit=20)
    user_bursts = user_analyzer.analyze_kp_activity_bursts()
    gems = analyzer.get_hidden_gems(top_n=20)
    
    # 整理赛道数据
    track_map = {}
    for r_node, c_id in communities.items():
        if c_id not in track_map: track_map[c_id] = []
        track_map[c_id].append(r_node.replace('r_', ''))
    
    # 按规模排序并取前 5 大赛道
    sorted_tracks = sorted(track_map.items(), key=lambda x: len(x[1]), reverse=True)[:5]

    tracks_for_llm = []
    for c_id, repo_ids in sorted_tracks:
        id_str = ",".join(repo_ids[:15])
        repos_data = db_manager.execute_query(f"SELECT full_name, description, topics FROM repos WHERE id IN ({id_str})", db_type="source")
        tracks_for_llm.append({
            "id": str(c_id),
            "count": len(repo_ids),
            "repos": repos_data
        })

    # 获取全周 Top 50
    top_50_repos = db_manager.execute_query(
        "SELECT full_name, description, stargazers_count, influence_score FROM repos WHERE influence_score > 0 ORDER BY influence_score DESC, stargazers_count DESC LIMIT 50",
        db_type="source"
    )

    # 3. 并发调用 AI 解析引擎 (确保周报项目也有 zread/LLM 深度内容)
    all_repos_to_analyze = list(dict.fromkeys(
        [g['full_name'] for g in gems[:15]] + 
        [r['full_name'] for t in tracks_for_llm for r in t['repos'][:10]] +
        [r['full_name'] for r in top_50_repos]
    ))
    logger.info(f"正在为周报中的 {len(all_repos_to_analyze)} 个项目启动深度解析引擎...")
    ai_summaries = await repo_analyzer.analyze_batch(all_repos_to_analyze)

    # 4. 生成【重头戏】：每周综合深度洞察
    logger.info("正在调用 LLM 进行全维度周度宏观综述分析...")
    macro_report = await generate_weekly_comprehensive_insight(rising_stars, gems, tracks_for_llm, user_bursts)
    
    # 同时生成赛道名称
    _, analyzed_tracks_info = await analyze_all_tracks_with_llm(tracks_for_llm)
    info_map = {str(t["id"]): t for t in analyzed_tracks_info}
    
    analyzed_tracks = []
    for t in tracks_for_llm:
        t_info = info_map.get(str(t['id']), {})
        analyzed_tracks.append({
            "id": t['id'],
            "count": t['count'],
            "repos": t['repos'],
            "name": t_info.get("name", f"赛道 #{t['id']}"),
            "insight": t_info.get("insight", "暂无深度洞察。")
        })

    # 5. 构建 Blocks
    blocks = []
    blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": [{"type": "text", "text": {"content": f"💎 GitHub Weekly Strategy & Alpha | {today_str}"}}]}})
    
    # 渲染宏观报告
    macro_blocks = notion_client.markdown_to_blocks(macro_report)
    blocks.append(create_callout_block("本周全球技术范式迁移深度报告", emoji="🔮", color="blue_background", children=macro_blocks))
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # --- 第一部分: 扫地僧项目 ---
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🕵️‍♂️ 能量中心：PageRank 隐藏宝藏 (扫地僧)"}, "annotations": {"color": "orange", "bold": True}}]}})
    
    gem_blocks = []
    for i, gem in enumerate(gems[:15], 1):
        fn = gem['full_name']
        summary = sanitize_ai_summary(ai_summaries.get(fn, gem['description'] or "No description"))
        gem_blocks.append(create_callout_block(f"**{i}. {fn}**", emoji="💎", color="orange_background"))
        gem_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": notion_client._parse_rich_text(f"PR 能量值: `{gem['pr_score']:.6f}` | Stars: `{gem['stars']}`\n[🔗 GitHub](https://github.com/{fn}) | [📖 zread](https://zread.ai/{fn})")}} )
        gem_blocks.append(create_toggle_block("🔍 深度解析", notion_client.markdown_to_blocks(summary)))
        gem_blocks.append({"object": "block", "type": "divider", "divider": {}})

    blocks.append(create_toggle_block("📦 查看被大牛们秘密关注的 15 个隐藏项目", gem_blocks))

    # --- 第二部分: 技术赛道全景 ---
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🚦 技术赛道全景 (Sector Analysis)"}, "annotations": {"color": "purple", "bold": True}}]}})
    
    for track in analyzed_tracks:
        track_blocks = []
        track_blocks.append(create_callout_block(track['insight'], emoji="💡", color="blue_background"))
        
        repo_md_list = []
        for r in track['repos'][:10]:
            fn = r['full_name']
            z_link = get_zread_link(fn)
            summary = sanitize_ai_summary(ai_summaries.get(fn, r.get('description') or "No description"))
            short_desc = clean_summary_for_table(summary)
            repo_md_list.append(f"- [**{fn}**](https://github.com/{fn}) ([📖 zread]({z_link})): {short_desc}")
        
        repo_list_blocks = notion_client.markdown_to_blocks("\n".join(repo_md_list))
        track_blocks.extend(repo_list_blocks)
        blocks.append(create_toggle_block(f"🏷️ {track['name']} ({track['count']} 个项目关联)", track_blocks))

    # --- 第三部分: Weekly Top 50 ---
    blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🏆 本周影响力之星 Top 50"}, "annotations": {"color": "blue", "bold": True}}]}})
    
    group_size = 10
    for g in range(0, len(top_50_repos), group_size):
        group_repos = top_50_repos[g:g+group_size]
        group_title = f"🌟 影响力项目排行 #{g+1} - #{g+len(group_repos)}"
        group_blocks = []
        for i, repo in enumerate(group_repos, g + 1):
            fn = repo['full_name']
            z_link = get_zread_link(fn)
            summary = sanitize_ai_summary(ai_summaries.get(fn, repo['description'] or "No description"))
            
            group_blocks.append({
                "object": "block", "type": "paragraph", 
                "paragraph": {"rich_text": notion_client._parse_rich_text(f"**{i}. {fn}** [🔗 GitHub](https://github.com/{fn}) | [📖 zread]({z_link})")}
            })
            group_blocks.append({
                "object": "block", "type": "quote", 
                "quote": {"rich_text": notion_client._parse_rich_text(clean_summary_for_table(summary))}
            })
        
        blocks.append(create_toggle_block(group_title, group_blocks))

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"深度挖掘生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (CST)"}, "annotations": {"color": "gray"}}]}})

    return blocks

def generate_weekly_report():
    blocks = asyncio.run(generate_weekly_report_blocks())
    success = notion_client.push_blocks(blocks, "GitHub每周深度情报")
    return "Weekly Deep Dive report generated and pushed." if success else "Failed."

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(generate_weekly_report())
