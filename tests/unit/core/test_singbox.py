from typing import Any, Self

import pytest

from openrot.core.singbox import (
    generate_free_config,
    generate_singbox_config,
    probe_vless,
)
from openrot.providers.vless import VlessNode


def _vless(**overrides: Any) -> VlessNode:
    defaults = {
        "address": "example.com",
        "port": 443,
        "uuid": "11111111-2222-3333-4444-555555555555",
    }
    defaults.update(overrides)
    return VlessNode(**defaults)


def test_relay_config_tls() -> None:
    data = generate_singbox_config(_vless(tls=True, servername="example.com"), 7890)
    inbound = data["inbounds"][0]
    assert inbound["type"] == "mixed"
    assert inbound["listen_port"] == 7890
    out = data["outbounds"][0]
    assert out["type"] == "vless"
    assert out["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert out["tls"]["enabled"] is True
    assert out["tls"]["server_name"] == "example.com"


def test_relay_config_ws_transport() -> None:
    data = generate_singbox_config(_vless(network="ws", tls=True), 7890)
    transport = data["outbounds"][0]["transport"]
    assert transport["type"] == "ws"
    assert transport["path"] == "/"
    assert transport["headers"]["Host"] == "example.com"


def test_relay_config_reality() -> None:
    node = _vless(
        security="reality",
        tls=True,
        servername="mynl.api.dznn.net",
        public_key="abcdefghijklmnop",
        short_id="1abc",
    )
    data = generate_singbox_config(node, 7890)
    reality = data["outbounds"][0]["tls"]["reality"]
    assert reality["enabled"] is True
    assert reality["public_key"] == "abcdefghijklmnop"
    assert reality["short_id"] == "1abc"


def test_plain_relay_has_no_tls_key() -> None:
    data = generate_singbox_config(_vless(), 7890)
    assert "tls" not in data["outbounds"][0]


def test_free_config_http() -> None:
    data = generate_free_config("http", "1.2.3.4", 8080, 7890)
    assert data["inbounds"][0]["listen_port"] == 7890
    assert data["inbounds"][0]["listen"] == "127.0.0.1"
    out = data["outbounds"][0]
    assert out["type"] == "http"
    assert out["server"] == "1.2.3.4"
    assert out["server_port"] == 8080


def test_free_config_listen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROT_LISTEN", "0.0.0.0")
    data = generate_free_config("http", "1.2.3.4", 8080, 7890)
    assert data["inbounds"][0]["listen"] == "0.0.0.0"


def test_relay_config_listen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROT_LISTEN", "0.0.0.0")
    data = generate_singbox_config(_vless(), 7890)
    assert data["inbounds"][0]["listen"] == "0.0.0.0"


def test_free_config_socks5() -> None:
    data = generate_free_config("socks5", "2.2.2.2", 1080, 7891)
    assert data["outbounds"][0]["type"] == "socks"


class _RunningProc:
    def poll(self) -> None:
        return None

    returncode = 0

    def terminate(self) -> None:
        pass


class _ExitedProc:
    def poll(self) -> int:
        return 3

    returncode = 3

    def terminate(self) -> None:
        pass


class _FakeResponse:
    status_code = 204


class _FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def get(self, url: str) -> _FakeResponse:
        return _FakeResponse()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def test_probe_vless_waits_for_readiness_then_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openrot.core.singbox as sb

    monkeypatch.setattr(sb, "_free_port", lambda: 12345)
    monkeypatch.setattr(sb, "write_config", lambda data, prefix="": _node_probe_conf())
    monkeypatch.setattr(sb, "_wait_for_port", lambda h, p, t: True)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: _RunningProc())
    monkeypatch.setattr(sb.httpx, "Client", _FakeClient)

    alive, latency = probe_vless(_vless(), "sing-box", timeout=5.0)
    assert alive is True
    assert latency is not None


def test_probe_vless_logs_stderr_when_singbox_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openrot.core.singbox as sb

    warnings: list[tuple[object, ...]] = []

    class _FakeLogger:
        def warning(self, *args: object) -> None:
            warnings.append(args)

    monkeypatch.setattr(sb, "_free_port", lambda: 12346)
    monkeypatch.setattr(sb, "write_config", lambda data, prefix="": _node_probe_conf())
    monkeypatch.setattr(sb, "_wait_for_port", lambda h, p, t: False)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda *a, **k: _ExitedProc())
    monkeypatch.setattr(
        sb,
        "log",
        type(
            "_L",
            (),
            {"get_logger": staticmethod(lambda: _FakeLogger())},
        )(),
    )

    alive, latency = probe_vless(_vless(), "sing-box", timeout=5.0)
    assert alive is False
    assert latency is None
    assert len(warnings) == 1
    message, rc = warnings[0][0], warnings[0][1]
    assert "sing-box exited during probe" in message
    assert rc == 3


def _node_probe_conf() -> object:
    class _Conf:
        def unlink(self, missing_ok: bool = False) -> None:
            return None

    return _Conf()
