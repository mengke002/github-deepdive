import asyncio
import logging
import re
from datetime import datetime
from crawl4ai import AsyncWebCrawler
from .github_client import github_client
from .database import db_manager
from .seed_collector import parse_markdown_table, get_repo_metadata

logger = logging.getLogger(__name__)

class DailyDiscovery:
    def run(self):
        """
        每日发现主入口：合并 Top100、Trending 和 Key Person 动态
        """
        logger.info("正在运行每日发现工作流...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 1. 抓取 GitHub Trending
            trending_repos = loop.run_until_complete(self.fetch_trending())
            # 2. 抓取 EvanLi Top100 (最新版本)
            top100_repos = self.fetch_top100_latest()
            # 3. 并发处理 Key Person 相关逻辑
            kp_starred_repos = loop.run_until_complete(self.process_key_persons())
            
            # 4. 合并去重并取前 200 个
            merged_list = list(dict.fromkeys(trending_repos + top100_repos + kp_starred_repos))[:200]
            logger.info(f"合并后的发现池大小: {len(merged_list)} 个仓库。")
            
            # 5. 存入数据库并标记来源
            self._store_discovery_results(merged_list, trending_repos, top100_repos, kp_starred_repos)
            
            # 6. 根据大牛背书情况更新 super_seed 状态
            self.mark_super_seeds()
            
        finally:
            loop.close()

    async def process_key_persons(self):
        """并发处理所有 Key Person 的动态监控、元数据同步和社交关系"""
        key_persons = db_manager.execute_query(
            "SELECT id, login FROM users WHERE is_key_person = 1", 
            db_type="source"
        )
        if not key_persons:
            logger.warning("数据库中未定义 Key Person。")
            return []

        # 限制并发请求数，防止触发 GitHub 的二级速率限制 (Abuse Detection)
        sem = asyncio.Semaphore(10)

        async def _process_single_kp(kp):
            async with sem:
                # 1. 获取 Star 动态
                starred = await asyncio.to_thread(github_client.get_user_starred, kp['login'], per_page=30)
                # 2. 获取元数据（用于快照）
                user_data = await asyncio.to_thread(github_client.request, "GET", f"https://api.github.com/users/{kp['login']}")
                # 3. 获取关注列表
                following = await asyncio.to_thread(github_client.get_user_following, kp['login'], per_page=100)
                return {
                    "kp_id": kp['id'],
                    "starred": starred,
                    "user_data": user_data,
                    "following": following
                }

        logger.info(f"正在并发处理 {len(key_persons)} 位 Key Person 的动态...")
        tasks = [_process_single_kp(kp) for kp in key_persons]
        results = await asyncio.gather(*tasks)

        all_kp_stars = []
        star_relations = []
        user_updates = []
        snapshots = []
        following_users = []
        following_relations = []
        today = datetime.now().strftime("%Y-%m-%d")

        for res in results:
            uid = res["kp_id"]
            
            # 处理 Star
            if res["starred"] and isinstance(res["starred"], list):
                for repo in res["starred"]:
                    all_kp_stars.append(repo['full_name'])
                    star_relations.append((repo['id'], uid, repo['full_name']))
            
            # 处理元数据
            ud = res["user_data"]
            if ud:
                user_updates.append((
                    ud.get("name"), ud.get("company"), ud.get("blog"), ud.get("location"),
                    ud.get("email"), ud.get("hireable"), ud.get("bio"), ud.get("followers"),
                    ud.get("following"), ud.get("public_repos"), uid
                ))
                snapshots.append((
                    uid, today, ud.get("followers"), ud.get("following"),
                    ud.get("public_repos"), ud.get("hireable"), ud.get("bio")
                ))
            
            # 处理关注关系
            if res["following"] and isinstance(res["following"], list):
                for target in res["following"]:
                    following_users.append((target['id'], target['login'], target['type']))
                    following_relations.append((uid, target['id'], 'FOLLOWS', 1.5))

        # 批量写入数据库 (真正的 Batch 操作)
        if star_relations:
            self._store_kp_relations(star_relations)

        if user_updates:
            sql_user = "UPDATE users SET name=%s, company=%s, blog=%s, location=%s, email=%s, hireable=%s, bio=%s, followers=%s, following=%s, public_repos=%s WHERE id=%s"
            db_manager.execute_batch(sql_user, user_updates, db_type="source")
        
        if snapshots:
            sql_snap = "INSERT IGNORE INTO user_snapshots (user_id, snapshot_date, followers_count, following_count, public_repos, hireable, bio) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            db_manager.execute_batch(sql_snap, snapshots, db_type="insight")
            logger.info(f"已记录 {len(snapshots)} 条用户快照。")

        if following_users:
            # 去重后再插入基础信息
            unique_following_users = list({u[0]: u for u in following_users}.values())
            sql_u = "INSERT IGNORE INTO users (id, login, type) VALUES (%s, %s, %s)"
            db_manager.execute_batch(sql_u, unique_following_users, db_type="source")
            
            sql_rel = "INSERT IGNORE INTO user_user_relations (user_id, target_user_id, relation_type, weight) VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE weight=VALUES(weight)"
            db_manager.execute_batch(sql_rel, following_relations, db_type="relation")
            logger.info(f"同步了 {len(following_relations)} 条用户关注关系。")

        unique_stars = list(set(all_kp_stars))
        logger.info(f"从大牛动态中发现了 {len(unique_stars)} 个不重复的项目。")
        return unique_stars

    async def fetch_trending(self, language="python"):
        """使用 crawl4ai 稳定抓取 GitHub Trending"""
        url = f"https://github.com/trending/{language}?since=daily"
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, bypass_cache=True)
            if not result.success:
                logger.error(f"Crawl4AI 抓取失败: {result.error_message}")
                return []
            
            content = result.markdown
            # 匹配 ## [ Owner / Repo ] 这种结构
            pattern = r'##\s+\[\s*([a-zA-Z0-9\._-]+\s*/\s*[a-zA-Z0-9\._-]+)\s*\]'
            matches = re.findall(pattern, content)
            
            clean_repos = [m.replace(" ", "") for m in matches]
            logger.info(f"成功抓取 {len(clean_repos)} 个 Trending 仓库。")
            return clean_repos

    def fetch_top100_latest(self):
        """获取 EvanLi 排行榜的最顶端数据"""
        url = "https://raw.githubusercontent.com/EvanLi/Github-Ranking/master/Top100/Python.md"
        try:
            import requests
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = parse_markdown_table(resp.text)
                return [d['full_name'] for d in data]
        except Exception as e:
            logger.error(f"获取 Top100 最新数据失败: {e}")
        return []


    def detect_co_endorsements(self):
        """
        检测 24h 内是否有 ≥2 个互相 Follow 的 KP 关注了同一个冷门 Repo
        （轻量级算法实现）
        """
        logger.info("正在检测圈子协同背书信号...")
        # 此逻辑较为复杂，通常在每日报告生成前调用，或作为独立分析步骤
        # 1. 查找 24h 内获得 ≥2 个 KP Star 的项目
        # 2. 检查这些 KP 之间是否存在 FOLLOWS 关系
        # 3. 如果存在，且项目 Stargazers 不多，则标记为 Insider Cluster 信号
        pass

    def _store_kp_relations(self, relations):
        """
        将 KP Star 记录到关系表，并确保仓库基础信息存在
        """
        if not relations: return
        
        # 1. 确保 repos 表中有这些仓库的基础信息
        repo_names = list(set([r[2] for r in relations]))
        metadata_map = get_repo_metadata(repo_names)
        
        repo_records = []
        for fn, meta in metadata_map.items():
            repo_records.append((meta['id'], fn, meta['description'], meta['stargazers_count']))
        
        if repo_records:
            sql_repo = "INSERT IGNORE INTO repos (id, full_name, description, stargazers_count) VALUES (%s, %s, %s, %s)"
            db_manager.execute_batch(sql_repo, repo_records, db_type="source")

        # 2. 写入关系表
        rel_records = []
        now = datetime.now()
        for rid, uid, _ in relations:
            rel_records.append((rid, uid, 'STARGAZER', 1.0, now, now))
        
        sql_rel = """
        INSERT INTO repo_user_relations (repo_id, user_id, relation_type, weight, first_interaction_at, last_interaction_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE 
            weight=VALUES(weight),
            last_interaction_at=VALUES(last_interaction_at)
        """
        db_manager.execute_batch(sql_rel, rel_records, db_type="relation")

    def _store_discovery_results(self, all_names, trending_list, top100_list, kp_list):
        """同步仓库元数据并标记发现来源"""
        metadata_map = get_repo_metadata(all_names)
        
        records = []
        for fn, meta in metadata_map.items():
            sources = []
            if fn in trending_list: sources.append("Trending")
            if fn in top100_list: sources.append("Top100")
            if fn in kp_list: sources.append("KeyPerson")
            source_str = "/".join(sources)
            
            records.append((
                meta["id"], meta["full_name"], meta["description"], meta["language"],
                meta["stargazers_count"], meta["forks_count"], meta["open_issues_count"],
                meta["updated_at"], source_str, True
            ))

        if records:
            sql = """
            INSERT INTO repos (id, full_name, description, language, stargazers_count, forks_count, open_issues_count, updated_at, seed_source, is_seed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                stargazers_count=VALUES(stargazers_count),
                description=VALUES(description),
                seed_source=VALUES(seed_source),
                is_seed=VALUES(is_seed)
            """
            db_manager.execute_batch(sql, records, db_type="source")
            logger.info(f"同步了 {len(records)} 条发现记录到数据库。")

    def mark_super_seeds(self):
        """
        标记被 2 个以上 Key Person Star 过的非种子项目为 super_seed
        由于 source 和 relation 数据库可能在不同实例，在 Python 层进行逻辑聚合
        """
        logger.info("正在根据大牛背书情况标记超级潜力股 (super_seed)...")
        
        # 1. 获取所有 Key Person 的 ID
        kp_users = db_manager.execute_query(
            "SELECT id FROM users WHERE is_key_person = 1", 
            db_type="source"
        )
        if not kp_users:
            logger.info("未发现 Key Person，跳过标记逻辑。")
            return
            
        kp_user_ids = [str(u['id']) for u in kp_users]
        
        # 2. 从关系库查询背书情况
        # 统计每个 repo 被多少个 KP star 过
        sql_relation = f"""
            SELECT repo_id, COUNT(user_id) as kp_count
            FROM repo_user_relations
            WHERE relation_type = 'STARGAZER'
              AND user_id IN ({', '.join(kp_user_ids)})
            GROUP BY repo_id
            HAVING kp_count >= 2
        """
        results = db_manager.execute_query(sql_relation, db_type="relation")
        
        if not results:
            logger.info("今日未发现获得 2 个以上大牛背书的项目。")
            return

        # 3. 更新源库中的 super_seed 标记
        target_repo_ids = [str(r['repo_id']) for r in results]
        sql_update = f"""
            UPDATE repos 
            SET super_seed = 1 
            WHERE id IN ({', '.join(target_repo_ids)})
              AND is_seed = 0
        """
        db_manager.execute_query(sql_update, db_type="source")
        logger.info(f"成功将 {len(target_repo_ids)} 个仓库标记为超级潜力股。")

repo_discovery = DailyDiscovery()
