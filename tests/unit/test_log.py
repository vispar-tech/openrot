import logging
from pathlib import Path

import pytest

from openrot import config as cfg
from openrot import log


def test_get_logger_rotating_handler_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "openrot-events.log"
    unique = f"openrot.events.{id(event_path)}"
    monkeypatch.setattr(cfg, "EVENT_LOG_PATH", event_path)
    monkeypatch.setattr(log, "LOGGER_NAME", unique)
    event_path.parent.mkdir(parents=True, exist_ok=True)

    logger = log.get_logger()
    assert logger.name == unique
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.handlers.RotatingFileHandler)
    assert logger.propagate is False

    logger.info("some event")
    handler = logger.handlers[0]
    handler.flush()
    assert event_path.exists()
    assert event_path.stat().st_mode & 0o777 == 0o600


def test_console_echo_streams_events_to_stdout_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    event_path = tmp_path / "openrot-events.log"
    unique = f"openrot.events.console.{id(event_path)}"
    monkeypatch.setattr(cfg, "EVENT_LOG_PATH", event_path)
    monkeypatch.setattr(log, "LOGGER_NAME", unique)
    event_path.parent.mkdir(parents=True, exist_ok=True)

    log.console_echo()
    logger = log.get_logger()
    logger.info("some echo event")
    assert "some echo event" in capsys.readouterr().out

    def console_handlers() -> int:
        return sum(
            1
            for h in logger.handlers
            if getattr(h, "identity", None) == log.CONSOLE_IDENTITY
        )

    assert console_handlers() == 1
    log.console_echo()
    assert console_handlers() == 1


def test_rotate_if_large_shifts_existing_backups(tmp_path: Path) -> None:
    path = tmp_path / "openrot.log"
    path.write_text("x" * 1_500_000)
    (tmp_path / "openrot.log.1").write_text("old1")
    (tmp_path / "openrot.log.2").write_text("old2")

    log.rotate_if_large(path)

    assert (tmp_path / "openrot.log.1").read_text() == "x" * 1_500_000
    assert (tmp_path / "openrot.log.2").read_text() == "old1"
    assert (tmp_path / "openrot.log.3").read_text() == "old2"
    assert not path.exists()
