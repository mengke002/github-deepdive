import os
import configparser
from dotenv import load_dotenv

def load_config(config_path="config.ini"):
    """
    Load configuration prioritizing Environment Variables (GitHub Actions)
    over local config.ini values.
    """
    # 优先加载本地 .env (如果存在)
    load_dotenv()
    
    config = configparser.ConfigParser()
    if os.path.exists(config_path):
        config.read(config_path, encoding='utf-8')
    
    def get_setting(section, key, env_var_name, default=None, aliases=None, key_aliases=None):
        # 1. Highest priority: Environment Variable
        env_val = os.getenv(env_var_name)
        if env_val is not None and env_val != "":
            return env_val
        
        # 2. Second priority: config.ini
        section_candidates = [section]
        if aliases:
            section_candidates.extend(aliases)

        candidate_keys = [key]
        if key_aliases:
            candidate_keys.extend(key_aliases)

        for candidate_section in section_candidates:
            for candidate_key in candidate_keys:
                try:
                    value = config.get(candidate_section, candidate_key)
                    if value is not None and value != "":
                        return value
                except (configparser.NoSectionError, configparser.NoOptionError):
                    continue

        # 3. Lowest priority: default value
        return default

    def split_tokens(raw_value):
        if not raw_value:
            return []
        if isinstance(raw_value, list):
            return [item.strip() for item in raw_value if item and item.strip()]
        return [item.strip() for item in str(raw_value).split(",") if item.strip()]

    def to_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # 构造并返回配置字典 (模拟 rss-info-collector 的最佳实践)
    settings = {
        "database": {
            "source": {
                "host": get_setting("database_source", "host", "TIDB_SOURCE_HOST", "127.0.0.1"),
                "port": to_int(get_setting("database_source", "port", "TIDB_SOURCE_PORT", 4000), 4000),
                "user": get_setting("database_source", "user", "TIDB_SOURCE_USER", "root"),
                "password": get_setting("database_source", "password", "TIDB_SOURCE_PASSWORD", ""),
                "db_name": get_setting("database_source", "db_name", "TIDB_SOURCE_DB", "gh_source_db"),
                "ssl_mode": get_setting("database_source", "ssl_mode", "TIDB_SOURCE_SSL", "REQUIRED"),
                "ssl_ca": get_setting("database_source", "ssl_ca", "TIDB_SSL_CA", ""),
                "connect_timeout": to_int(get_setting("database_source", "connect_timeout", "TIDB_SOURCE_CONNECT_TIMEOUT", 5), 5),
            },
            "relation": {
                "host": get_setting("database_relation", "host", "TIDB_RELATION_HOST", "127.0.0.1"),
                "port": to_int(get_setting("database_relation", "port", "TIDB_RELATION_PORT", 4000), 4000),
                "user": get_setting("database_relation", "user", "TIDB_RELATION_USER", "root"),
                "password": get_setting("database_relation", "password", "TIDB_RELATION_PASSWORD", ""),
                "db_name": get_setting("database_relation", "db_name", "TIDB_RELATION_DB", "gh_relation_db"),
                "ssl_mode": get_setting("database_relation", "ssl_mode", "TIDB_RELATION_SSL", "REQUIRED"),
                "ssl_ca": get_setting("database_relation", "ssl_ca", "TIDB_SSL_CA", ""),
                "connect_timeout": to_int(get_setting("database_relation", "connect_timeout", "TIDB_RELATION_CONNECT_TIMEOUT", 5), 5),
            },
            "insight": {
                "host": get_setting("database_insight", "host", "TIDB_INSIGHT_HOST", "127.0.0.1"),
                "port": to_int(get_setting("database_insight", "port", "TIDB_INSIGHT_PORT", 4000), 4000),
                "user": get_setting("database_insight", "user", "TIDB_INSIGHT_USER", "root"),
                "password": get_setting("database_insight", "password", "TIDB_INSIGHT_PASSWORD", ""),
                "db_name": get_setting("database_insight", "db_name", "TIDB_INSIGHT_DB", "gh_insight_db"),
                "ssl_mode": get_setting("database_insight", "ssl_mode", "TIDB_INSIGHT_SSL", "REQUIRED"),
                "ssl_ca": get_setting("database_insight", "ssl_ca", "TIDB_SSL_CA", ""),
                "connect_timeout": to_int(get_setting("database_insight", "connect_timeout", "TIDB_INSIGHT_CONNECT_TIMEOUT", 5), 5),
            }
        },
        "github": {
            # GitHub Token 可以是逗号分隔的字符串，以支持 Token Pool
            "tokens": split_tokens(get_setting("github", "tokens", "GITHUB_TOKEN_POOL", "")),
            "max_concurrent_requests": to_int(
                get_setting("github", "max_concurrent_requests", "GITHUB_MAX_CONCURRENT_REQUESTS", 10),
                10,
            ),
        },
        "bigquery": {
            "credentials_path": get_setting("bigquery", "credentials_path", "GOOGLE_APPLICATION_CREDENTIALS", "./gcp-credentials.json"),
            "project_id": get_setting("bigquery", "project_id", "GOOGLE_CLOUD_PROJECT", ""),
        },
        "llm": {
            "api_key": get_setting("llm", "api_key", "OPENAI_API_KEY", "", aliases=["llm"], key_aliases=["openai_api_key"]),
            "base_url": get_setting("llm", "base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1", aliases=["llm"], key_aliases=["openai_base_url"]),
            "model_names": split_tokens(get_setting("llm", "model_name", "OPENAI_MODEL_NAME", "gpt-4o-mini", aliases=["llm"], key_aliases=["openai_model_name"])),
        },
        "report_llm": {
            "api_key": get_setting("report_llm", "api_key", "REPORT_LLM_API_KEY", "", aliases=["report_llm"], key_aliases=["report_api_key"]),
            "base_url": get_setting("report_llm", "base_url", "REPORT_LLM_BASE_URL", "https://api.openai.com/v1", aliases=["report_llm"], key_aliases=["report_base_url"]),
            "model_names": split_tokens(get_setting("report_llm", "model_name", "REPORT_LLM_MODEL_NAME", "gpt-4o", aliases=["report_llm"], key_aliases=["report_model_name"])),
        },
        "intent_llm": {
            "api_key": get_setting("intent_llm", "api_key", "INTENT_LLM_API_KEY", "", aliases=["intent_llm"], key_aliases=["intent_api_key"]),
            "base_url": get_setting("intent_llm", "base_url", "INTENT_LLM_BASE_URL", "https://api.openai.com/v1", aliases=["intent_llm"], key_aliases=["intent_base_url"]),
            "model_names": split_tokens(get_setting("intent_llm", "model_name", "INTENT_LLM_MODEL_NAME", "gpt-4o", aliases=["intent_llm"], key_aliases=["intent_model_name"])),
        },
        "notion": {
            "token": get_setting("notion", "token", "NOTION_TOKEN", "", aliases=["notion"], key_aliases=["integration_token"]),
            "page_id": get_setting("notion", "page_id", "NOTION_PAGE_ID", "", aliases=["notion"], key_aliases=["parent_page_id"]),
        },
        "scheduler": {
            "daily_alpha_cron": get_setting("scheduler", "daily_alpha_cron", "DAILY_ALPHA_CRON", "0 8 * * *"),
            "weekly_deepdive_cron": get_setting("scheduler", "weekly_deepdive_cron", "WEEKLY_DEEPDIVE_CRON", "0 2 * * 1"),
        }
    }
    
    return settings