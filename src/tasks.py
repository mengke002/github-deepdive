import asyncio
import logging
from .daily_discovery import DailyDiscovery
from .velocity_calc import calculate_velocity_scores, get_rising_stars
from .seed_expansion import run_pilot_l2_expansion
from .daily_report import generate_daily_report
from .notion_client import notion_client
from .key_person_discovery import discover_key_persons
from .intent_detector import intent_detector

logger = logging.getLogger(__name__)

async def run_intent_analysis_async():
    """异步执行意图分析任务"""
    # 获取需要深入分析的项目：Rising Stars 前 5 名和所有 Super Seeds
    rising = get_rising_stars(limit=5)
    
    from .database import db_manager
    super_seeds = db_manager.execute_query("SELECT full_name FROM repos WHERE super_seed = 1 LIMIT 10", db_type="source")
    
    targets = list(set([r['full_name'] for r in rising] + [s['full_name'] for s in super_seeds]))
    
    if targets:
        logger.info(f"正在对 {len(targets)} 个核心项目进行深度意图分析...")
        await intent_detector.analyze_intent_batch(targets)

def run_intent_analysis():
    asyncio.run(run_intent_analysis_async())

def run_daily_alpha():
    """
    每日 Alpha 快轨工作流：发现 -> 扩展 -> 评分 -> 意图分析 -> 报告
    """
    logger.info("=== 开始运行每日 Alpha 工作流 ===")
    
    # 1. 自动挖掘新的 Key Person
    discover_key_persons()
    
    # 2. 核心发现：抓取 Trending 和监控大牛 Star 动态
    discovery = DailyDiscovery()
    discovery.run()
    
    # 3. 关联扩展：进行 Layer 2 BFS
    run_pilot_l2_expansion()
    
    # 4. 速度评分
    calculate_velocity_scores()
    
    # 5. 深度意图挖掘
    run_intent_analysis()
    
    # 6. 生成报告
    report_md = generate_daily_report(rising_limit=15, hot_limit=50, hidden_limit=10)
    
    # 7. 推送至 Notion
    if report_md:
        logger.info("每日 Alpha 工作流执行完毕。")
    
    return report_md

def run_weekly_deep_dive():
    """
    每周深度慢轨工作流
    """
    logger.info("=== 开始运行每周深度挖掘工作流 ===")
    
    # 1. 运行图算法分析 (PageRank 识别冷门种子, Louvain 划分赛道)
    from .graph_analyzer import GraphAnalyzer
    analyzer = GraphAnalyzer()
    analyzer.build_network(limit_days=60) # 每周扩展视野到 60 天
    
    pr_results = analyzer.run_personalized_pagerank()
    communities = analyzer.detect_communities()
    analyzer.store_results(pr_results, communities)
    
    # 2. 这里的 BFS 扩展逻辑可以集成 seed_expansion.py 的增强版
    # 目前先使用已有的关系库数据进行图挖掘
    
    # 3. 生成每周深度报告
    from .weekly_report import generate_weekly_report
    res = generate_weekly_report()
    
    logger.info(f"每周深度挖掘工作流完成: {res}")
    return res
