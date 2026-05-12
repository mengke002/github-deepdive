import logging
from datetime import datetime, timedelta
from .database import db_manager

logger = logging.getLogger(__name__)

class UserAnalyzer:
    """
    负责开发者层级的深度分析：活跃度爆发、圈子协同背书、跨领域异动检测。
    """
    
    def analyze_kp_activity_bursts(self, window_days=7):
        """
        计算 Key Person 的活跃度爆发比率。
        对比过去 24h 的 Star 数量与过去 window_days 天的日均值。
        """
        logger.info("正在分析 Key Person 活跃度爆发...")
        
        # 1. 获取所有 KPs
        kps = db_manager.execute_query("SELECT id, login FROM users WHERE is_key_person = 1", db_type="source")
        if not kps: return []

        bursts = []
        for kp in kps:
            # 统计 24h 内的 star 数量
            count_24h = db_manager.execute_query(
                "SELECT COUNT(*) as cnt FROM repo_user_relations WHERE user_id=%s AND relation_type='STARGAZER' AND last_interaction_at > NOW() - INTERVAL 1 DAY",
                (kp['id'],), db_type="relation"
            )[0]['cnt']
            
            # 统计过去 window_days 天的总量
            count_window = db_manager.execute_query(
                f"SELECT COUNT(*) as cnt FROM repo_user_relations WHERE user_id=%s AND relation_type='STARGAZER' AND last_interaction_at > NOW() - INTERVAL {window_days} DAY",
                (kp['id'],), db_type="relation"
            )[0]['cnt']
            
            avg_window = count_window / float(window_days)
            
            # 如果 24h 的量显著高于平均值（例如 3 倍），则视为爆发
            if avg_window > 0 and count_24h > avg_window * 3 and count_24h >= 3:
                burst_ratio = count_24h / avg_window
                bursts.append({
                    "login": kp['login'],
                    "count_24h": count_24h,
                    "avg_window": avg_window,
                    "ratio": burst_ratio
                })
        
        return sorted(bursts, key=lambda x: x['ratio'], reverse=True)

    def detect_insider_clusters(self):
        """
        识别“圈子协同背书”信号。
        逻辑：24h 内有 ≥2 个互相关注的 KP Star 了同一个项目。
        """
        logger.info("正在检测圈子协同背书信号...")
        
        # 1. 先从 source 库获取所有 Key Person 的 ID
        kp_res = db_manager.execute_query("SELECT id FROM users WHERE is_key_person = 1", db_type="source")
        if not kp_res:
            return []
        kp_ids_all = [str(r['id']) for r in kp_res]
        kp_id_filter = ",".join(kp_ids_all)

        # 2. 寻找 24h 内获得 ≥2 个 KP Star 的 Repo (在 relation 库查询)
        query = f"""
        SELECT repo_id, GROUP_CONCAT(user_id) as kp_ids, COUNT(*) as kp_count
        FROM repo_user_relations
        WHERE relation_type = 'STARGAZER'
          AND last_interaction_at > NOW() - INTERVAL 1 DAY
          AND user_id IN ({kp_id_filter})
        GROUP BY repo_id
        HAVING kp_count >= 2
        """
        potential_clusters = db_manager.execute_query(query, db_type="relation")
        
        clusters = []
        for pc in potential_clusters:
            kp_ids = [int(i) for i in pc['kp_ids'].split(",")]
            repo_id = pc['repo_id']
            
            # 2. 检查这些 KP 之间是否有相互关注关系
            # 在 user_user_relations 中查找这些 ID 对之间的 FOLLOWS 关系
            id_str = ",".join([str(i) for i in kp_ids])
            follow_relations = db_manager.execute_query(
                f"SELECT user_id, target_user_id FROM user_user_relations WHERE user_id IN ({id_str}) AND target_user_id IN ({id_str}) AND relation_type='FOLLOWS'",
                db_type="relation"
            )
            
            if follow_relations:
                # 获取 Repo 详情
                repo_info = db_manager.execute_query(f"SELECT full_name, stargazers_count FROM repos WHERE id={repo_id}", db_type="source")
                if repo_info:
                    clusters.append({
                        "full_name": repo_info[0]['full_name'],
                        "stargazers": repo_info[0]['stargazers_count'],
                        "kp_count": pc['kp_count'],
                        "kp_logins": self._get_logins(kp_ids)
                    })
        
        return clusters

    def _get_logins(self, user_ids):
        if not user_ids: return []
        id_str = ",".join([str(i) for i in user_ids])
        res = db_manager.execute_query(f"SELECT login FROM users WHERE id IN ({id_str})", db_type="source")
        return [r['login'] for r in res]

user_analyzer = UserAnalyzer()
