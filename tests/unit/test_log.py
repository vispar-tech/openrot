import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

from openrot import config as cfg
from openrot import log


class _NonTty:
    def write(self, s: str) -> None:
        pass

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class _Tty:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, s: str) -> None:
        self.writes.append(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


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
    file_handler = logger.handlers[0]
    assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
    assert file_handler.level == logging.INFO
    assert logger.propagate is False

    logger.info("some event")
    file_handler.flush()
    assert log_path.exists()
    assert log_path.stat().st_mode & 0o777 == 0o600


def test_console_handler_added_only_when_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "openrot.log"
    _fresh_logger(log_path, tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdout", _NonTty())
    assert len(log.get_logger().handlers) == 1

    log_path_tty = tmp_path / "tty.log"
    _fresh_logger(log_path_tty, tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "stdout", _Tty())
    logger = log.get_logger()
    assert len(logger.handlers) == 2
    console_handler = logger.handlers[1]
    assert isinstance(console_handler, logging.StreamHandler)
    assert console_handler.level == logging.INFO


def test_debug_only_to_file_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPENROT_DEBUG_REQ=1 => DEBUG accepted by logger and file, never console."""
    log_path = tmp_path / "openrot.log"
    _fresh_logger(log_path, tmp_path, monkeypatch)
    tty = _Tty()
    monkeypatch.setattr(sys, "stdout", tty)
    monkeypatch.setenv("OPENROT_DEBUG_REQ", "1")
    logger = log.get_logger()
    assert logger.level == logging.DEBUG
    file_handler = logger.handlers[0]
    assert file_handler.level == logging.DEBUG
    console_handler = logger.handlers[1]
    assert console_handler.level == logging.INFO

    logger.debug("[bridge][debug] body: stream=True")
    logger.info("regular info")
    file_handler.flush()
    content = log_path.read_text()
    assert "regular info" in content
    assert "stream=True" in content  # DEBUG truly reached the file
    assert not any("stream=True" in w for w in tty.writes)
    assert any("regular info" in w for w in tty.writes)
