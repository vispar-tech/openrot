"""Tests for the shared daemon lifecycle helpers (openrot.core.daemon)."""

from pathlib import Path

import pytest

from openrot.core import daemon


def test_command_uses_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.sys, "frozen", False, raising=False)
    monkeypatch.setattr(daemon.sys, "executable", "/opt/venv/bin/python")
    assert daemon.command("bridge") == [
        "/opt/venv/bin/python",
        "-m",
        "openrot",
        "start",
        "bridge",
    ]


def test_command_frozen_reuses_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon.sys, "frozen", True, raising=False)
    monkeypatch.setattr(daemon.sys, "executable", "/usr/local/bin/openrot")
    assert daemon.command("cascade") == ["/usr/local/bin/openrot", "start", "cascade"]


def test_start_forks_detached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class Proc:
        pid = 41

    def fake_popen(cmd: list[str], **kwargs: object) -> object:
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: None)
    monkeypatch.setattr(daemon, "save_daemon_pid", lambda pid, path=None: None)

    daemon.start(
        name="cascade",
        pid_path=tmp_path / "openrot.daemon.pid",
    )

    cmd = calls["cmd"]
    assert isinstance(cmd, list) and cmd[-2:] == ["start", "cascade"]
    kwargs = calls["kwargs"]
    assert isinstance(kwargs, dict) and kwargs.get("start_new_session") is True


def test_start_frozen_reuses_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon.sys, "frozen", True, raising=False)
    monkeypatch.setattr(daemon.sys, "executable", "/usr/local/bin/openrot")
    calls: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> object:
        calls["cmd"] = cmd

        class Proc:
            pid = 7

        return Proc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: None)
    monkeypatch.setattr(daemon, "save_daemon_pid", lambda pid, path=None: None)

    daemon.start(name="bridge", pid_path=tmp_path / "b.pid")

    assert calls["cmd"] == ["/usr/local/bin/openrot", "start", "bridge"]


def test_start_wont_duplicate_live_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"n": 0}

    def fake_popen(cmd: list[str], **kwargs: object) -> object:
        called["n"] += 1

        class Proc:
            pid = 1

        return Proc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 1234)
    monkeypatch.setattr(daemon, "is_running", lambda pid: True)

    daemon.start(name="cascade", pid_path=tmp_path / "c.pid")

    assert called["n"] == 0


def test_start_clears_stale_pid_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "openrot.daemon.pid"
    pid_file.write_text("999999")
    calls: dict[str, object] = {}

    def fake_popen(cmd: list[str], **kwargs: object) -> object:
        calls["cmd"] = cmd

        class Proc:
            pid = 55

        return Proc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 999999)
    monkeypatch.setattr(daemon, "is_running", lambda pid: False)
    monkeypatch.setattr(daemon, "save_daemon_pid", lambda pid, path=None: None)

    daemon.start(name="cascade", pid_path=pid_file)

    assert "cmd" in calls
    assert not pid_file.exists()


def test_stop_terminates_and_removes_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, int]] = []
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("42\n")
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 42)
    monkeypatch.setattr(daemon, "is_running", lambda pid: True)
    monkeypatch.setattr(daemon.os, "kill", lambda *a: killed.append(a))

    assert daemon.stop(pid_file) is True
    assert killed == [(42, daemon.signal.SIGTERM)]
    assert not pid_file.exists()


def test_stop_noop_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: None)
    assert daemon.stop(tmp_path / "nope.pid") is False


def test_stop_noop_when_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("999999\n")
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 999999)
    monkeypatch.setattr(daemon, "is_running", lambda pid: False)
    assert daemon.stop(pid_file) is False


def test_stop_returns_false_when_kill_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("42\n")
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 42)
    monkeypatch.setattr(daemon, "is_running", lambda pid: True)

    def boom(*a: object) -> object:
        raise OSError("no such process")

    monkeypatch.setattr(daemon.os, "kill", boom)
    assert daemon.stop(pid_file) is False
    assert pid_file.exists()


def test_stop_and_wait_terminates_and_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[int, int]] = []
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("42\n")
    call_count = 0

    def mock_is_running(pid: int) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count < 3

    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 42)
    monkeypatch.setattr(daemon, "is_running", mock_is_running)
    monkeypatch.setattr(daemon.os, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(daemon.time, "sleep", lambda _: None)

    assert daemon.stop_and_wait(pid_file) is True
    assert killed == [(42, daemon.signal.SIGTERM)]
    assert not pid_file.exists()
    assert call_count == 3


def test_stop_and_wait_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("42\n")
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 42)
    monkeypatch.setattr(daemon, "is_running", lambda pid: True)
    monkeypatch.setattr(daemon.os, "kill", lambda *a: None)
    monkeypatch.setattr(daemon.time, "sleep", lambda _: None)

    assert daemon.stop_and_wait(pid_file) is False


def test_stop_and_wait_noop_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: None)
    assert daemon.stop_and_wait(tmp_path / "nope.pid") is False


def test_stop_and_wait_noop_when_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("999999\n")
    monkeypatch.setattr(daemon, "load_daemon_pid", lambda path=None: 999999)
    monkeypatch.setattr(daemon, "is_running", lambda pid: False)
    assert daemon.stop_and_wait(pid_file) is False
