import logging
from pathlib import Path

import pytest

from openrot import config as cfg
from openrot import log


def test_get_logger_rotating_handler_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "openrot.log"
    unique = f"openrot.events.{id(log_path)}"
    monkeypatch.setattr(cfg, "LOG_PATH", log_path)
    monkeypatch.setattr(log, "LOGGER_NAME", unique)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = log.get_logger()
    assert logger.name == unique
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.handlers.RotatingFileHandler)
    assert logger.propagate is False

    logger.info("some event")
    handler = logger.handlers[0]
    handler.flush()
    assert log_path.exists()
    assert log_path.stat().st_mode & 0o777 == 0o600
