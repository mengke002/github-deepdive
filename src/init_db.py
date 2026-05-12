import logging
from src.database import db_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def init_source_db():
    conn = db_manager.get_connection("source")
    with conn.cursor() as cursor:
        # Repos Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS repos (
            id BIGINT PRIMARY KEY,
            full_name VARCHAR(255) NOT NULL UNIQUE,
            owner_type VARCHAR(20),
            owner_id BIGINT,
            description TEXT,
            homepage VARCHAR(500),
            language VARCHAR(100),
            topics JSON,
            stargazers_count INT,
            forks_count INT,
            open_issues_count INT,
            subscribers_count INT,
            size_kb INT,
            created_at DATETIME,
            updated_at DATETIME,
            pushed_at DATETIME,
            license_spdx VARCHAR(100),
            archived BOOLEAN DEFAULT FALSE,
            is_fork BOOLEAN DEFAULT FALSE,
            
            is_seed BOOLEAN DEFAULT FALSE,
            seed_source VARCHAR(50),
            seed_weight DECIMAL(10,4) DEFAULT 0,
            first_ranked_at DATETIME,
            highest_rank INT,
            
            health_score DECIMAL(5,2) DEFAULT 0,
            trend_momentum DECIMAL(10,4) DEFAULT 0,
            influence_score DECIMAL(10,4) DEFAULT 0,
            final_score DECIMAL(10,4) DEFAULT 0,
            
            star_velocity_24h INT DEFAULT 0,
            star_velocity_7d DECIMAL(10,2) DEFAULT 0,
            velocity_score DECIMAL(10,4) DEFAULT 0,
            latest_release_tag VARCHAR(100),
            issue_heat_score DECIMAL(10,4) DEFAULT 0,
            super_seed BOOLEAN DEFAULT FALSE,
            commercial_signal BOOLEAN DEFAULT FALSE,
            
            created_at_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at_ts DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_language (language),
            INDEX idx_stargazers (stargazers_count),
            INDEX idx_is_seed (is_seed),
            INDEX idx_final_score (final_score)
        );
        """)

        # Users Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            login VARCHAR(255) NOT NULL UNIQUE,
            type VARCHAR(20),
            name VARCHAR(255),
            company VARCHAR(255),
            blog VARCHAR(500),
            location VARCHAR(255),
            email VARCHAR(255),
            hireable BOOLEAN,
            bio TEXT,
            followers INT,
            following INT,
            public_repos INT,
            created_at DATETIME,
            updated_at DATETIME,
            
            is_key_person BOOLEAN DEFAULT FALSE,
            influence_score DECIMAL(10,4) DEFAULT 0,
            expertise_domains JSON,
            
            created_at_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at_ts DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_influence (influence_score),
            INDEX idx_key_person (is_key_person)
        );
        """)
    logger.info("Source DB tables initialized.")

def init_relation_db():
    conn = db_manager.get_connection("relation")
    with conn.cursor() as cursor:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS repo_user_relations (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            repo_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            relation_type VARCHAR(50) NOT NULL,
            weight DECIMAL(5,2) DEFAULT 0,
            contributions_count INT DEFAULT 0,
            first_interaction_at DATETIME,
            last_interaction_at DATETIME,
            UNIQUE KEY uk_repo_user_type (repo_id, user_id, relation_type),
            INDEX idx_repo (repo_id),
            INDEX idx_user (user_id),
            INDEX idx_type (relation_type)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_user_relations (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            target_user_id BIGINT NOT NULL,
            relation_type VARCHAR(50) NOT NULL,  -- 'FOLLOWS','CO_CONTRIBUTOR','SAME_ORG'
            weight DECIMAL(5,2) DEFAULT 0,
            common_repos_count INT DEFAULT 0,
            UNIQUE KEY uk_user_user_type (user_id, target_user_id, relation_type),
            INDEX idx_user_user_type (user_id, target_user_id, relation_type)
        );
        """)
    logger.info("Relation DB tables initialized.")

def init_insight_db():
    conn = db_manager.get_connection("insight")
    with conn.cursor() as cursor:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS ranking_history (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            repo_id BIGINT,
            repo_full_name VARCHAR(255),
            snapshot_date DATETIME NOT NULL,
            rank_position INT NOT NULL,
            stars_at_snapshot INT,
            forks_at_snapshot INT,
            open_issues_at_snapshot INT,
            language VARCHAR(50),
            INDEX idx_repo_date (repo_full_name, snapshot_date),
            INDEX idx_date (snapshot_date)
        );
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            repo_id BIGINT NOT NULL,
            snapshot_date DATE NOT NULL,
            stars INT,
            forks INT,
            open_issues INT,
            UNIQUE KEY uk_repo_date (repo_id, snapshot_date)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_snapshots (
            user_id BIGINT,
            snapshot_date DATE,
            followers_count INT,
            following_count INT,
            public_repos INT,
            hireable BOOLEAN,
            bio TEXT,
            PRIMARY KEY (user_id, snapshot_date)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_summaries (
            repo_full_name VARCHAR(255) PRIMARY KEY,
            summary TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_updated_at (updated_at)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS repo_dependencies (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            repo_id BIGINT NOT NULL,
            dep_name VARCHAR(255) NOT NULL,
            dep_version VARCHAR(100),
            dep_type VARCHAR(50), -- 'python', 'npm', etc.
            UNIQUE KEY uk_repo_dep (repo_id, dep_name)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS intent_analyses (
            repo_full_name VARCHAR(255) PRIMARY KEY,
            market_gaps TEXT,
            pain_points TEXT,
            commercial_signals TEXT,
            raw_analysis JSON,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        );
        """)
    logger.info("Insight DB tables initialized.")

if __name__ == "__main__":
    init_source_db()
    init_relation_db()
    init_insight_db()
