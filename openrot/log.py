import logging
import logging.handlers
import sys

from openrot import config as cfg

LOGGER_NAME = "openrot.events"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_CONSOLE_FORMAT = "%(levelname)s %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
CONSOLE_IDENTITY = "openrot.console-echo"


def get_logger() -> logging.Logger:
    """Return the events logger, configured once to append to a rotating file."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    cfg.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    cfg.LOG_PATH.chmod(0o600)
    return logger


def console_echo() -> None:
    """Echo the events logger to stdout (once), for interactive foreground runs."""
    logger = get_logger()
    for handler in logger.handlers:
        if getattr(handler, "identity", None) == CONSOLE_IDENTITY:
            return
    stream = logging.StreamHandler(sys.stdout)
    stream.identity = CONSOLE_IDENTITY  # type: ignore[attr-defined]
    stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(stream)
