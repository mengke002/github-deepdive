import logging
import pandas as pd
import asyncio
from datetime import datetime, timedelta
from .database import db_manager
from .bigquery_client import bigquery_client
from .github_client import github_client

logger = logging.getLogger(__name__)

class KeyPersonDiscovery:
    """
    自动从热门项目中识别高价值开发者，构建自进化的影响力人物池。
    """

    async def run_discovery_async(self, min_followers=500):
        logger.info("开始自动挖掘 Key Person...")
        
        # 1. 获取 Star 数前 50 的热门/榜单项目
        repos = db_manager.execute_query(
            "SELECT id, full_name FROM repos WHERE stargazers_count > 1000 ORDER BY stargazers_count DESC LIMIT 50",
            db_type="source"
        )
        if not repos:
            logger.info("未找到足够的热门项目用于挖掘。")
            return
        
        repo_ids = [r['id'] for r in repos]
        
        # 2. 从 BigQuery 获取过去 6 个月的核心贡献者
        now = datetime.now()
        months = [(now - timedelta(days=30*i)).strftime("%Y%m") for i in range(6)]
        
        df_contributors = bigquery_client.get_core_contributors_for_seeds(repo_ids, months=months)
        if df_contributors.empty:
            logger.warning("在热门项目中未发现贡献者。")
            return
            
        # 3. 筛选并并发验证候选人
        # 排除已是 KP 的用户 (一次性批量查询 DB)
        existing_kp_rows = db_manager.execute_query("SELECT login FROM users WHERE is_key_person = 1", db_type="source")
        existing_kp_set = {r['login'] for r in existing_kp_rows} if existing_kp_rows else set()

        potential_kp_list = [u for u in potential_kp if u not in existing_kp_set][:500]
        
        logger.info(f"从热门项目贡献者中发现 {total_potential} 位候选人，正在并发验证前 {len(potential_kp_list)} 位...")
        
        sem = asyncio.Semaphore(15) # 挖掘阶段可以稍高，因为只是单次 GET 请求

        async def _validate_potential_kp(login):
            async with sem:
                user_data = await asyncio.to_thread(github_client.request, "GET", f"https://api.github.com/users/{login}")
                if not user_data: return None
                
                followers = user_data.get("followers", 0)
                if followers >= min_followers:
                    return {
                        "id": user_data["id"],
                        "login": user_data["login"],
                        "name": user_data.get("name"),
                        "company": user_data.get("company"),
                        "bio": user_data.get("bio"),
                        "followers": followers
                    }
                return None

        tasks = [_validate_potential_kp(login) for login in potential_kp_list]
        results = await asyncio.gather(*tasks)
        
        new_kp_records = [r for r in results if r]
        
        if new_kp_records:
            sql = """
            INSERT INTO users (id, login, name, company, bio, followers, is_key_person)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
            ON DUPLICATE KEY UPDATE is_key_person = 1, followers = VALUES(followers)
            """
            params = [(r['id'], r['login'], r['name'], r['company'], r['bio'], r['followers']) for r in new_kp_records]
            db_manager.execute_batch(sql, params, db_type="source")
            for r in new_kp_records:
                logger.info(f"发现新 Key Person: {r['login']} ({r['followers']} 粉丝)")
        
        logger.info(f"Key Person 挖掘完成。新增了 {len(new_kp_records)} 位大牛。")

    def run_discovery(self, min_followers=500):
        import asyncio
        asyncio.run(self.run_discovery_async(min_followers))


def discover_key_persons():
    kp_discovery = KeyPersonDiscovery()
    kp_discovery.run_discovery()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    discover_key_persons()
