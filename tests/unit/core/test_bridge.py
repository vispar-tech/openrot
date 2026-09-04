"""Tests for the bridge reverse-proxy: 429 -> rotate + retry, and lifecycle.

The file historically also housed the ``start bridge`` helpers; those were
merged into ``openrot.core.bridge``, and their tests live here too.
"""

import socket
from pathlib import Path
from typing import Self

import httpx
import pytest

from openrot.config import ActiveLevel, Config
from openrot.core import bridge, daemon


def _cfg(**kw: object) -> Config:
    defaults: dict[str, object] = {
        "port": 7890,
        "bridge_upstream": "https://up.test/v1",
    }
    defaults.update(kw)
    return Config(**defaults)


def _req(method: str = "POST", path: str = "/v1/chat/completions") -> bridge._Request:
    return bridge._Request(method, path, {"Authorization": "Bearer x"}, b"{}")


def test_upstream_url_joins_base_and_path() -> None:
    assert bridge.upstream_url(_cfg(), "/v1/models") == "https://up.test/v1/v1/models"


def test_upstream_url_uses_configured_upstream() -> None:
    assert (
        bridge.upstream_url(_cfg(bridge_upstream="http://local:9/x"), "y")
        == "http://local:9/x/y"
    )


def test_is_rate_limited() -> None:
    assert bridge._is_rate_limited(httpx.Response(429), [429])
    assert not bridge._is_rate_limited(httpx.Response(200), [429])
    assert bridge._is_rate_limited(httpx.Response(503), [429, 503])


def test_forward_headers_strips_hop_by_hop_and_sets_host() -> None:
    headers = {"Host": "x", "Connection": "keep-alive", "Authorization": "Bearer k"}
    out = bridge._forward_headers(headers, "up.test")
    assert out["Host"] == "up.test"
    assert "Connection" not in out
    assert out["Authorization"] == "Bearer k"


class _FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.closed = False
        self.last_url: str | None = None

    def request(self, method: str, url: str, **kw: object) -> httpx.Response:
        self.last_url = url
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_forward_returns_upstream_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([httpx.Response(200, content=b"ok")])
    monkeypatch.setattr(bridge, "_client", lambda cfg: fake)
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(), _req())
    try:
        assert resp.status_code == 200
        assert resp.content == b"ok"
    finally:
        client.close()
    assert rotated == []
    assert fake.last_url == "https://up.test/v1/v1/chat/completions"


def test_forward_rotates_and_retries_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: list[httpx.Response] = [
        httpx.Response(429),
        httpx.Response(200, content=b"recovered"),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_client", make_client)
    rotated: list = []
    logged: list[str] = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: (rotated.append(1), True)[1])
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))

    resp, client = bridge.forward(_cfg(), _req())
    try:
        assert rotated == [1]
        assert resp.status_code == 200
        assert resp.content == b"recovered"
    finally:
        client.close()
    assert any("rotation took" in msg for msg in logged)


def test_forward_waits_and_retries_when_rotation_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: list[httpx.Response] = [
        httpx.Response(429),
        httpx.Response(200, content=b"recovered"),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_client", make_client)
    logged: list[str] = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: False)
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))

    resp, client = bridge.forward(_cfg(), _req())
    try:
        assert resp.status_code == 200
        assert resp.content == b"recovered"
    finally:
        client.close()
    assert any("waited" in msg for msg in logged)
    assert not any("rotation took" in msg for msg in logged)


def test_forward_no_rotate_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge, "_client", lambda cfg: _FakeClient([httpx.Response(429)])
    )
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(), _req(), rotate_on_429=False)
    try:
        assert resp.status_code == 429
    finally:
        client.close()
    assert rotated == []


def test_forward_429_after_retry_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: list[httpx.Response] = [httpx.Response(429), httpx.Response(429)]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_client", make_client)
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(), _req())
    try:
        assert rotated == [1]
        assert resp.status_code == 429
    finally:
        client.close()


def test_forward_retries_configured_status(monkeypatch: pytest.MonkeyPatch) -> None:
    queue: list[httpx.Response] = [httpx.Response(503), httpx.Response(200)]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_client", make_client)
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(bridge_retry_statuses=[503]), _req())
    try:
        assert rotated == [1]
        assert resp.status_code == 200
    finally:
        client.close()


def test_forward_ignores_non_configured_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge, "_client", lambda cfg: _FakeClient([httpx.Response(503)])
    )
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(bridge_retry_statuses=[429]), _req())
    try:
        assert rotated == []
        assert resp.status_code == 503
    finally:
        client.close()


def test_forward_respects_requested_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: list[httpx.Response] = [
        httpx.Response(429),
        httpx.Response(429),
        httpx.Response(429),
        httpx.Response(200),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_client", make_client)
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(bridge_retry_attempts=3), _req())
    try:
        assert rotated == [1, 1, 1]
        assert resp.status_code == 200
    finally:
        client.close()


def test_forward_zero_attempts_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bridge, "_client", lambda cfg: _FakeClient([httpx.Response(429)])
    )
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))

    resp, client = bridge.forward(_cfg(bridge_retry_attempts=0), _req())
    try:
        assert rotated == []
        assert resp.status_code == 429
    finally:
        client.close()


def test_fetch_raises_upstream_error_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoomClient:
        def request(self, method: str, url: str, **kw: object) -> httpx.Response:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(bridge, "_client", lambda cfg: BoomClient())
    with pytest.raises(bridge.UpstreamError):
        bridge.forward(_cfg(), _req())


def test_base_url_uses_configured_port() -> None:
    assert bridge.base_url(_cfg(bridge_port=9000)) == "http://127.0.0.1:9000/v1"


def test_running_false_when_no_listener() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert bridge.running(_cfg(bridge_port=port)) is False


def test_serve_starts_cascade_and_listens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(bridge, "_running_level", lambda cfg_obj, check: False)
    started: list[tuple[bool, bool]] = []
    monkeypatch.setattr(bridge.cascade, "start", lambda f, d: started.append((f, d)))
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_port=7891))
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    bridge.serve()
    assert started == [(False, False)]


def test_serve_skips_start_when_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(bridge, "_running_level", lambda cfg_obj, check: True)
    started: list = []
    monkeypatch.setattr(bridge.cascade, "start", lambda *a: started.append(a))
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_port=7891))
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    bridge.serve()
    assert started == []


def test_daemonize_delegates_to_daemon_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kwargs_called: dict[str, object] = {}

    def fake_start(**kw: object) -> None:
        kwargs_called.update(kw)

    pid_path = tmp_path / "x.pid"
    monkeypatch.setattr(daemon, "start", fake_start)
    monkeypatch.setattr(bridge.cfg, "BRIDGE_PID_PATH", pid_path)

    bridge.daemonize()

    assert kwargs_called["name"] == "bridge"
    assert kwargs_called["pid_path"] == pid_path
    assert "log_path" not in kwargs_called
    assert "rotate_log" not in kwargs_called


def test_stop_daemon_delegates_to_daemon_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called: list[Path] = []

    def fake_stop(path: Path) -> bool:
        called.append(path)
        return True

    pid_path = tmp_path / "x.pid"
    monkeypatch.setattr(daemon, "stop", fake_stop)
    monkeypatch.setattr(bridge.cfg, "BRIDGE_PID_PATH", pid_path)

    assert bridge.stop_daemon() is True
    assert called == [pid_path]


def test_running_level_combinations() -> None:
    assert bridge._running_level(_cfg(), lambda c: True) is False
    conf = Config(active_level=ActiveLevel.NODE)
    assert bridge._running_level(conf, lambda c: True) is True
    assert bridge._running_level(conf, lambda c: False) is False


def test_warn_if_exposed_silent_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    bridge.warn_if_exposed("127.0.0.1")
    bridge.warn_if_exposed("localhost")
    assert logged == []


def test_warn_if_exposed_warns_on_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[str] = []
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    bridge.warn_if_exposed("0.0.0.0")
    bridge.warn_if_exposed("192.168.1.20")
    assert len(logged) == 2
    assert all("SECURITY" in msg for msg in logged)


def test_serve_warns_when_binding_beyond_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            pass

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    logged: list[str] = []
    monkeypatch.setattr(bridge, "_running_level", lambda cfg_obj, check: True)
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_port=7891))
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(bridge.cfg, "listen_address", lambda: "0.0.0.0")
    monkeypatch.setattr(bridge, "Bridge", FakeServer)

    bridge.serve()

    assert any("SECURITY" in msg for msg in logged)


class _FakeStdout:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_serve_hides_ctrl_c_tip_when_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    printed: list[str] = []
    console_printed: list[str] = []
    monkeypatch.setattr(bridge, "_running_level", lambda cfg_obj, check: True)
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_port=7891))
    monkeypatch.setattr(bridge, "_log", lambda msg: printed.append(msg))
    monkeypatch.setattr(bridge.sys, "stdout", _FakeStdout(False))
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    monkeypatch.setattr(bridge, "console_echo", lambda: None)
    monkeypatch.setattr(
        bridge.console, "print", lambda *a, **k: console_printed.append(a[0])
    )

    bridge.serve()

    assert not any("Ctrl-C to stop." in msg for msg in printed)
    assert not any("Ctrl-C to stop." in msg for msg in console_printed)
    assert any("stopping bridge" in msg for msg in console_printed)


def test_serve_shows_ctrl_c_tip_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    printed: list[str] = []
    console_printed: list[str] = []
    monkeypatch.setattr(bridge, "_running_level", lambda cfg_obj, check: True)
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_port=7891))
    monkeypatch.setattr(bridge, "_log", lambda msg: printed.append(msg))
    monkeypatch.setattr(bridge.sys, "stdout", _FakeStdout(True))
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    monkeypatch.setattr(bridge, "console_echo", lambda: None)
    monkeypatch.setattr(
        bridge.console, "print", lambda *a, **k: console_printed.append(a[0])
    )

    bridge.serve()

    assert any("Ctrl-C to stop." in msg for msg in console_printed)
    assert any("stopping bridge" in msg for msg in console_printed)


def test_respond_streams_body_and_skips_hop_by_hop() -> None:
    from io import BytesIO
    from types import SimpleNamespace

    sent_headers: list[tuple[str, str]] = []

    def send_header(key: str, value: str) -> None:
        sent_headers.append((key, value))

    wfile = BytesIO()
    handler = SimpleNamespace(
        send_response=lambda code: None,
        send_header=send_header,
        end_headers=lambda: None,
        wfile=wfile,
    )
    resp = httpx.Response(
        200,
        headers={"Content-Type": "text/plain", "Connection": "keep-alive"},
        content=b"hello",
    )
    bridge._respond(handler, resp)
    assert wfile.getvalue() == b"hello"
    assert not any(k.lower() == "connection" for k, _ in sent_headers)


def test_respond_survives_broken_pipe() -> None:
    from types import SimpleNamespace

    class BoomWfile:
        def write(self, chunk: bytes) -> None:
            raise BrokenPipeError

    handler = SimpleNamespace(
        send_response=lambda code: None,
        send_header=lambda *a: None,
        end_headers=lambda: None,
        wfile=BoomWfile(),
    )
    resp = httpx.Response(200, content=b"data")
    bridge._respond(handler, resp)


def test_handler_returns_502_on_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    status: list[int] = []
    written: list[bytes] = []
    headers: list[tuple[str, str]] = []

    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.headers = SimpleNamespace(get=lambda k, d=0: 0, items=list)
    handler.rfile = SimpleNamespace(read=lambda n: b"")
    handler.path = "/v1/chat/completions"
    handler.wfile = SimpleNamespace(write=written.append)
    handler.send_response = lambda code: status.append(code)
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None

    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg())

    def boom(cfg_obj: object, request: object, **kwargs: object) -> object:
        raise bridge.UpstreamError("up boom")

    monkeypatch.setattr(bridge, "forward", boom)
    handler._handle("POST")
    assert status == [502]
    assert b'"upstream"' in written[0]


def test_handler_logs_request_elapsed_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    body = b'{"model":"gpt-4o","messages":[{"content":"hello world"}]}'
    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.headers = SimpleNamespace(get=lambda k, d=0: len(body), items=list)
    handler.rfile = SimpleNamespace(read=lambda n: body)
    handler.path = "/v1/chat/completions"
    handler.wfile = SimpleNamespace(write=lambda b: None)
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    logged: list[str] = []
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg())
    monkeypatch.setattr(bridge, "_respond", lambda handler, resp: None)
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))

    resp = httpx.Response(200, content=b"ok")
    client = _FakeClient([resp])

    def fake_forward(cfg_obj: object, request: object, **kw: object) -> object:
        return (resp, client)

    monkeypatch.setattr(bridge, "forward", fake_forward)
    handler._handle("POST")

    line = next(msg for msg in logged if msg.startswith("[bridge] POST"))
    assert "gpt-4o" in line
    assert "model=gpt-4o" not in line
    assert "input=" in line
    assert any(k in line for k in ("ms", "s"))


def test_handler_rotates_and_retries_after_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.headers = SimpleNamespace(get=lambda k, d=0: 0, items=list)
    handler.rfile = SimpleNamespace(read=lambda n: b"")
    handler.path = "/v1/chat/completions"
    handler.wfile = SimpleNamespace(write=lambda b: None)
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    logged: list[str] = []
    rotated: list = []
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg())
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(bridge, "_respond", lambda handler, resp: None)
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: (rotated.append(1), True)[1])

    resp = httpx.Response(200, content=b"ok")
    client = _FakeClient([resp])
    calls: list[bool] = []

    def flaky_forward(cfg_obj: object, request: object, **kw: object) -> object:
        calls.append(kw.get("rotate_on_429", True))
        if not calls or len(calls) == 1:
            raise bridge.UpstreamError("conn drop")
        return (resp, client)

    monkeypatch.setattr(bridge, "forward", flaky_forward)
    handler._handle("POST")
    assert len(rotated) == 1
    assert any("rotation took" in msg for msg in logged)
    assert any(
        msg.startswith("[bridge] POST") and ("ms" in msg or "s" in msg)
        for msg in logged
    )


def test_bridge_server_binds_and_closes() -> None:
    server = bridge.Bridge("127.0.0.1", 0)
    try:
        assert server.server_address[1] > 0
    finally:
        server.server_close()
