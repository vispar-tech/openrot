from pathlib import Path

import pytest
from typer.testing import CliRunner

from openrot import cli
from openrot import config as cfg


def test_start_cascade_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, bool]] = []

    def fake_start(foreground: bool, daemon: bool) -> None:
        calls.append((foreground, daemon))

    monkeypatch.setattr(cli.cascade, "start", fake_start)
    result = CliRunner().invoke(cli.app, ["start", "cascade"])
    assert result.exit_code == 0
    assert calls == [(True, False)]


def test_start_cascade_daemon_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, bool]] = []

    def fake_start(foreground: bool, daemon: bool) -> None:
        calls.append((foreground, daemon))

    monkeypatch.setattr(cli.cascade, "start", fake_start)
    result = CliRunner().invoke(cli.app, ["start", "cascade", "--daemon"])
    assert result.exit_code == 0
    assert calls == [(False, True)]


def test_start_requires_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, bool]] = []

    def fake_start(foreground: bool, daemon: bool) -> None:
        calls.append((foreground, daemon))

    monkeypatch.setattr(cli.cascade, "start", fake_start)
    result = CliRunner().invoke(cli.app, ["start"])
    assert result.exit_code == 2
    assert "Missing argument 'MODE'" in result.output
    assert calls == []


def test_start_bridge_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake_serve() -> None:
        called.append(True)

    monkeypatch.setattr(cli.bridge, "serve", fake_serve)
    result = CliRunner().invoke(cli.app, ["start", "bridge"])
    assert result.exit_code == 0
    assert called == [True]


def test_start_bridge_daemon_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake_daemonize() -> None:
        called.append(True)

    monkeypatch.setattr(cli.bridge, "daemonize", fake_daemonize)
    result = CliRunner().invoke(cli.app, ["start", "bridge", "--daemon"])
    assert result.exit_code == 0
    assert called == [True]


def test_start_unknown_mode_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(cli.app, ["start", "bogus"])
    assert result.exit_code == 1
    assert "bogus" in result.output


def test_stop_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    prompts = []

    def fake_confirm(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    def fake_stop() -> None:
        calls.append("stop")

    monkeypatch.setattr(cli.typer, "confirm", fake_confirm)
    monkeypatch.setattr(cli.cascade, "stop", fake_stop)
    monkeypatch.setattr(cli.bridge, "stop_daemon", lambda: False)
    result = CliRunner().invoke(cli.app, ["stop"])
    assert result.exit_code == 0
    assert calls == ["stop"]
    assert prompts and "bridge daemon" in prompts[0]
    assert prompts and "level" in prompts[0]


def test_logs_no_follow_tails_each_file(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a[0]))
    monkeypatch.setattr(
        cli.cfg, "LOG_PATH", cfg.CONFIG_PATH.parent / "test-openrot.log"
    )
    monkeypatch.setattr(
        cli.cfg, "EVENT_LOG_PATH", cfg.CONFIG_PATH.parent / "test-openrot-events.log"
    )
    cli.cfg.LOG_PATH.write_text("daemon line 1\ndaemon line 2\n")
    cli.cfg.EVENT_LOG_PATH.write_text("events line 1\nevents line 2\n")

    cli._print_log_tail("daemon", cli.cfg.LOG_PATH, 50)
    cli._print_log_tail("events", cli.cfg.EVENT_LOG_PATH, 50)

    assert any("daemon line 2" in p for p in printed)
    assert any("events line 2" in p for p in printed)


def test_logs_missing_file_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a[0]))
    missing = cfg.CONFIG_PATH.parent / "does-not-exist.log"
    cli._print_log_tail("daemon", missing, 50)
    assert any("no daemon log" in p for p in printed)


def test_logs_includes_bridge_source(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_print_tail(name: str, path: Path, lines: int) -> None:
        seen.append(name)

    monkeypatch.setattr(cli, "_print_log_tail", fake_print_tail)
    result = CliRunner().invoke(cli.app, ["logs", "--no-follow"])
    assert result.exit_code == 0
    assert seen == ["daemon", "events", "bridge"]


def test_follow_logs_emits_tails_then_follows(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []

    def fake_print(msg: object, *a: object, **k: object) -> None:
        printed.append(str(msg))

    monkeypatch.setattr(cli.console, "print", fake_print)
    monkeypatch.setattr(
        cli.cfg, "LOG_PATH", cfg.CONFIG_PATH.parent / "follow-openrot.log"
    )
    monkeypatch.setattr(
        cli.cfg, "EVENT_LOG_PATH", cfg.CONFIG_PATH.parent / "follow-openrot-events.log"
    )
    cli.cfg.EVENT_LOG_PATH.write_text("events tail\n")
    monkeypatch.setattr(cli, "_read_last_lines", lambda path, count: ["TAIL-A"])

    def fake_sleep(t: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cli._follow_logs(
            {"daemon": cli.cfg.LOG_PATH, "events": cli.cfg.EVENT_LOG_PATH}, 2
        )

    assert any("TAIL-A" in p for p in printed)
