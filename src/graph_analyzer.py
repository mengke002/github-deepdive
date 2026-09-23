import networkx as nx
import pandas as pd
import logging
from .database import db_manager

logger = logging.getLogger(__name__)

class GraphAnalyzer:
    def __init__(self):
        self.G = nx.Graph()
        self.pr_results = None
        self.communities = None
        self.limit_days = 60

    def build_network(self, limit_days=60):
        """
        从 TiDB 加载关系数据，构建异构网络。
        优先加载强信号关系和近期动态。
        """
        self.limit_days = limit_days
        logger.info(f"正在从 TiDB 构建关联网络 (最近 {limit_days} 天)...")
        self.G = nx.Graph()
        self.pr_results = None
        self.communities = None

        # 1. 加载 Repo-User 关系 (Star, Contributor 等)
        repo_user_query = f"""
        SELECT repo_id, user_id, relation_type, weight 
        FROM repo_user_relations
        WHERE last_interaction_at > NOW() - INTERVAL {limit_days} DAY
        """
        df_ru = pd.DataFrame(db_manager.execute_query(repo_user_query, db_type="relation"))
        
        if not df_ru.empty:
            for _, row in df_ru.iterrows():
                u_node = f"u_{row['user_id']}"
                r_node = f"r_{row['repo_id']}"
                if self.G.has_edge(u_node, r_node):
                    self.G[u_node][r_node]['weight'] += float(row['weight'])
                else:
                    self.G.add_edge(u_node, r_node, weight=float(row['weight']), type=row['relation_type'])

        # 2. 加载 User-User 关系 (Follows)
        user_user_query = """
        SELECT user_id, target_user_id, weight FROM user_user_relations
        """
        df_uu = pd.DataFrame(db_manager.execute_query(user_user_query, db_type="relation"))
        if not df_uu.empty:
            for _, row in df_uu.iterrows():
                u1 = f"u_{row['user_id']}"
                u2 = f"u_{row['target_user_id']}"
                if self.G.has_edge(u1, u2):
                    self.G[u1][u2]['weight'] += float(row['weight'])
                else:
                    self.G.add_edge(u1, u2, weight=float(row['weight']), type='FOLLOWS')

        logger.info(f"网络构建完成: {self.G.number_of_nodes()} 节点, {self.G.number_of_edges()} 连边。")

    def run_personalized_pagerank(self, force=False):
        """
        运行 Localized PageRank。
        以 Key Persons 为源点进行能量扩散，发现“扫地僧”项目。支持结果缓存。
        """
        if self.pr_results is not None and not force:
            return self.pr_results

        if self.G.number_of_nodes() == 0:
            self.build_network(limit_days=self.limit_days)

        # 1. 获取所有 Key Person ID
        kp_res = db_manager.execute_query("SELECT id FROM users WHERE is_key_person = 1", db_type="source")
        kp_nodes = [f"u_{r['id']}" for r in kp_res if f"u_{r['id']}" in self.G]
        
        if not kp_nodes:
            logger.warning("图中未发现 Key Person 节点，无法运行 PageRank。")
            self.pr_results = []
            return []

        # 2. 构造 Personalization 向量
        personalization = {node: 1.0 / len(kp_nodes) for node in kp_nodes}
        
        logger.info(f"正在以 {len(kp_nodes)} 位 Key Person 为源点运行 PageRank...")
        pr_scores = nx.pagerank(self.G, alpha=0.85, personalization=personalization, weight='weight')
        
        # 3. 过滤并排序 Repo 节点
        repo_scores = {node: score for node, score in pr_scores.items() if node.startswith('r_')}
        self.pr_results = sorted(repo_scores.items(), key=lambda x: x[1], reverse=True)
        return self.pr_results

    def detect_communities(self, force=False, max_neighbors_per_user=60):
        """
        利用 Louvain 算法在 Repo-Repo 网络上进行社区发现（赛道聚类）。
        包含邻居规模截断，防止海量关注用户引发边组合爆炸。
        """
        if self.communities is not None and not force:
            return self.communities

        if self.G.number_of_nodes() == 0:
            self.build_network(limit_days=self.limit_days)

        logger.info("正在生成 Repo-Repo 共现网络并运行社区发现...")
        
        # 1. 构建 Repo-Repo 投影图
        from itertools import combinations
        repo_graph = nx.Graph()
        
        for node in self.G.nodes():
            if node.startswith('u_'):
                neighbors = [n for n in self.G.neighbors(node) if n.startswith('r_')]
                if len(neighbors) > 1:
                    # 避免单个高活跃账号组合爆炸，限制单用户最大关联邻居数
                    if len(neighbors) > max_neighbors_per_user:
                        neighbors = sorted(
                            neighbors, 
                            key=lambda r: self.G[node][r].get('weight', 1.0), 
                            reverse=True
                        )[:max_neighbors_per_user]

                    for r1, r2 in combinations(neighbors, 2):
                        if repo_graph.has_edge(r1, r2):
                            repo_graph[r1][r2]['weight'] += 1
                        else:
                            repo_graph.add_edge(r1, r2, weight=1)

        if repo_graph.number_of_edges() == 0:
            self.communities = {}
            return {}

        # 2. 如果边数过多，自适应剪除只有 1 次弱偶发共现的边，提高聚类紧凑度
        if repo_graph.number_of_edges() > 40000:
            weak_edges = [(u, v) for u, v, d in repo_graph.edges(data=True) if d.get('weight', 1) < 2]
            repo_graph.remove_edges_from(weak_edges)

        # 3. 运行 Louvain 算法
        from networkx.algorithms.community import louvain_communities
        communities = louvain_communities(repo_graph, weight='weight', seed=42)
        
        repo_to_community = {}
        for idx, community in enumerate(communities):
            for node in community:
                repo_to_community[node] = idx
                
        self.communities = repo_to_community
        logger.info(f"成功识别出 {len(communities)} 个技术赛道。")
        return self.communities

    def get_hidden_gems(self, top_n=20, max_stars=5000):
        """
        核心挖掘逻辑：寻找 PageRank 高但 Star 数相对不高的“潜力股”。
        彻底消除重复建图，采用批量 SQL 一次性过滤，杜绝 N+1 查询。
        """
        if self.pr_results is None:
            self.run_personalized_pagerank()
        
        if not self.pr_results:
            return []

        # 取 PR 前 250 个候选仓库 ID
        candidates = self.pr_results[:250]
        repo_ids = [r_node.replace('r_', '') for r_node, _ in candidates]
        pr_map = {r_node.replace('r_', ''): score for r_node, score in candidates}
        
        hidden_gems = []
        if repo_ids:
            id_str = ",".join(repo_ids)
            rows = db_manager.execute_query(
                f"SELECT id, full_name, stargazers_count, description FROM repos WHERE id IN ({id_str})",
                db_type="source"
            )
            row_map = {str(r['id']): r for r in rows}
            
            for rid in repo_ids:
                info = row_map.get(str(rid))
                if not info: 
                    continue
                stars = info.get('stargazers_count') or 0
                if stars < max_stars:
                    hidden_gems.append({
                        "id": info['id'],
                        "full_name": info['full_name'],
                        "pr_score": pr_map[str(rid)],
                        "stars": stars,
                        "description": info.get('description') or "No description"
                    })
                    if len(hidden_gems) >= top_n:
                        break
        
        return hidden_gems

    def store_results(self, pr_results=None, community_map=None):
        """
        将计算结果存回数据库 (gh_insight_db)。
        使用 INSERT ... ON DUPLICATE KEY UPDATE 模式进行真正的高性能批量更新。
        需要补全 full_name 以满足 NOT NULL 约束。
        """
        logger.info("正在执行高性能批量入库...")
        pr_results = pr_results if pr_results is not None else (self.pr_results or [])
        community_map = community_map if community_map is not None else (self.communities or {})
        
        # 获取所有需要更新的 Repo 的 full_name
        repo_ids = set()
        if pr_results:
            repo_ids.update([int(r_node.replace('r_', '')) for r_node, _ in pr_results])
        if community_map:
            repo_ids.update([int(r_node.replace('r_', '')) for r_node in community_map.keys()])
            
        if not repo_ids:
            return
            
        id_str = ",".join(map(str, repo_ids))
        res = db_manager.execute_query(f"SELECT id, full_name FROM repos WHERE id IN ({id_str})", db_type="source")
        id_to_fullname = {r['id']: r['full_name'] for r in res}
        
        # 1. 存储 PageRank 分数
        if pr_results:
            pr_records = []
            for r_node, score in pr_results:
                repo_id = int(r_node.replace('r_', ''))
                fn = id_to_fullname.get(repo_id)
                if fn:
                    pr_records.append((repo_id, fn, float(score)))
            
            sql_update_repos = """
            INSERT INTO repos (id, full_name, influence_score) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE influence_score = VALUES(influence_score)
            """
            db_manager.execute_batch(sql_update_repos, pr_records, db_type="source")
            logger.info(f"已批量更新 {len(pr_records)} 个项目的 PageRank 分数。")

        # 2. 存储社区/赛道 ID
        if community_map:
            comm_records = []
            for r_node, c_id in community_map.items():
                repo_id = int(r_node.replace('r_', ''))
                fn = id_to_fullname.get(repo_id)
                if fn:
                    comm_records.append((repo_id, fn, f"Track #{c_id}"))
            
            sql_comm = """
            INSERT INTO repos (id, full_name, tech_category) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE tech_category = VALUES(tech_category)
            """
            db_manager.execute_batch(sql_comm, comm_records, db_type="source")
            logger.info(f"已批量更新 {len(comm_records)} 个项目的技术赛道标签。")
            
        logger.info("图计算结果存储完成。")
