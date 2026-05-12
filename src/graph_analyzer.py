import networkx as nx
import pandas as pd
import logging
from .database import db_manager

logger = logging.getLogger(__name__)

class GraphAnalyzer:
    def __init__(self):
        self.G = nx.Graph() # Use simple Graph for PageRank/Louvain to simplify, or MultiDiGraph for complexity

    def build_network(self, limit_days=30):
        """
        从 TiDB 加载关系数据，构建异构网络。
        优先加载强信号关系和近期动态。
        """
        logger.info(f"正在从 TiDB 构建关联网络 (最近 {limit_days} 天)...")
        self.G = nx.Graph()

        # 1. 加载 Repo-User 关系 (Star, Contributor 等)
        # 限制时间范围以保持图的敏锐度
        repo_user_query = f"""
        SELECT repo_id, user_id, relation_type, weight 
        FROM repo_user_relations
        WHERE last_interaction_at > NOW() - INTERVAL {limit_days} DAY
        """
        df_ru = pd.DataFrame(db_manager.execute_query(repo_user_query, db_type="relation"))
        
        for _, row in df_ru.iterrows():
            u_node = f"u_{row['user_id']}"
            r_node = f"r_{row['repo_id']}"
            # 如果边已存在，累加权重
            if self.G.has_edge(u_node, r_node):
                self.G[u_node][r_node]['weight'] += float(row['weight'])
            else:
                self.G.add_edge(u_node, r_node, weight=float(row['weight']), type=row['relation_type'])

        # 2. 加载 User-User 关系 (Follows)
        user_user_query = """
        SELECT user_id, target_user_id, weight FROM user_user_relations
        """
        df_uu = pd.DataFrame(db_manager.execute_query(user_user_query, db_type="relation"))
        for _, row in df_uu.iterrows():
            u1 = f"u_{row['user_id']}"
            u2 = f"u_{row['target_user_id']}"
            if self.G.has_edge(u1, u2):
                self.G[u1][u2]['weight'] += float(row['weight'])
            else:
                self.G.add_edge(u1, u2, weight=float(row['weight']), type='FOLLOWS')

        logger.info(f"网络构建完成: {self.G.number_of_nodes()} 节点, {self.G.number_of_edges()} 连边。")

    def run_personalized_pagerank(self):
        """
        运行 Localized PageRank。
        以 Key Persons 为源点进行能量扩散，发现“扫地僧”项目。
        """
        # 1. 获取所有 Key Person ID
        kp_res = db_manager.execute_query("SELECT id FROM users WHERE is_key_person = 1", db_type="source")
        kp_nodes = [f"u_{r['id']}" for r in kp_res if f"u_{r['id']}" in self.G]
        
        if not kp_nodes:
            logger.warning("图中未发现 Key Person 节点，无法运行 PageRank。")
            return {}

        # 2. 构造 Personalization 向量
        personalization = {node: 1.0 / len(kp_nodes) for node in kp_nodes}
        
        logger.info(f"正在以 {len(kp_nodes)} 位 Key Person 为源点运行 PageRank...")
        pr_scores = nx.pagerank(self.G, alpha=0.85, personalization=personalization, weight='weight')
        
        # 3. 过滤并排序 Repo 节点
        repo_scores = {node: score for node, score in pr_scores.items() if node.startswith('r_')}
        sorted_repos = sorted(repo_scores.items(), key=lambda x: x[1], reverse=True)
        
        return sorted_repos

    def detect_communities(self):
        """
        利用 Louvain 算法在 Repo-Repo 网络上进行社区发现（赛道聚类）。
        Repo-Repo 边的权重基于共同贡献者/关注者的 Jaccard 相似度或共现次数。
        """
        logger.info("正在生成 Repo-Repo 共现网络并运行社区发现...")
        
        # 1. 构建 Repo-Repo 投影图 (Projection)
        # 这里的简单实现：如果两个 Repo 被同一个 User star/contribute 过，则建立连边
        repo_nodes = [n for n in self.G.nodes() if n.startswith('r_')]
        repo_graph = nx.Graph()
        
        # 使用 NetworkX 的二部图投影可能在大图上很慢，这里用迭代方式
        for node in self.G.nodes():
            if node.startswith('u_'):
                neighbors = [n for n in self.G.neighbors(node) if n.startswith('r_')]
                if len(neighbors) > 1:
                    # 在邻居 Repo 之间建立两两连边
                    from itertools import combinations
                    for r1, r2 in combinations(neighbors, 2):
                        if repo_graph.has_edge(r1, r2):
                            repo_graph[r1][r2]['weight'] += 1
                        else:
                            repo_graph.add_edge(r1, r2, weight=1)

        if repo_graph.number_of_edges() == 0:
            return {}

        # 2. 运行 Louvain 算法
        from networkx.algorithms.community import louvain_communities
        communities = louvain_communities(repo_graph, weight='weight', seed=42)
        
        # 3. 整理结果：repo_id -> community_id
        repo_to_community = {}
        for idx, community in enumerate(communities):
            for node in community:
                repo_to_community[node] = idx
                
        logger.info(f"成功识别出 {len(communities)} 个技术赛道。")
        return repo_to_community

    def get_hidden_gems(self, top_n=20):
        """
        核心挖掘逻辑：寻找 PageRank 高但 Star 数相对不高的“潜力股”。
        """
        self.build_network()
        pr_results = self.run_personalized_pagerank()
        
        hidden_gems = []
        for r_node, pr_score in pr_results:
            repo_id = r_node.replace('r_', '')
            # 查询数据库获取当前 Star 数
            repo_info = db_manager.execute_query(
                f"SELECT full_name, stargazers_count, description FROM repos WHERE id={repo_id}", 
                db_type="source"
            )
            if repo_info:
                info = repo_info[0]
                # 扫地僧定义：PR 分数高，但 Star < 5000 (可调)
                stars = info.get('stargazers_count') or 0
                if stars < 5000:
                    hidden_gems.append({
                        "full_name": info['full_name'],
                        "pr_score": pr_score,
                        "stars": stars,
                        "description": info.get('description') or "No description"
                    })
            if len(hidden_gems) >= top_n:
                break
        
        return hidden_gems

    def store_results(self, pr_results, community_map):
        """
        将计算结果存回数据库 (gh_insight_db)。
        使用 INSERT ... ON DUPLICATE KEY UPDATE 模式进行真正的高性能批量更新。
        需要补全 full_name 以满足 NOT NULL 约束。
        """
        logger.info("正在执行高性能批量入库...")
        
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
