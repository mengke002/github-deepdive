import logging
import sys

def setup_logger(config=None):
    """
    Set up standard logging to stdout.
    This works seamlessly with GitHub Actions logs.
    """
    log_level_name = None
    if config and isinstance(config, dict):
        log_level_name = config.get("logging", {}).get("level")

    log_level = getattr(logging, str(log_level_name).upper(), logging.INFO) if log_level_name else logging.INFO
    
    # 避免重复添加 handler
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
    
    return logging.getLogger(__name__)