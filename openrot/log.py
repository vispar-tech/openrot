import logging
import logging.handlers
import sys
from pathlib import Path

from openrot import config as cfg

LOGGER_NAME = "openrot.events"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
CONSOLE_IDENTITY = "openrot.console-echo"


def rotate_if_large(
    path: Path, max_bytes: int = _MAX_BYTES, backup_count: int = _BACKUP_COUNT
) -> None:
    """Rotate a plain log file to .1/.2/... once it exceeds ``max_bytes``.

    Shared by the events logger's ``RotatingFileHandler`` (which rotates in
    place) and the daemon, whose subprocess writes a raw file descriptor that
    cannot go through a logging handler — rotation there happens on start.
    """
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for i in range(backup_count - 1, 0, -1):
        src = Path(f"{path}.{i}")
        if src.exists():
            src.replace(Path(f"{path}.{i + 1}"))
    path.replace(Path(f"{path}.1"))


def get_logger() -> logging.Logger:
    """Return the events logger, configured once to append to a rotating file."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    cfg.EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.EVENT_LOG_PATH, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    cfg.EVENT_LOG_PATH.chmod(0o600)
    return logger


def console_echo() -> None:
    """Echo the events logger to stdout (once), for interactive foreground runs."""
    logger = get_logger()
    for handler in logger.handlers:
        if getattr(handler, "identity", None) == CONSOLE_IDENTITY:
            return
    stream = logging.StreamHandler(sys.stdout)
    stream.identity = CONSOLE_IDENTITY  # type: ignore[attr-defined]
    stream.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(stream)
