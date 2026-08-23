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
        第一层扩展：通过项目 ID 和月份过滤获取核心贡献者。
        采用按月迭代分片查询，确保在 BigQuery Sandbox 沙盒模式下永不超限。
        """
        if not repo_ids:
            return pd.DataFrame()
            
        repo_list_str = ", ".join([str(rid) for rid in repo_ids])
        
        if not months:
            now = datetime.now()
            months = [(now - timedelta(days=30*i)).strftime("%Y%m") for i in range(2)]
            
        dfs = []
        for m in months:
            sql = f"""
            SELECT 
                repo.id as repo_id,
                repo.name as repo_name,
                actor.id as user_id,
                actor.login as user_login,
                COUNT(*) as activity_count
            FROM `githubarchive.month.{m}`
            WHERE repo.id IN ({repo_list_str})
              AND type IN ('PullRequestEvent', 'PushEvent', 'IssuesEvent')
              AND actor.login NOT LIKE '%bot%'
            GROUP BY repo_id, repo_name, user_id, user_login
            QUALIFY ROW_NUMBER() OVER(PARTITION BY repo_id ORDER BY activity_count DESC) <= 30
            """
            df_m = self.query(sql)
            if not df_m.empty:
                dfs.append(df_m)

        if not dfs:
            return pd.DataFrame()
            
        combined = pd.concat(dfs, ignore_index=True)
        agg = combined.groupby(['repo_id', 'repo_name', 'user_id', 'user_login'], as_index=False)['activity_count'].sum()
        return agg.sort_values(by=['repo_id', 'activity_count'], ascending=[True, False])

    def discover_related_repos_by_users(self, user_logins, exclude_repos=None, limit_per_user=10, months=None):
        """
        第二层扩展：寻找种子贡献者参与过的其他项目。
        采用按月分片迭代查询，彻底规避 BigQuery 沙盒模式单次查询体积限制。
        """
        if not user_logins:
            return pd.DataFrame()
            
        if not months:
            now = datetime.now()
            months = [(now - timedelta(days=30*i)).strftime("%Y%m") for i in range(2)]
            
        user_list_str = ", ".join([f"'{login}'" for login in user_logins])
        
        exclude_clause = ""
        if exclude_repos:
            exclude_list_str = ", ".join([f"'{name}'" for name in exclude_repos[:2000]])
            exclude_clause = f"AND repo.name NOT IN ({exclude_list_str})"

        dfs = []
        for m in months:
            sql = f"""
            SELECT 
                actor.id as user_id,
                actor.login as user_login,
                repo.id as repo_id,
                repo.name as repo_name,
                COUNT(*) as contribution_count
            FROM `githubarchive.month.{m}`
            WHERE actor.login IN ({user_list_str})
              AND type IN ('PullRequestEvent', 'PushEvent', 'IssuesEvent')
              {exclude_clause}
            GROUP BY user_id, user_login, repo_id, repo_name
            QUALIFY ROW_NUMBER() OVER(PARTITION BY user_id ORDER BY contribution_count DESC) <= {limit_per_user}
            """
            df_m = self.query(sql)
            if not df_m.empty:
                dfs.append(df_m)

        if not dfs:
            return pd.DataFrame()
            
        combined = pd.concat(dfs, ignore_index=True)
        agg = combined.groupby(['user_id', 'user_login', 'repo_id', 'repo_name'], as_index=False)['contribution_count'].sum()
        return agg.sort_values(by=['user_id', 'contribution_count'], ascending=[True, False])


    def test_connection(self):
        """测试连接"""
        result = self.query("SELECT 1 AS ok")
        if result is None or result.empty:
            return None
        return result.to_dict(orient="records")

bigquery_client = BigQueryClient()
