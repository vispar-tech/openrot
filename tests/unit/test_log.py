import logging
import logging.handlers
from pathlib import Path

import pytest

from openrot import config as cfg
from openrot import log


def _fresh_logger(
    log_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unique = f"openrot.events.{id(tmp_path)}"
    monkeypatch.setattr(cfg, "LOG_PATH", log_path)
    monkeypatch.setattr(log, "LOGGER_NAME", unique)
    logger = logging.getLogger(unique)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    log_path.parent.mkdir(parents=True, exist_ok=True)


def test_get_logger_rotating_handler_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "openrot.log"
    _fresh_logger(log_path, tmp_path, monkeypatch)

    logger = log.get_logger()
    assert logger.name == log.LOGGER_NAME
    assert len(logger.handlers) == 1
    file_handler = logger.handlers[0]
    assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
    assert file_handler.level == logging.INFO
    assert logger.propagate is False

    logger.info("some event")
    file_handler.flush()
    assert log_path.exists()
    assert log_path.stat().st_mode & 0o777 == 0o600


def test_debug_only_reaches_file_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPENROT_DEBUG_REQ=1 => DEBUG accepted by logger and file."""
    log_path = tmp_path / "openrot.log"
    _fresh_logger(log_path, tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROT_DEBUG_REQ", "1")
    logger = log.get_logger()
    assert logger.level == logging.DEBUG
    assert len(logger.handlers) == 1
    file_handler = logger.handlers[0]
    assert file_handler.level == logging.DEBUG

    logger.debug("[bridge][debug] body: stream=True")
    file_handler.flush()
    content = log_path.read_text()
    assert "stream=True" in content  # DEBUG truly reached the file
