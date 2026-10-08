"""In-process CliRunner tests that exercise the CLI command bodies directly.

Unlike the subprocess-based CLI tests, invoking via `CliRunner` on the actual
command functions lets coverage count the executed lines. Dependencies are
monkeypatched at the `cli` module level so no external tools or network are
required.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openrot import cli
from openrot.config import ActiveLevel, Config
from openrot.providers.warp import WarpStatus

runner = CliRunner()


def test_version(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "openrot" in result.output


def test_rotate_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def fake_rotate() -> bool:
        return True

    monkeypatch.setattr(cli.cascade, "rotate", fake_rotate)
    result = runner.invoke(cli.app, ["rotate"])
    assert result.exit_code == 0
    assert calls == []


def test_stop_stops_cascade_and_bridge_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_stop_daemon() -> bool:
        calls.append("bridge")
        return True

    monkeypatch.setattr(cli.typer, "confirm", lambda prompt: True)
    monkeypatch.setattr(cli.cascade, "stop", lambda: calls.append("cascade"))
    monkeypatch.setattr(cli.bridge, "stop_daemon", fake_stop_daemon)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0
    assert calls == ["cascade", "bridge"]
    assert "bridge daemon stopped" in result.output


def test_status_warp(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "warp" in result.output.lower()
    assert "bridge: stopped" in result.output


def test_status_warp_json_reports_real_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """warp_connected must reflect an actual probe through WARP, not the flag."""

    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: None)
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    ok_result = runner.invoke(cli.app, ["status", "--json"])
    assert ok_result.exit_code == 0
    data = json.loads(ok_result.output)
    assert data["warp_connected"] is False
    assert data["connectivity"]["ok"] is False
    assert data["bridge_running"] is False

    monkeypatch.setattr(cli.warp, "current_ip", lambda: "1.2.3.4")
    monkeypatch.setattr(cli.bridge, "running", lambda c: True)
    good_result = runner.invoke(cli.app, ["status", "--json"])
    data = json.loads(good_result.output)
    assert data["warp_connected"] is True
    assert data["connectivity"] == {"ok": True, "ip": "1.2.3.4"}
    assert data["bridge_running"] is True


def test_status_warp_text_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: None)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "FAIL" in result.output


def test_status_bridge_running_warp_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live bridge shows its URL in the human output and true in JSON."""

    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "1.2.3.4")
    monkeypatch.setattr(cli.bridge, "running", lambda c: True)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "bridge: running (http://127.0.0.1:7891/v1)" in result.output

    result_json = runner.invoke(cli.app, ["status", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert data["bridge_running"] is True


def test_run_no_command(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["run"])
    assert result.exit_code == 2


def test_run_starts_and_executes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_obj = Config(active_level=ActiveLevel.NONE)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.cascade, "level_serving", lambda c: False)
    monkeypatch.setattr(cli.cascade, "start", lambda: None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, env=None: type("P", (), {"returncode": 0})(),
    )
    result = runner.invoke(cli.app, ["run", "--", "true"])
    assert result.exit_code == 0
    assert "running:" in result.output


def test_status_verbose_shows_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "is_installed", lambda: True)
    monkeypatch.setattr(cli.warp, "status", lambda: WarpStatus.CONNECTED)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    result = runner.invoke(cli.app, ["status", "--verbose"])
    assert result.exit_code == 0
    assert "openrot status --verbose" in result.output
    assert "WARP" in result.output
    assert "sing-box" not in result.output
    assert "proxy" not in result.output


def test_warp_install_already(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.warp, "is_installed", lambda: True)
    monkeypatch.setattr(cli.warp, "bin_path", lambda: "/usr/bin/warp-cli")
    result = runner.invoke(cli.app, ["warp", "install"])
    assert result.exit_code == 0
    assert "already installed" in result.output


def test_warp_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.warp, "connect", lambda: True)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    result = runner.invoke(cli.app, ["warp", "on"])
    assert result.exit_code == 0
    assert "connected" in result.output
    assert "9.9.9.9" in result.output


def test_warp_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.warp, "disconnect", lambda: True)
    result = runner.invoke(cli.app, ["warp", "off"])
    assert result.exit_code == 0
    assert "WARP disconnected" in result.output


def test_warp_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.warp, "status", lambda: WarpStatus.DISCONNECTED)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: None)
    result = runner.invoke(cli.app, ["warp", "status"])
    assert result.exit_code == 0
    assert "WARP status" in result.output


def test_warp_status_json(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(cli.warp, "status", lambda: WarpStatus.CONNECTED)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    result = runner.invoke(cli.app, ["warp", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {"status": "connected", "ip": "9.9.9.9"}
