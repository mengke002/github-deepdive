import re
import math
import logging
import hashlib
import concurrent.futures
from datetime import datetime
from collections import defaultdict
import requests

from src.github_client import github_client
from src.database import db_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REPO_OWNER = "EvanLi"
REPO_NAME = "Github-Ranking"
FILE_PATH = "Top100/Python.md"

def fetch_commits():
    """Fetch all commits for the given file using pagination."""
    commits = []
    page = 1
    while True:
        logger.info(f"Fetching commits page {page}...")
        resp = github_client.request("GET", f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits", 
                                   params={"path": FILE_PATH, "per_page": 100, "page": page})
        if not resp or not isinstance(resp, list):
            break
        commits.extend(resp)
        if len(resp) < 100:
            break
        page += 1
    
    return commits

def parse_markdown_table(content):
    """Parses the Markdown table and returns a list of repo data."""
    lines = content.split('\n')
    data = []
    headers_found = False
    
    for line in lines:
        line = line.strip()
        if not line.startswith('|'):
            continue
        
        parts = [p.strip() for p in line.split('|')[1:-1]]
        if not parts or parts[0].lower() == 'ranking' or set(parts[0]) == {'-', ':'}:
            headers_found = True
            continue
            
        if headers_found and parts[0].isdigit():
            try:
                rank = int(parts[0])
                proj_name_str = parts[1]
                match = re.search(r'\[.*?\]\((https://github\.com/([^/]+/[^/]+?))/?\)', proj_name_str)
                if not match:
                    continue
                full_name = match.group(2)
                
                stars = int(parts[2]) if parts[2].isdigit() else 0
                forks = int(parts[3]) if parts[3].isdigit() else 0
                language = parts[4]
                open_issues = int(parts[5]) if parts[5].isdigit() else 0
                desc = parts[6]
                
                data.append({
                    "rank": rank,
                    "full_name": full_name,
                    "stars": stars,
                    "forks": forks,
                    "open_issues": open_issues,
                    "language": language,
                    "description": desc
                })
            except Exception as e:
                pass
    return data

def process_commit(commit_info):
    """Downloads and parses a single commit version."""
    sha = commit_info['sha']
    date_str = commit_info['commit']['committer']['date']
    date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
    
    raw_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{sha}/{FILE_PATH}"
    try:
        resp = requests.get(raw_url, timeout=20)
        if resp.status_code == 200:
            rows = parse_markdown_table(resp.text)
            return {"date": date, "rows": rows, "sha": sha}
    except Exception as e:
        logger.error(f"Error downloading {sha}: {e}")
    return None

def get_repo_metadata(full_names):
    """Fetches repository IDs and basic metadata for unique repo names."""
    metadata = {}
    unique_names = list(set(full_names))
    logger.info(f"Fetching metadata for {len(unique_names)} unique repositories...")
    
    def fetch_one(name):
        repo_data = github_client.get_repo(name)
        if repo_data:
            return name, {
                "id": repo_data["id"],
                "full_name": repo_data["full_name"],
                "owner_id": repo_data["owner"]["id"],
                "owner_type": repo_data["owner"]["type"],
                "description": repo_data.get("description"),
                "homepage": repo_data.get("homepage"),
                "language": repo_data.get("language"),
                "stargazers_count": repo_data.get("stargazers_count"),
                "forks_count": repo_data.get("forks_count"),
                "open_issues_count": repo_data.get("open_issues_count"),
                "created_at": repo_data.get("created_at"),
                "updated_at": repo_data.get("updated_at"),
                "pushed_at": repo_data.get("pushed_at"),
                "license": repo_data.get("license", {}).get("spdx_id") if repo_data.get("license") else None
            }
        return name, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_name = {executor.submit(fetch_one, name): name for name in unique_names}
        for future in concurrent.futures.as_completed(future_to_name):
            name, res = future.result()
            if res:
                metadata[name] = res
                
    return metadata

def calculate_seed_weight(highest_rank, rank_count, days_ranked):
    # Weight formula from design doc: 0.4*(1/rank) + 0.3*(count) + 0.3*(days)
    rank_score = (1.0 / highest_rank) * 0.4
    count_score = min(rank_count / 365.0, 1.0) * 0.3
    days_score = min(days_ranked / 365.0, 1.0) * 0.3
    return rank_score + count_score + days_score

def merge_and_store(commits):
    """Orchestrates parallel downloading, metadata fetching, and storage."""
    logger.info(f"Processing {len(commits)} commits in parallel...")
    
    all_snapshots = []
    repo_stats = defaultdict(lambda: {
        "rank_count": 0, "highest_rank": 999, "first_date": None, "last_date": None
    })
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(process_commit, commits))
    
    valid_results = [r for r in results if r]
    # Sort by date ascending to process history correctly
    valid_results.sort(key=lambda x: x["date"])
    
    all_names = set()
    for res in valid_results:
        for row in res["rows"]:
            fn = row["full_name"]
            all_names.add(fn)
            
            # Update stats
            stats = repo_stats[fn]
            stats["rank_count"] += 1
            stats["highest_rank"] = min(stats["highest_rank"], row["rank"])
            if not stats["first_date"] or res["date"] < stats["first_date"]:
                stats["first_date"] = res["date"]
            if not stats["last_date"] or res["date"] > stats["last_date"]:
                stats["last_date"] = res["date"]
            
            all_snapshots.append({
                "full_name": fn,
                "date": res["date"],
                "rank": row["rank"],
                "stars": row["stars"],
                "forks": row["forks"],
                "issues": row["open_issues"]
            })

    # Get metadata (IDs)
    metadata_map = get_repo_metadata(list(all_names))
    
    # Prepare SQL data
    repo_records = []
    for fn, meta in metadata_map.items():
        stats = repo_stats[fn]
        days_ranked = (stats["last_date"] - stats["first_date"]).days + 1
        weight = calculate_seed_weight(stats["highest_rank"], stats["rank_count"], days_ranked)
        
        repo_records.append((
            meta["id"], meta["full_name"], meta["owner_type"], meta["owner_id"],
            meta["description"], meta["homepage"], meta["language"],
            meta["stargazers_count"], meta["forks_count"], meta["open_issues_count"],
            meta["created_at"], meta["updated_at"], meta["pushed_at"], meta["license"],
            True, "EvanLi/Github-Ranking", weight, stats["first_date"], stats["highest_rank"]
        ))

    # Insert Repos into source DB
    if repo_records:
        repo_query = """
        INSERT INTO repos (
            id, full_name, owner_type, owner_id, description, homepage, language,
            stargazers_count, forks_count, open_issues_count, created_at, updated_at, pushed_at, license_spdx,
            is_seed, seed_source, seed_weight, first_ranked_at, highest_rank
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            stargazers_count=VALUES(stargazers_count),
            forks_count=VALUES(forks_count),
            open_issues_count=VALUES(open_issues_count),
            updated_at=VALUES(updated_at),
            pushed_at=VALUES(pushed_at),
            seed_weight=VALUES(seed_weight),
            highest_rank=LEAST(highest_rank, VALUES(highest_rank))
        """
        db_manager.execute_batch(repo_query, repo_records, db_type="source")
        logger.info(f"Upserted {len(repo_records)} repositories into source DB.")

    # Insert Snapshots into insight DB
    snapshot_records = []
    for snap in all_snapshots:
        meta = metadata_map.get(snap["full_name"])
        if meta:
            snapshot_records.append((
                meta["id"], snap["full_name"], snap["date"], snap["rank"],
                snap["stars"], snap["forks"], snap["issues"], meta["language"]
            ))
            
    if snapshot_records:
        snap_query = """
        INSERT INTO ranking_history (
            repo_id, repo_full_name, snapshot_date, rank_position, stars_at_snapshot, 
            forks_at_snapshot, open_issues_at_snapshot, language
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        # Batch insert in chunks of 5000 to prevent packet size issues
        chunk_size = 5000
        for i in range(0, len(snapshot_records), chunk_size):
            chunk = snapshot_records[i:i + chunk_size]
            db_manager.execute_batch(snap_query, chunk, db_type="insight")
            logger.info(f"Stored chunk {i//chunk_size + 1} ({len(chunk)} snapshots) into insight DB.")
        logger.info(f"Successfully stored all {len(snapshot_records)} snapshots.")

def run():
    commits = fetch_commits()
    if not commits:
        logger.error("No commits found.")
        return
    # For initial run, maybe limit to 500 to avoid hitting API limits too hard if metadata fetching is slow
    # But let's try all as it's efficient with threading
    merge_and_store(commits)

if __name__ == "__main__":
    run()
