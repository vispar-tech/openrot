from pathlib import Path

import pytest

from openrot.core import proxy


def test_write_pid_is_0600(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.pid"
    proxy._write_pid(1234, pid_file)
    assert pid_file.read_text().strip() == "1234"
    assert pid_file.stat().st_mode & 0o777 == 0o600


def test_save_load_pid_roundtrip(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.pid"
    proxy.save_pid(42, pid_file)
    assert proxy.load_pid(pid_file) == 42


def test_load_pid_returns_none_when_missing(tmp_path: Path) -> None:
    assert proxy.load_pid(tmp_path / "none.pid") is None


def test_load_pid_returns_none_when_invalid(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.pid"
    pid_file.write_text("not-a-number")
    assert proxy.load_pid(pid_file) is None


def test_save_daemon_pid_roundtrip(tmp_path: Path) -> None:
    pid_file = tmp_path / "openrot.daemon.pid"
    proxy.save_daemon_pid(7, pid_file)
    assert proxy.load_daemon_pid(pid_file) == 7
    assert pid_file.stat().st_mode & 0o777 == 0o600


def test_is_running_uses_kill_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(proxy.os, "kill", lambda *a: calls.append(a))
    assert proxy.is_running(9) is True
    assert calls == [(9, 0)]


def test_launch_raises_when_singbox_exits_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_write_config(data: object) -> Path:
        return Path("_launch-config.json")

    class PreExited:
        def poll(self) -> int:
            return 1

        returncode = 1

    monkeypatch.setattr(proxy, "write_config", fake_write_config)
    monkeypatch.setattr(proxy.subprocess, "Popen", lambda *a, **k: PreExited())
    monkeypatch.setattr(proxy.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="exited immediately"):
        proxy._launch({"log": {}}, "sing-box")


def test_stop_proxy_false_when_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "load_pid", lambda: None)
    assert proxy.stop_proxy() is False


def test_launch_returns_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        pid = 1234

        def poll(self) -> None:
            return None

    monkeypatch.setattr(proxy, "write_config", lambda data: Path("_cfg.json"))
    monkeypatch.setattr(proxy.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(proxy.time, "sleep", lambda s: None)
    assert proxy._launch({"log": {}}, "sing-box") == 1234


def test_start_node_http_uses_free_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Node, NodeProtocol

    captured: list[tuple[object, ...]] = []
    node = Node(id="n1", raw="http://proxy.host:8080", protocol=NodeProtocol.HTTP)
    monkeypatch.setattr(
        proxy, "start_free_proxy", lambda *a, **k: captured.append(a) or 5
    )
    pid = proxy.start_node(node, 7000, "sing-box")
    assert pid == 5
    assert captured and captured[0][0] == "http"
    assert captured[0][2] == 8080


def test_start_node_proxy_invalid_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Node, NodeProtocol

    node = Node(id="n1", raw="http://", protocol=NodeProtocol.HTTP)
    with pytest.raises(RuntimeError, match="invalid proxy node"):
        proxy.start_node(node, 7000, "sing-box")


def test_is_running_false_when_kill_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_lookup(*args: object) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(proxy.os, "kill", raise_lookup)
    assert proxy.is_running(999) is False


def test_is_running_true_on_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_perm(*args: object) -> None:
        raise PermissionError

    monkeypatch.setattr(proxy.os, "kill", raise_perm)
    assert proxy.is_running(999) is True


def test_stop_proxy_sends_term(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []
    call_count = {"n": 0}

    def fake_is_running(pid: int) -> bool:
        call_count["n"] += 1
        return call_count["n"] <= 1

    monkeypatch.setattr(proxy, "load_pid", lambda: 5)
    monkeypatch.setattr(proxy, "is_running", fake_is_running)
    monkeypatch.setattr(proxy.os, "kill", lambda *a: signals.append(a))
    assert proxy.stop_proxy(5) is True
    assert signals == [(5, proxy.signal.SIGTERM)]
