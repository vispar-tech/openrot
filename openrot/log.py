import logging
import logging.handlers
import os
import sys

from openrot import config as cfg

LOGGER_NAME = "openrot.events"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_CONSOLE_FORMAT = "%(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_DEBUG_ENV = "OPENROT_DEBUG_REQ"


def get_logger() -> logging.Logger:
    """Return the events logger, configured once to append to a rotating file.

    A console handler (INFO, ``%(message)s``-only) is attached only when stdout
    is a terminal, so internal events (rotations, starts, bridge requests) are
    visible during interactive use but stay file-only in daemon/scripted runs.
    ``OPENROT_DEBUG_REQ=1`` raises the *file* handler to DEBUG (request-level
    dumps); the console handler never logs DEBUG.
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
    if sys.stdout.isatty():
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        logger.addHandler(console_handler)
    logger.propagate = False
    cfg.LOG_PATH.chmod(0o600)
    return logger
