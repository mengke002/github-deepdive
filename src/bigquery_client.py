import os
import logging
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from datetime import datetime, timedelta
from .config import load_config

logger = logging.getLogger(__name__)

class BigQueryClient:
    """
    Google BigQuery 客户端，用于查询 GitHub Archive 历史数据。
    """
    def __init__(self):
        self.settings = load_config()
        self.credentials_path = self.settings.get("bigquery", {}).get("credentials_path", "")
        self.project_id = self.settings.get("bigquery", {}).get("project_id", "")

    def _load_credentials(self):
        """从本地文件或环境变量加载 GCP 凭据"""
        credentials_path = self.credentials_path or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if credentials_path and os.path.exists(credentials_path):
            return service_account.Credentials.from_service_account_file(credentials_path)
        return None

    def get_client(self):
        """获取 BigQuery 客户端实例"""
        credentials = self._load_credentials()
        if credentials is not None:
            return bigquery.Client(credentials=credentials, project=self.project_id or credentials.project_id)
        if self.project_id:
            return bigquery.Client(project=self.project_id)
        return bigquery.Client()

    def query(self, sql_query, dry_run=False):
        """执行 SQL 查询"""
        client = self.get_client()
        try:
            job_config = bigquery.QueryJobConfig(dry_run=dry_run, use_query_cache=True)
            query_job = client.query(sql_query, job_config=job_config)
            
            if dry_run:
                # 预估查询处理的数据量
                return query_job.total_bytes_processed
                
            rows = [dict(row) for row in query_job.result()]
            return pd.DataFrame(rows)
        except Exception as e:
            logger.error(f"BigQuery 查询错误: {e}")
            return pd.DataFrame()

    def get_core_contributors_for_seeds(self, repo_ids, months=None):
        """
        第一层扩展：通过项目 ID 和月份过滤获取核心贡献者，以节省成本。
        months: 形如 ['202601', '202602'] 的字符串列表
        """
        if not repo_ids:
            return pd.DataFrame()
            
        repo_list_str = ", ".join([str(rid) for rid in repo_ids])
        
        # 默认查询最近 3 个月，以兼顾覆盖面和成本
        if not months:
            now = datetime.now()
            months = [(now - timedelta(days=30*i)).strftime("%Y%m") for i in range(3)]
            
        month_filter = ", ".join([f"'{m}'" for m in months])
        
        sql = f"""
        SELECT 
            repo.id as repo_id,
            repo.name as repo_name,
            actor.id as user_id,
            actor.login as user_login,
            COUNT(*) as activity_count
        FROM `githubarchive.month.*`
        WHERE _TABLE_SUFFIX IN ({month_filter})
          AND repo.id IN ({repo_list_str})
          AND type IN ('PullRequestEvent', 'PushEvent', 'IssuesEvent')
          AND actor.login NOT LIKE '%bot%'
        GROUP BY repo_id, repo_name, user_id, user_login
        QUALIFY ROW_NUMBER() OVER(PARTITION BY repo_id ORDER BY activity_count DESC) <= 30
        """
        return self.query(sql)

    def discover_related_repos_by_users(self, user_logins, exclude_repos=None, limit_per_user=10, months=None):
        """
        第二层扩展：寻找种子贡献者参与过的其他项目。
        采用平衡策略：默认使用 6 个月的窗口，确保新项目覆盖的同时避免扫描过多全量数据。
        """
        if not user_logins:
            return pd.DataFrame()
            
        if not months:
            # 平衡策略：使用最近 6 个月的数据
            now = datetime.now()
            months = [(now - timedelta(days=30*i)).strftime("%Y%m") for i in range(6)]
            
        month_filter = ", ".join([f"'{m}'" for m in months])
        user_list_str = ", ".join([f"'{login}'" for login in user_logins])
        
        exclude_clause = ""
        if exclude_repos:
            # 限制排除列表长度，防止 SQL 语句过长
            exclude_list_str = ", ".join([f"'{name}'" for name in exclude_repos[:2000]])
            exclude_clause = f"AND repo.name NOT IN ({exclude_list_str})"

        sql = f"""
        SELECT 
            actor.id as user_id,
            actor.login as user_login,
            repo.id as repo_id,
            repo.name as repo_name,
            COUNT(*) as contribution_count
        FROM `githubarchive.month.*`
        WHERE _TABLE_SUFFIX IN ({month_filter})
          AND actor.login IN ({user_list_str})
          AND type IN ('PullRequestEvent', 'PushEvent', 'IssuesEvent')
          {exclude_clause}
        GROUP BY user_id, user_login, repo_id, repo_name
        QUALIFY ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY contribution_count DESC) <= {limit_per_user}
        """
        return self.query(sql)

    def test_connection(self):
        """测试连接"""
        result = self.query("SELECT 1 AS ok")
        if result is None or result.empty:
            return None
        return result.to_dict(orient="records")

bigquery_client = BigQueryClient()
