"""Tests for the bridge reverse-proxy: 429 -> rotate + retry, and lifecycle.

The file historically also housed the ``start bridge`` helpers; those were
merged into ``openrot.core.bridge``, and their tests live here too.
"""

import gzip
import json
import re
import socket
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import httpx
import pytest

from openrot.config import ActiveLevel, Config
from openrot.core import bridge, daemon


def _cfg(**kw: object) -> Config:
    defaults: dict[str, object] = {
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


def test_client_proxies_through_warp_socks5(monkeypatch: pytest.MonkeyPatch) -> None:
    """The upstream client talks straight to WARP's SOCKS5 listener."""
    captured: dict[str, object] = {}

    def fake_make_client(**kw: object) -> None:
        captured.update(kw)

    monkeypatch.setattr(bridge.warp, "proxy_address", lambda: ("127.0.0.1", 40000))
    monkeypatch.setattr(bridge, "make_client", fake_make_client)

    bridge._client(_cfg())

    assert captured["proxy"] == "socks5://127.0.0.1:40000"
    assert captured["timeout"] == 300


def _usage_limit_body() -> bytes:
    """Return a JSON body that signals FreeUsageLimitError."""
    return b'{"type":"error","error":{"type":"FreeUsageLimitError","message":"quota"}}'


def test_is_usage_limit_error() -> None:
    assert bridge._is_usage_limit_error(
        httpx.Response(429, content=_usage_limit_body())
    )
    assert not bridge._is_usage_limit_error(
        httpx.Response(200, content=_usage_limit_body())
    )
    assert not bridge._is_usage_limit_error(httpx.Response(429))
    assert not bridge._is_usage_limit_error(
        httpx.Response(429, content=b'{"error":{"type":"RateLimitError"}}')
    )
    assert not bridge._is_usage_limit_error(httpx.Response(429, content=b"not json"))


def test_is_usage_limit_error_reads_streamed_response() -> None:
    """A streamed (unread) 429 must not raise ResponseNotRead from ``.text``.

    ``_fetch`` sends with ``stream=True``, so the body is unreadable until
    ``read()`` — this previously escaped into the handler and cost the client
    a 502 instead of a usage-limit rotation.
    """

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> object:
            yield _usage_limit_body()

        def close(self) -> None:
            pass

    resp = httpx.Response(429, stream=Stream())
    with pytest.raises(httpx.ResponseNotRead):
        _ = resp.text  # reproduces the original bug when read() is skipped

    resp = httpx.Response(429, stream=Stream())
    assert bridge._is_usage_limit_error(resp) is True
    # the buffered body is still available to _respond's iter_bytes()
    assert b"".join(resp.iter_bytes()) == _usage_limit_body()
    # non-429 stays untouched so the 200 path keeps streaming
    untouched = httpx.Response(200, stream=Stream())
    assert bridge._is_usage_limit_error(untouched) is False
    assert untouched.is_stream_consumed is False


def test_retry_delay_from_header() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "30"})
    assert bridge._retry_delay(resp) == 30.0


def test_retry_delay_default() -> None:
    resp = httpx.Response(429)
    assert bridge._retry_delay(resp) == 5.0


def test_forward_headers_strips_hop_by_hop_and_sets_host() -> None:
    headers = {"Host": "x", "Connection": "keep-alive", "Authorization": "Bearer k"}
    out = bridge._forward_headers(headers, "up.test")
    assert out["Host"] == "up.test"
    assert "Connection" not in out
    assert out["Authorization"] == "Bearer k"


def test_forward_headers_sets_canonical_session_on_all_headers() -> None:
    out = bridge._forward_headers({}, "up.test")
    session = out["X-Opencode-Session"]
    assert session.startswith("ses_")
    time_part = session[len("ses_") :][:12]
    assert re.fullmatch(r"[0-9a-f]{12}", time_part)
    # descending encoding: counter 1 -> low 12 bits fc0-fff
    assert re.fullmatch(r"[0-9a-f]{9}f[c-f][0-9a-f]", time_part)
    tail = session[len("ses_") + 12 :]
    assert len(tail) == 14 and tail.isalnum()
    for name in ("X-Opencode-Session-Id", "X-Session-Affinity", "X-Session-Id"):
        assert out[name] == session


def test_forward_headers_keeps_canonical_client_session() -> None:
    canonical = "ses_ec40f42c4ffeAOIUMQQVMQTCUU"
    out = bridge._forward_headers({"X-Opencode-Session": canonical}, "up.test")
    assert out["X-Opencode-Session"] == canonical
    assert out["X-Opencode-Session-Id"] == canonical


def test_forward_headers_reencodes_foreign_session_deterministically() -> None:
    a = bridge._forward_headers({"X-Opencode-Session": "uuid-1234"}, "up.test")
    b = bridge._forward_headers({"X-Opencode-Session": "uuid-1234"}, "up.test")
    assert a["X-Opencode-Session"] == b["X-Opencode-Session"]
    assert a["X-Opencode-Session"].startswith("ses_")


def test_forward_headers_drops_legacy_request_header() -> None:
    out = bridge._forward_headers({"X-Opencode-Request": "msg_abc"}, "up.test")
    assert "X-Opencode-Request" not in out
    assert not any(k.lower() == "x-opencode-request" for k in out)


def test_forward_headers_sets_project_client_and_auth_when_absent() -> None:
    out = bridge._forward_headers({}, "up.test")
    assert re.fullmatch(r"[0-9a-f]{40}", out["X-Opencode-Project"])
    assert out["X-Opencode-Client"] == "cli"
    assert out["Authorization"] == "Bearer public"
    assert out["Accept"] == "application/json, text/event-stream"


def test_forward_headers_keeps_existing_project_and_auth() -> None:
    headers = {"X-Opencode-Project": "global", "Authorization": "Bearer sk-x"}
    out = bridge._forward_headers(headers, "up.test")
    assert out["X-Opencode-Project"] == "global"
    assert out["Authorization"] == "Bearer sk-x"


def test_forward_headers_injects_opencode_user_agent_when_absent() -> None:
    out = bridge._forward_headers({}, "up.test")
    assert out["User-Agent"] == bridge._OPENCODE_UA


def test_forward_headers_overrides_non_opencode_user_agent() -> None:
    out = bridge._forward_headers({"User-Agent": "curl/8.0"}, "up.test")
    assert out["User-Agent"] == bridge._OPENCODE_UA
    assert sum(k.lower() == "user-agent" for k in out) == 1


def test_forward_headers_keeps_opencode_user_agent() -> None:
    ua = "opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"
    out = bridge._forward_headers({"User-Agent": ua}, "up.test")
    assert out["User-Agent"] == ua


def test_canonical_session_id_reencodes_non_session_seed() -> None:
    value = bridge._canonical_session_id("some-random-signal")
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", value)


def test_normalize_body_forces_stream_and_usage() -> None:
    out = json.loads(bridge._normalize_body(b'{"model": "m", "messages": []}'))
    assert out["stream"] is True
    assert out["stream_options"]["include_usage"] is True


def test_normalize_body_injects_core_tools_when_missing() -> None:
    body = json.dumps(
        {
            "model": "m",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode()
    out = json.loads(bridge._normalize_body(body))
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == bridge.CORE_TOOL_NAMES
    assert out["tools"][0]["function"]["description"]  # genuine definition


def test_normalize_body_replaces_recognised_stub_with_genuine() -> None:
    body = json.dumps(
        {
            "model": "m",
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "read",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
    ).encode()
    out = json.loads(bridge._normalize_body(body))
    assert out["tools"][0] == bridge.GENUINE_TOOLS["read"]


def _title_body(system: str) -> dict:
    return {
        "model": "m",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "hi"},
        ],
    }


def test_normalize_body_rewrites_opencode_title_prompt() -> None:
    original = (
        "You are a title generator. You output ONLY a thread title. "
        "Nothing else.\n\n<task>\nGenerate a brief title...\n</task>\n\n"
        "<rules>\n- you MUST use the same language...\n</rules>\n\n"
        '<examples>\n"debug 500 errors" -> Debugging 500 errors\n</examples>'
    )
    out = json.loads(bridge._normalize_body(json.dumps(_title_body(original)).encode()))
    system = out["messages"][0]["content"]
    assert system == bridge.TITLE_SYSTEM_PROMPT
    assert "title generator" not in system[:80].lower()


def test_normalize_body_leaves_unrelated_system_prompt() -> None:
    system = "You are opencode, an interactive CLI tool."
    out = json.loads(bridge._normalize_body(json.dumps(_title_body(system)).encode()))
    assert out["messages"][0]["content"] == system


def test_normalize_body_title_rewrite_is_idempotent() -> None:
    original = "You are a title generator. You output ONLY a thread title."
    once = bridge._normalize_body(json.dumps(_title_body(original)).encode())
    twice = bridge._normalize_body(once)
    assert once == twice


def test_normalize_title_prompt_ignores_non_string_content() -> None:
    data = {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "title generator"}],
            },
            {"role": "user", "content": "hi"},
        ]
    }
    assert bridge._normalize_title_prompt(data) is False


def test_normalize_body_keeps_unknown_tools_and_appends_missing_core() -> None:
    body = json.dumps(
        {
            "model": "m",
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "my_custom_tool",
                        "description": "d",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
    ).encode()
    out = json.loads(bridge._normalize_body(body))
    names = [t["function"]["name"] for t in out["tools"]]
    assert names[0] == "my_custom_tool"
    assert out["tools"][0]["function"]["description"] == "d"
    assert set(bridge.CORE_TOOL_NAMES) <= set(names)


def test_normalize_body_returns_compliant_body_byte_identical() -> None:
    body = json.dumps(
        {
            "model": "m",
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": [bridge.GENUINE_TOOLS[n] for n in bridge.CORE_TOOL_NAMES],
        }
    ).encode()
    assert bridge._normalize_body(body) == body


def test_normalize_body_leaves_invalid_and_empty_bodies() -> None:
    assert bridge._normalize_body(b"") == b""
    assert bridge._normalize_body(b"not json") == b"not json"


def test_forward_returns_upstream_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient([httpx.Response(200, content=b"ok")])
    monkeypatch.setattr(bridge, "_get_client", lambda cfg: fake)
    bridge._shared_client = None
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


def test_fetch_forces_accept_encoding_identity() -> None:
    captured: dict[str, object] = {}

    class SpyClient:
        def build_request(self, method: str, url: str, **kw: object) -> httpx.Request:
            return httpx.Request(method, url, **kw)

        def send(
            self, request: httpx.Request, *, stream: bool = True
        ) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=b"ok")

    bridge._fetch(SpyClient(), _cfg(), _req())
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers.get("accept-encoding") == "identity"


def test_forward_rotates_and_retries_on_usage_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: list[httpx.Response] = [
        httpx.Response(429, content=_usage_limit_body()),
        httpx.Response(200, content=b"recovered"),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_get_client", make_client)
    bridge._shared_client = None
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


def test_forward_waits_on_regular_429_without_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: list[httpx.Response] = [
        httpx.Response(429),  # no FreeUsageLimitError body
        httpx.Response(200, content=b"recovered"),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_get_client", make_client)
    bridge._shared_client = None
    rotated: list = []
    logged: list[str] = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: (rotated.append(1), True)[1])
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(bridge, "time", bridge.time)  # real time.sleep

    resp, client = bridge.forward(_cfg(), _req())
    try:
        assert rotated == []
        assert resp.status_code == 200
        assert resp.content == b"recovered"
    finally:
        client.close()
    assert any("rate limited" in msg for msg in logged)
    assert not any("rotation" in msg for msg in logged)


def test_forward_no_rotate_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge, "_get_client", lambda cfg: _FakeClient([httpx.Response(429)])
    )
    bridge._shared_client = None
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
    body = _usage_limit_body()
    queue: list[httpx.Response] = [
        httpx.Response(429, content=body),
        httpx.Response(429, content=body),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_get_client", make_client)
    bridge._shared_client = None
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

    monkeypatch.setattr(bridge, "_get_client", make_client)
    bridge._shared_client = None
    rotated: list = []
    monkeypatch.setattr(bridge.cascade, "rotate", lambda: rotated.append(1))
    logged: list[str] = []
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))

    resp, client = bridge.forward(_cfg(bridge_retry_statuses=[503]), _req())
    try:
        assert rotated == []
        assert resp.status_code == 200
    finally:
        client.close()
    assert any("rate limited" in msg for msg in logged)


def test_forward_ignores_non_configured_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge, "_get_client", lambda cfg: _FakeClient([httpx.Response(503)])
    )
    bridge._shared_client = None
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
    body = _usage_limit_body()
    queue: list[httpx.Response] = [
        httpx.Response(429, content=body),
        httpx.Response(429, content=body),
        httpx.Response(429, content=body),
        httpx.Response(200),
    ]
    fake = _FakeClient(queue)

    def make_client(cfg: Config) -> _FakeClient:
        return fake

    monkeypatch.setattr(bridge, "_get_client", make_client)
    bridge._shared_client = None
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
        bridge, "_get_client", lambda cfg: _FakeClient([httpx.Response(429)])
    )
    bridge._shared_client = None
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
        def build_request(self, method: str, url: str, **kw: object) -> httpx.Request:
            return httpx.Request(method, url, **kw)

        def send(
            self, request: httpx.Request, *, stream: bool = True
        ) -> httpx.Response:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(bridge, "_get_client", lambda cfg: BoomClient())
    bridge._shared_client = None
    with pytest.raises(httpx.HTTPError):
        bridge.forward(_cfg(), _req())


def test_shared_client_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge._shared_client = None
    fake = _FakeClient([httpx.Response(200), httpx.Response(200)])
    monkeypatch.setattr(bridge, "_client", lambda cfg: fake)

    _, c1 = bridge.forward(_cfg(), _req())
    _, c2 = bridge.forward(_cfg(), _req())
    assert c1 is c2
    c1.close()


def test_reset_client_closes_and_discards() -> None:
    fake = _FakeClient([httpx.Response(200)])
    bridge._shared_client = fake  # type: ignore[assignment]
    bridge._reset_client()
    assert bridge._shared_client is None
    assert fake.closed


def test_send_502(monkeypatch: pytest.MonkeyPatch) -> None:
    status_codes: list[int] = []
    headers: list[tuple[str, str]] = []

    handler = SimpleNamespace(
        send_response=lambda code: status_codes.append(code),
        send_header=lambda k, v: headers.append((k, v)),
        end_headers=lambda: None,
        wfile=BytesIO(),
    )
    # Capture wfile writes

    class FakeWfile:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, chunk: bytes) -> None:
            self.data.extend(chunk)

    handler.wfile = FakeWfile()  # type: ignore[assignment]
    bridge._send_502(handler, "boom")
    assert status_codes == [502]
    assert any(k == "Content-Type" for k, _ in headers)
    payload = bytes(handler.wfile.data)  # type: ignore[attr-defined]
    assert b"boom" in payload


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

    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: False)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    started: list[str] = []
    monkeypatch.setattr(
        bridge.cascade, "start", lambda: started.append("start") or True
    )
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    bridge.serve()
    assert started == ["start"]


def test_serve_keeps_serving_when_warp_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed warp connect logs a warning but the bridge keeps serving."""

    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    console_printed: list[str] = []
    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: False)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    monkeypatch.setattr(bridge.cascade, "start", lambda: False)
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.NONE),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    monkeypatch.setattr(
        bridge.console, "print", lambda *a, **k: console_printed.append(str(a[0]))
    )

    bridge.serve()

    assert any("warp unavailable" in msg for msg in console_printed)


def test_serve_skips_start_when_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: True)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    started: list[str] = []
    monkeypatch.setattr(bridge.cascade, "start", lambda: started.append("start"))
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    bridge.serve()
    assert started == []


def test_serve_starts_background_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """serve() always starts the cascade background loops (health + scheduler)."""

    class FakeServer:
        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: True)
    monkeypatch.setattr(bridge.cascade, "start", lambda: True)
    background_calls: list[int] = []
    monkeypatch.setattr(
        bridge.cascade, "background", lambda: background_calls.append(1)
    )
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "Bridge", FakeServer)

    bridge.serve()

    assert background_calls == [1]


def test_serve_exits_when_port_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: True)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    monkeypatch.setattr(bridge.cascade, "start", lambda: started.append("start"))
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: True)

    with pytest.raises(SystemExit) as exc:
        bridge.serve()

    assert exc.value.code == 1
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
    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: True)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(bridge.cfg, "listen_address", lambda: "0.0.0.0")
    monkeypatch.setattr(bridge, "Bridge", FakeServer)

    bridge.serve()

    assert any("SECURITY" in msg for msg in logged)


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
    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: True)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "_log", lambda msg: printed.append(msg))
    monkeypatch.setattr(bridge.sys, "stdout", _FakeStdout(False))
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
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
    monkeypatch.setattr(bridge.cascade, "level_serving", lambda cfg_obj: True)
    monkeypatch.setattr(bridge.cascade, "background", lambda: None)
    monkeypatch.setattr(
        bridge.cfg,
        "load_config",
        lambda: _cfg(bridge_port=7891, active_level=ActiveLevel.WARP),
    )
    monkeypatch.setattr(bridge, "port_in_use", lambda h, p: False)
    monkeypatch.setattr(bridge, "_log", lambda msg: printed.append(msg))
    monkeypatch.setattr(bridge.sys, "stdout", _FakeStdout(True))
    monkeypatch.setattr(bridge, "Bridge", FakeServer)
    monkeypatch.setattr(
        bridge.console, "print", lambda *a, **k: console_printed.append(a[0])
    )

    bridge.serve()

    assert any("Ctrl-C to stop." in msg for msg in console_printed)
    assert any("stopping bridge" in msg for msg in console_printed)


def test_respond_streams_body_and_skips_hop_by_hop() -> None:
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
        headers={
            "Content-Type": "text/plain",
            "Content-Length": "5",
            "Connection": "keep-alive",
        },
        content=b"hello",
    )
    bridge._respond(handler, resp)
    assert wfile.getvalue() == b"hello"
    assert not any(k.lower() == "connection" for k, _ in sent_headers)
    assert any(h.lower() == "content-length" and v == "5" for h, v in sent_headers)


def test_respond_chunks_when_no_content_length() -> None:
    sent_headers: list[tuple[str, str]] = []

    def send_header(key: str, value: str) -> None:
        sent_headers.append((key, value))

    handler = SimpleNamespace(
        send_response=lambda code: None,
        send_header=send_header,
        end_headers=lambda: None,
        wfile=BytesIO(),
    )
    resp = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content=b"data: x\n\n",
    )
    resp.headers.pop("content-length")
    bridge._respond(handler, resp)
    assert bytes(handler.wfile.getvalue()) == b"9\r\ndata: x\n\n\r\n0\r\n\r\n"
    assert any(
        k.lower() == "transfer-encoding" and v == "chunked" for k, v in sent_headers
    )


def test_respond_skips_content_encoding_when_body_decoded() -> None:
    sent_headers: list[tuple[str, str]] = []

    def send_header(key: str, value: str) -> None:
        sent_headers.append((key, value))

    handler = SimpleNamespace(
        send_response=lambda code: None,
        send_header=send_header,
        end_headers=lambda: None,
        wfile=BytesIO(),
    )
    resp = httpx.Response(
        200,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "Content-Length": "123",
        },
        content=gzip.compress(b'{"ok":true}'),
    )
    bridge._respond(handler, resp)
    assert bytes(handler.wfile.getvalue()) == b'b\r\n{"ok":true}\r\n0\r\n\r\n'
    assert not any(k.lower() == "content-encoding" for k, _ in sent_headers)
    assert not any(k.lower() == "content-length" for k, _ in sent_headers)


def test_respond_survives_broken_pipe() -> None:
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


def test_log_error_body_skips_200(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple] = []
    monkeypatch.setattr(bridge.events, "warning", lambda *a: logged.append(a))
    bridge._log_error_body(200, b"ok")
    assert logged == []


def test_log_error_body_logs_full_body(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple] = []
    monkeypatch.setattr(bridge.events, "warning", lambda *a: logged.append(a))
    bridge._log_error_body(429, b'{"error":{"type":"FreeUsageLimitError"}}')
    assert logged == [
        (
            "[bridge] status=%s body: %s",
            429,
            '{"error":{"type":"FreeUsageLimitError"}}',
        )
    ]


def test_respond_logs_full_body_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple] = []
    monkeypatch.setattr(bridge.events, "warning", lambda *a: logged.append(a))
    handler = SimpleNamespace(
        send_response=lambda code: None,
        send_header=lambda *a: None,
        end_headers=lambda: None,
        wfile=BytesIO(),
    )
    resp = httpx.Response(429, content=b'{"error":"quota"}')
    bridge._respond(handler, resp)
    assert logged == [("[bridge] status=%s body: %s", 429, '{"error":"quota"}')]


def test_respond_does_not_log_body_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple] = []
    monkeypatch.setattr(bridge.events, "warning", lambda *a: logged.append(a))
    handler = SimpleNamespace(
        send_response=lambda code: None,
        send_header=lambda *a: None,
        end_headers=lambda: None,
        wfile=BytesIO(),
    )
    resp = httpx.Response(200, content=b"ok")
    bridge._respond(handler, resp)
    assert logged == []


def test_handler_returns_502_on_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_min_interval=0))

    def boom(cfg_obj: object, request: object, **kwargs: object) -> object:
        raise httpx.HTTPError("up boom")

    monkeypatch.setattr(bridge, "forward", boom)
    handler._handle("POST")
    assert status == [502]
    assert b'"upstream"' in written[0]


def test_fail_upstream_logs_full_502_body(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple] = []
    monkeypatch.setattr(bridge.events, "warning", lambda *a: logged.append(a))
    monkeypatch.setattr(
        bridge.BridgeHandler, "_log_request_console", lambda self, method: None
    )

    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.wfile = SimpleNamespace(write=lambda b: None)
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    handler._fail_upstream("POST", 0.0, httpx.HTTPError("up boom"))

    assert logged == [
        (
            "[bridge] status=%s body: %s",
            502,
            '{"error": {"message": "up boom", "type": "upstream"}}',
        )
    ]


def test_handler_logs_request_elapsed_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_min_interval=0))
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


def test_respond_counts_streamed_output_bytes() -> None:
    """output= reflects real bytes written, not (absent) content-length."""
    written: list[bytes] = []

    class _Wfile:
        def write(self, chunk: bytes) -> int:
            written.append(chunk)
            return len(chunk)

        def flush(self) -> None:
            pass

    class _Handler:
        wfile = _Wfile()

        def send_response(self, code: int) -> None:
            pass

        def send_header(self, key: str, value: str) -> None:
            pass

        def end_headers(self) -> None:
            pass

    resp = httpx.Response(200, content=iter([b"abc", b"de"]), headers={})
    handler = _Handler()
    assert bridge._respond(handler, resp) == 5
    assert written  # body went through wfile


def test_handler_logs_status_and_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"model":"gpt-4o","messages":[{"content":"hi"}]}'
    req_id = "ses_abc123/msg_a1b2c3d4e5f6g7h8i9j0"
    ses_id = "ses_01234567890123456789012345"
    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.headers = SimpleNamespace(
        get=lambda k, d="": len(body) if k == "Content-Length" else d,
        items=lambda: [("x-opencode-request", req_id), ("x-opencode-session", ses_id)],
    )
    handler.rfile = SimpleNamespace(read=lambda n: body)
    handler.path = "/v1/chat/completions"
    handler.wfile = SimpleNamespace(write=lambda b: None)
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    logged: list[str] = []
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_min_interval=0))
    monkeypatch.setattr(bridge, "_respond", lambda handler, resp: 1500)
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))

    resp = httpx.Response(429, content=b"x")
    client = _FakeClient([resp])

    def fake_forward(cfg_obj: object, request: object, **kw: object) -> object:
        return (resp, client)

    monkeypatch.setattr(bridge, "forward", fake_forward)
    handler._handle("POST")

    line = next(msg for msg in logged if msg.startswith("[bridge] POST"))
    assert "output=1.5k" in line
    assert "status=429" in line
    assert "req=msg_a1b2c3d4e5f6g7h8i9j0" in line
    assert f"ses={ses_id}" in line


def test_handler_retries_without_rotation_after_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
    handler.headers = SimpleNamespace(get=lambda k, d=0: 0, items=list)
    handler.rfile = SimpleNamespace(read=lambda n: b"")
    handler.path = "/v1/chat/completions"
    handler.wfile = SimpleNamespace(write=lambda b: None)
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None

    logged: list[str] = []
    warning_called = {"n": 0}
    monkeypatch.setattr(bridge.cfg, "load_config", lambda: _cfg(bridge_min_interval=0))
    monkeypatch.setattr(bridge, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(
        bridge.events, "warning", lambda msg, *a: warning_called.__setitem__("n", 1)
    )
    monkeypatch.setattr(bridge, "_respond", lambda handler, resp: None)

    resp = httpx.Response(200, content=b"ok")
    client = _FakeClient([resp])
    calls: list[bool] = []

    def flaky_forward(cfg_obj: object, request: object, **kw: object) -> object:
        calls.append(kw.get("rotate_on_429", True))
        if not calls or len(calls) == 1:
            raise httpx.HTTPError("conn drop")
        return (resp, client)

    monkeypatch.setattr(bridge, "forward", flaky_forward)
    handler._handle("POST")
    assert warning_called["n"] == 1
    assert len(calls) == 2
    assert calls[0] is True
    assert calls[1] is False
    assert not any("rotation" in msg for msg in logged)


def test_pace_start_first_call_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge._last_start_monotonic = 0.0
    sleeps: list[float] = []
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 100.0)
    bridge._pace_start(_cfg(bridge_min_interval=0.5))
    assert sleeps == []
    assert bridge._last_start_monotonic == 100.0


def test_pace_start_waits_remaining_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge._last_start_monotonic = 0.0
    sleeps: list[float] = []
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 0.1)
    bridge._pace_start(_cfg(bridge_min_interval=0.5))
    assert sleeps == [pytest.approx(0.4)]
    assert bridge._last_start_monotonic == 0.1


def test_pace_start_zero_interval_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge._last_start_monotonic = 100.0
    sleeps: list[float] = []
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 100.0)
    bridge._pace_start(_cfg(bridge_min_interval=0))
    assert sleeps == []


def test_pace_start_enforces_gap_between_consecutive_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge._last_start_monotonic = 0.0
    sleeps: list[float] = []
    now = {"t": 100.0}
    monkeypatch.setattr(bridge.time, "sleep", sleeps.append)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now["t"])
    bridge._pace_start(_cfg(bridge_min_interval=0.5))
    assert sleeps == []
    assert bridge._last_start_monotonic == 100.0
    now["t"] = 100.2
    bridge._pace_start(_cfg(bridge_min_interval=0.5))
    assert sleeps == [pytest.approx(0.3)]
    assert bridge._last_start_monotonic == 100.2


def test_bridge_server_binds_and_closes() -> None:
    server = bridge.Bridge("127.0.0.1", 0)
    try:
        assert server.server_address[1] > 0
    finally:
        server.server_close()


class _FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.closed = False
        self.last_url: str | None = None

    def build_request(self, method: str, url: str, **kw: object) -> httpx.Request:
        return httpx.Request(method, url, **kw)

    def send(self, request: httpx.Request, *, stream: bool = True) -> httpx.Response:
        self.last_url = str(request.url)
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _FakeStdout:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty
