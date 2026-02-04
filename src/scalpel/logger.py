import bittensor as bt
import os
from pathlib import Path


def configure_logging() -> str:
    """
    Configure Bittensor logging based on LOG_LEVEL env and enable file logging.

    Rules:
    - Level is read from LOG_LEVEL: TRACE | DEBUG | INFO | WARNING (default TRACE).
    - File logging is controlled by BT_LOGGING_RECORD_LOG and BT_LOGGING_LOGGING_DIR.
    - Directory is created if it doesn't exist.
    """
    level_name = os.getenv("LOG_LEVEL", "TRACE").upper()

    record_log_env = os.getenv("BT_LOGGING_RECORD_LOG", "1")
    record_log = record_log_env.lower() in ("1", "true", "yes", "on")

    log_dir = os.getenv("BT_LOGGING_LOGGING_DIR", "./logs")
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    if level_name == "TRACE":
        bt.logging.set_trace()
    elif level_name == "DEBUG":
        bt.logging.debug()
    elif level_name in ("INFO", "WARNING", "WARN"):
        bt.logging.debug(False)
        bt.logging.set_trace(False)
    else:
        bt.logging.set_debug()
        level_name = "DEBUG"

    bt.logging(record_log=record_log, logging_dir=str(log_dir_path))

    bt.logging.debug(
        f"✅ Logging configured: level={level_name}, record_log={record_log}, dir={log_dir_path}"
    )
    return level_name
