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
    同时支持 Trending 实时增量对全新项目的保底评估。
    """
    logger.info("开始计算项目的增长动能分数...")
    
    # 1. 获取当前日期（用于相对计算）
    latest_snap = db_manager.execute_query(
        "SELECT MAX(snapshot_date) as latest FROM ranking_history", 
        db_type="insight"
    )
    if not latest_snap or not latest_snap[0]['latest']:
        logger.warning("ranking_history 表中未找到快照数据，无法计算增速。")
        return
        
    latest_date = latest_snap[0]['latest']
    logger.info(f"以 {latest_date} 作为增速计算的基准日期。")

    # 2. 查询最新快照以及前序对比快照
    # 弹性窗口策略：寻找在 latest_date 之前、最近（7 天内）的历史快照以应对可能的时间断层
    query = f"""
    WITH Latest AS (
        SELECT repo_id, stars_at_snapshot as stars, snapshot_date 
        FROM ranking_history 
        WHERE snapshot_date = '{latest_date}'
    ),
    Prev_Ordered AS (
        SELECT repo_id, stars_at_snapshot as stars, snapshot_date,
               ROW_NUMBER() OVER(PARTITION BY repo_id ORDER BY snapshot_date DESC) as rn
        FROM ranking_history 
        WHERE snapshot_date < '{latest_date}'
          AND snapshot_date >= '{latest_date - timedelta(days=7)}'
    ),
    PrevClosest AS (
        SELECT repo_id, stars, snapshot_date FROM Prev_Ordered WHERE rn = 1
    )
    SELECT 
        L.repo_id,
        L.stars as current_stars,
        L.snapshot_date as snap_date,
        P.stars as prev_stars,
        P.snapshot_date as prev_date
    FROM Latest L
    LEFT JOIN PrevClosest P ON L.repo_id = P.repo_id
    """
    
    stats = db_manager.execute_query(query, db_type="insight")
    update_records = []
    
    if stats:
        # 查询 repos 表中已有的 star_velocity_24h (防止覆盖 Trending 页面直接解析到的当日激增数据)
        repo_ids = [str(r['repo_id']) for r in stats]
        existing_vel = {}
        if repo_ids:
            for i in range(0, len(repo_ids), 500):
                chunk = repo_ids[i:i+500]
                chunk_res = db_manager.execute_query(
                    f"SELECT id, star_velocity_24h, stargazers_count FROM repos WHERE id IN ({','.join(chunk)})",
                    db_type="source"
                )
                for cr in chunk_res:
                    existing_vel[cr['id']] = cr.get('star_velocity_24h') or 0

        for row in stats:
            repo_id = row['repo_id']
            current_stars = row['current_stars'] or 0
            prev_stars = row.get('prev_stars')
            prev_date = row.get('prev_date')
            
            v_24h = 0
            v_7d = 0.0
            
            if prev_stars is not None and prev_date is not None:
                # 根据历史快照相距的天数归一化为 24h 增速
                seconds_diff = (row['snap_date'] - prev_date).total_seconds()
                days_diff = max(seconds_diff / 86400.0, 0.1)
                delta_stars = max(0, current_stars - prev_stars)
                v_24h = int(delta_stars / max(1.0, days_diff))
                v_7d = float(delta_stars / max(1.0, days_diff / 7.0))
            
            # 若快照差值为 0（例如首次入库的 Trending 新项目），但 repos 表已有抓取到的今日增速，则予以保留
            if v_24h == 0 and existing_vel.get(repo_id, 0) > 0:
                v_24h = existing_vel[repo_id]
                v_7d = float(v_24h * 7)
            
            # 动能分数公式: log(1 + 增速) * log(1 + 累计星数)
            velocity_score = math.log1p(v_24h) * math.log1p(current_stars)
            
            update_records.append((
                int(v_24h), float(v_7d), float(velocity_score), repo_id
            ))

    # 3. 批量更新源数据库中的 repos 表
    if update_records:
        update_sql = """
        UPDATE repos 
        SET star_velocity_24h = %s,
            star_velocity_7d = %s,
            velocity_score = %s
        WHERE id = %s
        """
        db_manager.execute_batch(update_sql, update_records, db_type="source")
        logger.info(f"成功更新了 {len(update_records)} 个仓库的增速分数。")

    # 4. 保底更新：确保所有今日 Trending 且有增量的项目均已同步最新的动能分数
    fallback_trending = db_manager.execute_query(
        "SELECT id, stargazers_count, star_velocity_24h FROM repos WHERE (last_trending_date = CURRENT_DATE OR star_velocity_24h > 0) AND velocity_score = 0",
        db_type="source"
    )
    if fallback_trending:
        fb_updates = []
        for tr in fallback_trending:
            v = tr.get('star_velocity_24h') or 0
            stars = tr.get('stargazers_count') or 0
            if v > 0:
                score = math.log1p(v) * math.log1p(stars)
                fb_updates.append((float(score), tr['id']))
        if fb_updates:
            db_manager.execute_batch("UPDATE repos SET velocity_score = %s WHERE id = %s", fb_updates, db_type="source")
            logger.info(f"为 {len(fb_updates)} 个当日趋势项目同步了保底动能分数。")

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
    SELECT full_name, star_velocity_24h, velocity_score, stargazers_count, latest_release_tag, issue_heat_score
    FROM repos
    WHERE star_velocity_24h > 0
    ORDER BY velocity_score DESC
    LIMIT {limit}
    """
    return db_manager.execute_query(query, db_type="source")

def get_weekly_rising_stars(limit=15):
    """
    从数据库中查询过去 7 天综合增速最猛的周度“增长之王”。
    综合考虑 7 天平均增量 (star_velocity_7d) 与累计体量。
    """
    query = f"""
    SELECT full_name, star_velocity_7d, star_velocity_24h, velocity_score, stargazers_count, description, latest_release_tag, issue_heat_score
    FROM repos
    WHERE star_velocity_7d > 0
    ORDER BY (star_velocity_7d * LOG10(stargazers_count + 10)) DESC
    LIMIT {limit}
    """
    results = db_manager.execute_query(query, db_type="source")
    if not results:
        # 若历史快照积累不足，自动降级为当日增长黑马
        results = get_rising_stars(limit=limit)
    return results

def get_weekly_trending_leaders(limit=10):
    """
    从 ranking_history 与 repos 中聚合查询过去 7 天登顶/上榜 Trending 的核心项目。
    """
    query = f"""
    SELECT repo_full_name as full_name, 
           COUNT(DISTINCT DATE(snapshot_date)) as trending_days,
           MAX(stars_at_snapshot) as stars_at_snap
    FROM ranking_history
    WHERE rank_position <= 50
      AND snapshot_date >= NOW() - INTERVAL 7 DAY
    GROUP BY repo_full_name
    ORDER BY trending_days DESC, stars_at_snap DESC
    LIMIT {limit}
    """
    rows = db_manager.execute_query(query, db_type="insight")
    results = []
    if rows:
        names = [f"'{r['full_name']}'" for r in rows if r.get('full_name')]
        if names:
            repo_infos = db_manager.execute_query(
                f"SELECT full_name, description, stargazers_count, star_velocity_7d FROM repos WHERE full_name IN ({','.join(names)})",
                db_type="source"
            )
            info_map = {r['full_name']: r for r in repo_infos}
            for r in rows:
                fn = r['full_name']
                inf = info_map.get(fn, {})
                results.append({
                    "full_name": fn,
                    "trending_days": r['trending_days'],
                    "stargazers_count": inf.get('stargazers_count') or r.get('stars_at_snap') or 0,
                    "star_velocity_7d": inf.get('star_velocity_7d') or 0,
                    "description": inf.get('description') or "No description"
                })
    
    # 兜底：如果 ranking_history 7 天内数据不足，取最近 7 天的 Trending 项目
    if not results:
        fb_query = f"""
        SELECT full_name, description, stargazers_count, star_velocity_7d, star_velocity_24h, 1 as trending_days
        FROM repos
        WHERE last_trending_date >= CURRENT_DATE - INTERVAL 7 DAY
           OR seed_source LIKE '%Trending%'
        ORDER BY star_velocity_24h DESC, stargazers_count DESC
        LIMIT {limit}
        """
        results = db_manager.execute_query(fb_query, db_type="source")
    
    return results

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    calculate_velocity_scores()
    n = 15
    top_rising = get_rising_stars(limit=n)
    print(f"\n基于增长动能分数排序的前 {n} 个黑马项目:")
    for i, repo in enumerate(top_rising, 1):
        print(f"{i}. {repo['full_name']} | 24h 新增: +{repo['star_velocity_24h']} stars | 动能分数: {repo['velocity_score']:.2f}")
