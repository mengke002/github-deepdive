import logging
import math
import re
from datetime import datetime, timedelta
from .database import db_manager
from .github_client import github_client

logger = logging.getLogger(__name__)

def calculate_velocity_scores():
    """
    基于 ranking_history 快照计算项目的 24 小时增速、7 天增速以及综合增长动能分数 (velocity_score)。
    """
    logger.info("开始计算项目的增长动能分数...")
    
    # 1. 获取当前日期（用于相对计算）
    # 由于可能存在历史数据回填，我们以数据库中最新的快照日期为基准
    latest_snap = db_manager.execute_query(
        "SELECT MAX(snapshot_date) as latest FROM ranking_history", 
        db_type="insight"
    )
    if not latest_snap or not latest_snap[0]['latest']:
        logger.warning("ranking_history 表中未找到快照数据，无法计算增速。")
        return
        
    latest_date = latest_snap[0]['latest']
    date_24h_ago = latest_date - timedelta(days=1)
    date_7d_ago = latest_date - timedelta(days=7)

    logger.info(f"以 {latest_date} 作为增速计算的基准日期。")

    # 2. 获取最新、24 小时前和 7 天前的快照数据
    # 使用兼容 TiDB 的标准窗口函数
    query = f"""
    WITH Latest AS (
        SELECT repo_id, stars_at_snapshot as stars, snapshot_date 
        FROM ranking_history 
        WHERE snapshot_date = '{latest_date}'
    ),
    Prev24h_Ordered AS (
        SELECT repo_id, stars_at_snapshot as stars,
               ROW_NUMBER() OVER(PARTITION BY repo_id ORDER BY snapshot_date DESC) as rn
        FROM ranking_history 
        WHERE snapshot_date <= '{date_24h_ago}'
        AND snapshot_date > '{date_24h_ago - timedelta(days=2)}'
    ),
    Prev24h AS (
        SELECT repo_id, stars FROM Prev24h_Ordered WHERE rn = 1
    ),
    Prev7d_Ordered AS (
        SELECT repo_id, stars_at_snapshot as stars,
               ROW_NUMBER() OVER(PARTITION BY repo_id ORDER BY snapshot_date DESC) as rn
        FROM ranking_history 
        WHERE snapshot_date <= '{date_7d_ago}'
        AND snapshot_date > '{date_7d_ago - timedelta(days=2)}'
    ),
    Prev7d AS (
        SELECT repo_id, stars FROM Prev7d_Ordered WHERE rn = 1
    )
    SELECT 
        L.repo_id,
        L.stars as current_stars,
        COALESCE(P24.stars, L.stars) as stars_24h_ago,
        COALESCE(P7.stars, L.stars) as stars_7d_ago
    FROM Latest L
    LEFT JOIN Prev24h P24 ON L.repo_id = P24.repo_id
    LEFT JOIN Prev7d P7 ON L.repo_id = P7.repo_id
    """
    
    stats = db_manager.execute_query(query, db_type="insight")
    if not stats:
        logger.warning("未找到足够的对比快照数据。")
        return

    update_records = []
    for row in stats:
        repo_id = row['repo_id']
        current_stars = row['current_stars']
        
        # 计算增量
        v_24h = max(0, current_stars - row['stars_24h_ago'])
        v_7d = max(0, (current_stars - row['stars_7d_ago']) / 7.0)
        
        # 动能分数公式: log(1 + 增速) * log(1 + 累计星数)
        # 该公式能有效挖掘出正在快速增长的小型项目，同时给获得大量绝对增长的大型项目以合理权重。
        velocity_score = math.log1p(v_24h) * math.log1p(current_stars)
        
        update_records.append((
            int(v_24h), float(v_7d), float(velocity_score), repo_id
        ))

    # 3. 更新源数据库中的 repos 表
    if update_records:
        update_sql = """
        UPDATE repos 
        SET star_velocity_24h = %s,
            star_velocity_7d = %s,
            velocity_score = %s
        WHERE id = %s
        """
        # 批量更新
        db_manager.execute_batch(update_sql, update_records, db_type="source")
        logger.info(f"成功更新了 {len(update_records)} 个仓库的增速分数。")

def collect_fine_grained_signals(limit=20):
    """
    针对增速最快的项目，采集更精细的信号：Release, Issue 热度, 依赖项。
    """
    logger.info(f"正在为前 {limit} 个黑马项目采集细粒度信号...")
    rising_stars = get_rising_stars(limit=limit)
    if not rising_stars: return

    for repo in rising_stars:
        full_name = repo['full_name']
        logger.info(f"正在分析 {full_name} 的细粒度信号...")
        
        # 1. Release 监听
        release = github_client.get_latest_release(full_name)
        latest_tag = release.get("tag_name") if release and isinstance(release, dict) else None
        
        # 2. Issue 活跃度计算 (热度分数)
        # 获取最近 30 个 open 且评论最多的 issue
        issues = github_client.get_issues(full_name, state="open", sort="comments")
        issue_heat = 0
        if issues and isinstance(issues, list):
            # 简单的热度算法：评论总数
            issue_heat = sum([i.get("comments", 0) for i in issues])
        
        # 更新仓库基础信号
        db_manager.execute_query(
            "UPDATE repos SET latest_release_tag=%s, issue_heat_score=%s WHERE full_name=%s",
            (latest_tag, float(issue_heat), full_name),
            db_type="source"
        )

        # 3. 依赖扫描雏形 (Python 示例)
        # 尝试获取 requirements.txt
        req_content = github_client.get_content(full_name, "requirements.txt")
        if req_content:
            deps = parse_python_dependencies(req_content)
            store_dependencies(full_name, deps, "python")

def parse_python_dependencies(content):
    """简单的正则解析 requirements.txt"""
    deps = []
    lines = content.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"): continue
        # 匹配 name[==version]
        match = re.match(r'^([a-zA-Z0-9\._-]+)([=<>!~]+.*)?$', line)
        if match:
            deps.append((match.group(1), match.group(2) or ""))
    return deps

def store_dependencies(full_name, deps, dep_type):
    """存入数据库"""
    repo = db_manager.execute_query(f"SELECT id FROM repos WHERE full_name='{full_name}'", db_type="source")
    if not repo: return
    repo_id = repo[0]['id']
    
    records = []
    for name, version in deps:
        records.append((repo_id, name, version, dep_type))
    
    if records:
        sql = """
        INSERT INTO repo_dependencies (repo_id, dep_name, dep_version, dep_type)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE dep_version=VALUES(dep_version)
        """
        db_manager.execute_batch(sql, records, db_type="insight")

def get_rising_stars(limit=10):
    """
    从数据库中查询当前增长势头最猛的“黑马”项目。
    """
    query = f"""
    SELECT full_name, star_velocity_24h, velocity_score, stargazers_count
    FROM repos
    WHERE star_velocity_24h > 0
    ORDER BY velocity_score DESC
    LIMIT {limit}
    """
    return db_manager.execute_query(query, db_type="source")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    calculate_velocity_scores()
    n = 15
    top_rising = get_rising_stars(limit=n)
    print(f"\n基于增长动能分数排序的前 {n} 个黑马项目:")
    for i, repo in enumerate(top_rising, 1):
        print(f"{i}. {repo['full_name']} | 24h 新增: +{repo['star_velocity_24h']} stars | 动能分数: {repo['velocity_score']:.2f}")
