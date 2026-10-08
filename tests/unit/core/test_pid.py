"""Tests for pid file handling and process-liveness helpers.

Relocated from ``tests/unit/core/test_proxy.py`` when the node proxy chain
was removed in Phase 2 of the sing-box removal.
"""

from pathlib import Path

import pytest

from openrot.core import pid


def test_pidfile_save_load_roundtrip(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.pid"
    pid.PidFile(pid_file).save(42)
    assert pid.PidFile(pid_file).load() == 42
    assert pid_file.stat().st_mode & 0o777 == 0o600


def test_pidfile_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert pid.PidFile(tmp_path / "none.pid").load() is None


def test_pidfile_load_returns_none_when_invalid(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.pid"
    pid_file.write_text("not-a-number")
    assert pid.PidFile(pid_file).load() is None


def test_pidfile_remove(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.pid"
    pid.PidFile(pid_file).save(7)
    pid.PidFile(pid_file).remove()
    assert not pid_file.exists()
    pid.PidFile(pid_file).remove()  # missing_ok


def test_save_daemon_pid_roundtrip(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.daemon.pid"
    pid.save_daemon_pid(7, pid_file)
    assert pid.load_daemon_pid(pid_file) == 7
    assert pid_file.stat().st_mode & 0o777 == 0o600


def test_load_daemon_pid_missing(tmp_path: Path) -> None:
    assert pid.load_daemon_pid(tmp_path / "none.pid") is None


def test_is_running_uses_kill_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(pid.os, "kill", lambda *a: calls.append(a))
    assert pid.is_running(9) is True
    assert calls == [(9, 0)]


def test_is_running_false_when_kill_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_lookup(*args: object) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(pid.os, "kill", raise_lookup)
    assert pid.is_running(999) is False


def test_is_running_true_on_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_perm(*args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(pid.os, "kill", raise_perm)
    assert pid.is_running(999) is True
