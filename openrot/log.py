import logging
import logging.handlers
import os

from openrot import config as cfg

LOGGER_NAME = "openrot.events"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_DEBUG_ENV = "OPENROT_DEBUG_REQ"


def get_logger() -> logging.Logger:
    """Return the events logger, configured once to append to a rotating file.

    Events are file-only: terminal output is done explicitly via ``console.print``
    on the CLI path. ``OPENROT_DEBUG_REQ=1`` raises the file level to DEBUG
    (request-level dumps).
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    cfg.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_level = logging.DEBUG if os.environ.get(_DEBUG_ENV) else logging.INFO
    logger.setLevel(file_level)
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(file_handler)
    logger.propagate = False
    cfg.LOG_PATH.chmod(0o600)
    return logger
