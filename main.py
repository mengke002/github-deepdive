import argparse
import logging
import asyncio
from src.logger import setup_logger
from src.tasks import (
    run_daily_alpha, 
    run_weekly_deep_dive, 
    DailyDiscovery, 
    calculate_velocity_scores, 
    run_intent_analysis
)

def main():
    setup_logger()
    parser = argparse.ArgumentParser(description="GitHub Deep Dive Analysis & Mining System")
    parser.add_argument(
        "--task", 
        required=True,
        help="Task to run (e.g., daily_alpha, weekly_deepdive, daily_sync, fetch_trending, etc.)"
    )
    
    args = parser.parse_args()
    
    logger = logging.getLogger(__name__)
    logger.info(f"Executing task: {args.task}")

    task_map = {
        "daily_alpha": run_daily_alpha,
        "daily_sync": run_daily_alpha,
        "weekly_deepdive": run_weekly_deep_dive,
        "weekly_graph_build": run_weekly_deep_dive,
        "fetch_trending": lambda: DailyDiscovery().run(),
        "update_metrics": calculate_velocity_scores,
        "intent_analysis": run_intent_analysis,
    }
    
    if args.task == "generate_report":
        # 默认生成每日报告，如果以后有每周报告参数可以再加
        from src.daily_report import generate_daily_report
        generate_daily_report()
    elif args.task in task_map:
        task_map[args.task]()
    else:
        logger.error(f"Unknown task: {args.task}")
        exit(1)

if __name__ == "__main__":
    main()
