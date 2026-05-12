import logging
import pandas as pd
from .bigquery_client import bigquery_client
from .database import db_manager
from .github_client import github_client

logger = logging.getLogger(__name__)

class SeedExpansion:
    """
    负责利用 BigQuery 和 GitHub API 进行 BFS（广度优先搜索）网络扩展，
    以填充关系数据库。
    """
    
    def expand_layer_1(self, limit_per_repo=30):
        """
        第一层扩展：为所有种子仓库寻找核心贡献者。
        """
        logger.info("正在使用 BigQuery 扩展 BFS 第一层（贡献者）...")
        
        # 1. 从源数据库获取种子仓库
        seeds = db_manager.execute_query(
            "SELECT id, full_name FROM repos WHERE is_seed = TRUE", 
            db_type="source"
        )
        if not seeds:
            logger.warning("数据库中未找到种子仓库。")
            return
            
        seed_ids = [s['id'] for s in seeds]
        
        # 2. 查询 BigQuery 获取核心贡献者
        df_l1 = bigquery_client.get_core_contributors_for_seeds(seed_ids)
        if df_l1.empty:
            logger.warning("BigQuery 未返回任何种子仓库的贡献者。")
            return
            
        logger.info(f"通过 BQ 发现了 {len(df_l1)} 条‘贡献者-仓库’关系。")
        
        # 3. 将关系同步到 TiDB
        self._sync_relations_to_db(df_l1)

    def expand_layer_2(self, limit_per_user=10):
        """
        第二层扩展：寻找核心贡献者活跃的其他项目。
        """
        logger.info("正在使用 BigQuery 扩展 BFS 第二层（关联仓库）...")
        
        # 1. 从关系库获取活跃贡献者的 ID
        u_ids = db_manager.execute_query(
            "SELECT DISTINCT user_id FROM repo_user_relations WHERE relation_type = 'CONTRIBUTOR' LIMIT 1000",
            db_type="relation"
        )
        if not u_ids:
            logger.warning("关系数据库中未找到贡献者。")
            return

        user_id_list = [str(r['user_id']) for r in u_ids]
        
        # 2. 从源数据库获取他们的登录名
        contributors = db_manager.execute_query(
            f"SELECT login FROM users WHERE id IN ({', '.join(user_id_list)})",
            db_type="source"
        )
        if not contributors:
            return

        user_logins = [c['login'] for c in contributors]
        
        # 3. 获取已有的仓库列表以进行排除
        seeds = db_manager.execute_query("SELECT full_name FROM repos", db_type="source")
        existing_repos = [s['full_name'] for s in seeds]

        # 4. 查询 BQ 获取他们参与的其他项目
        df_l2 = bigquery_client.discover_related_repos_by_users(user_logins, exclude_repos=existing_repos, limit_per_user=limit_per_user)
        if df_l2.empty:
            logger.info("未通过 BQ 发现新的关联仓库。")
            return
            
        logger.info(f"通过 BQ 发现了 {len(df_l2)} 条新的关联关系。")
        
        # 5. 同步并入库
        self._sync_relations_to_db(df_l2)

    def _sync_relations_to_db(self, df):
        """
        确保仓库和用户存在于源数据库中，然后记录它们的关系。
        直接使用 BigQuery 返回的 ID，避免调用 GitHub API。
        """
        if df.empty:
            return

        # 1. 同步仓库到源数据库 (如果缺失)
        unique_repos = df[['repo_id', 'repo_name']].drop_duplicates()
        repo_records = [(row['repo_id'], row['repo_name']) for _, row in unique_repos.iterrows()]
        if repo_records:
            repo_sql = "INSERT IGNORE INTO repos (id, full_name) VALUES (%s, %s)"
            db_manager.execute_batch(repo_sql, repo_records, db_type="source")
            logger.info(f"从 BQ 同步了 {len(repo_records)} 个唯一仓库到源数据库。")

        # 2. 同步用户到源数据库 (如果缺失)
        unique_users = df[['user_id', 'user_login']].drop_duplicates()
        user_records = [(row['user_id'], row['user_login'], 'User') for _, row in unique_users.iterrows()]
        if user_records:
            user_sql = "INSERT IGNORE INTO users (id, login, type) VALUES (%s, %s, %s)"
            db_manager.execute_batch(user_sql, user_records, db_type="source")
            logger.info(f"从 BQ 同步了 {len(user_records)} 个用户到源数据库。")

        # 3. 同步关系到关系数据库
        relation_records = [
            (row['repo_id'], row['user_id'], 'CONTRIBUTOR', 5.0, row.get('activity_count', row.get('contribution_count', 0)))
            for _, row in df.iterrows()
        ]

        if relation_records:
            rel_sql = """
            INSERT INTO repo_user_relations (repo_id, user_id, relation_type, weight, contributions_count)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                contributions_count = VALUES(contributions_count),
                weight = VALUES(weight)
            """
            chunk_size = 5000
            for i in range(0, len(relation_records), chunk_size):
                chunk = relation_records[i:i+chunk_size]
                db_manager.execute_batch(rel_sql, chunk, db_type="relation")
            
            logger.info(f"在关系数据库中存储了 {len(relation_records)} 条关系。")

def run_full_l1_expansion():
    """
    为所有种子执行全量第一层扩展。
    """
    expansion = SeedExpansion()
    seeds = db_manager.execute_query("SELECT id FROM repos WHERE is_seed=TRUE", db_type="source")
    seed_ids = [s['id'] for s in seeds]
    
    if not seed_ids:
        logger.warning("未找到可供扩展的种子。")
        return

    # 构造月份列表
    now = datetime.now()
    months = [(now - timedelta(days=30*i)).strftime("%Y%m") for i in range(12)]
    
    logger.info(f"开始为 {len(seed_ids)} 个种子执行全量 L1 扩展，回溯周期为 {len(months)} 个月...")
    
    chunk_size = 500
    total_relations = 0
    for i in range(0, len(seed_ids), chunk_size):
        chunk = seed_ids[i:i+chunk_size]
        df = bigquery_client.get_core_contributors_for_seeds(chunk, months=months)
        if not df.empty:
            expansion._sync_relations_to_db(df)
            total_relations += len(df)
            
    logger.info(f"全量 L1 扩展完成。共发现 {total_relations} 条关系。")

def run_pilot_l2_expansion():
    """
    第二层扩展的试点运行。
    """
    expansion = SeedExpansion()
    expansion.expand_layer_2(limit_per_user=5)
