import pytest

from openrot.providers import warp as w


class _NoSleep:
    @staticmethod
    def sleep(seconds: float) -> None:
        pass


def _patch(monkeypatch: pytest.MonkeyPatch, status_values: list[w.WarpStatus]) -> None:
    calls = {"n": 0}

    def fake_status() -> w.WarpStatus:
        calls["n"] += 1
        idx = min(calls["n"], len(status_values)) - 1
        return status_values[idx]

    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "status", fake_status)
    monkeypatch.setattr(w, "_run", lambda *args, **kw: "")
    monkeypatch.setattr(w, "time", _NoSleep)


def test_connect_polls_until_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, [w.WarpStatus.CONNECTING, w.WarpStatus.CONNECTED])
    assert w.connect() is True


def test_connect_returns_false_when_never_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, [w.WarpStatus.CONNECTING])
    assert w.connect() is False


def test_connect_sets_proxy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "_run", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(w, "status", lambda: w.WarpStatus.CONNECTED)
    monkeypatch.setattr(w, "time", _NoSleep)
    assert w.connect() is True
    assert ("mode", "proxy") in calls
    assert ("proxy", "port", str(w.WARP_PROXY_PORT)) in calls


def test_proxy_address_default() -> None:
    assert w.proxy_address() == (w.WARP_PROXY_HOST, w.WARP_PROXY_PORT)


def test_proxy_address_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROT_WARP_PORT", "56789")
    assert w.proxy_address() == ("127.0.0.1", 56789)


def test_proxy_address_host_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROT_WARP_HOST", "host.docker.internal")
    monkeypatch.setenv("OPENROT_WARP_PORT", "40000")
    assert w.proxy_address() == ("host.docker.internal", 40000)


def test_bin_path_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        w.shutil,
        "which",
        lambda name: "/opt/warp-cli" if name == "/custom/warp" else None,
    )
    monkeypatch.setenv("OPENROT_WARP", "/custom/warp")
    assert w.bin_path() == "/opt/warp-cli"


def test_bin_path_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w.shutil, "which", lambda name: None)
    monkeypatch.delenv("OPENROT_WARP", raising=False)
    assert w.bin_path() is None
    assert w.is_installed() is False


def test_status_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: False)
    assert w.status() == w.WarpStatus.NOT_INSTALLED


def test_status_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "_run", lambda *a, **kw: "Status update: whatever\n")
    assert w.status() == w.WarpStatus.UNKNOWN


def test_status_parses_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "_run", lambda *a, **kw: "Status update: connected\n")
    assert w.status() == w.WarpStatus.CONNECTED


def test_disconnect_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "_run", lambda *a, **kw: "")
    monkeypatch.setattr(w, "status", lambda: w.WarpStatus.DISCONNECTED)
    assert w.disconnect() is True


def test_disconnect_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "_run", lambda *a, **kw: "")
    monkeypatch.setattr(w, "status", lambda: w.WarpStatus.CONNECTED)
    assert w.disconnect() is False


def test_disconnect_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: False)
    assert w.disconnect() is False


def test_rotate_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(w, "is_installed", lambda: True)
    monkeypatch.setattr(w, "disconnect", lambda: calls.append("dis") or True)
    monkeypatch.setattr(w, "connect", lambda: calls.append("con") or True)
    assert w.rotate() is True
    assert calls == ["dis", "con"]


def test_rotate_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: False)
    assert w.rotate() is False


def test_install_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: False)
    with pytest.raises(RuntimeError, match="warp-cli not found"):
        w.install()


def test_install_noop_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "is_installed", lambda: True)
    w.install()


def test_current_ip_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"ip": "9.9.9.9"}

    monkeypatch.setattr(w.httpx.Client, "get", lambda self, url: _Resp())
    assert w.current_ip() == "9.9.9.9"


def test_current_ip_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(self: object, url: str) -> None:
        del self, url
        raise w.httpx.ConnectError("nope")

    monkeypatch.setattr(w.httpx.Client, "get", _boom)
    assert w.current_ip() is None


def test_current_ip_none_on_json_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            raise ValueError("bad json")

    monkeypatch.setattr(w.httpx.Client, "get", lambda self, url: _Resp())
    assert w.current_ip() is None
