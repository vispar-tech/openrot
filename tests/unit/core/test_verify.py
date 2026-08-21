"""Unit tests for the aggregation-time verification pipeline."""

import inspect
from types import SimpleNamespace
from typing import Self

import pytest

from openrot.core import verify
from openrot.models import Node, NodeStatus
from openrot.providers import vless

_RELAY = (
    "vless://f8c2e8a0-0000-4000-8000-000000000001@example.com:443?encryption=none#test"
)
_RELAY2 = (
    "vless://f8c2e8a0-0000-4000-8000-000000000002@other.example:8443?encryption=none#t2"
)


def test_verify_pool_default_limits_cap_to_20() -> None:
    assert verify.TOP_LIMIT == 20
    assert (
        inspect.signature(verify.verify_vless_pool).parameters["limit"].default
        == verify.TOP_LIMIT
    )
    assert (
        inspect.signature(verify.verify_proxy_pool).parameters["limit"].default
        == verify.TOP_LIMIT
    )


def test_verify_progress_reports_each_node(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = [("http", "1.1.1.1", 80), ("socks5", "2.2.2.2", 1080)]
    monkeypatch.setattr(
        verify,
        "tcp_reachable",
        lambda host, port, timeout: host == "1.1.1.1",
    )
    monkeypatch.setattr(
        verify.free, "probe_targets", lambda p, h, port, timeout, url=None: [5.0]
    )

    done: list[tuple[object, object, object]] = []
    verify.verify_proxy_pool(
        candidates, 3.0, on_progress=lambda s, d, t: done.append((s, d, t))
    )
    tcp_done = [(s, d, t) for s, d, t in done if s == "tcp"]
    probe_done = [(s, d, t) for s, d, t in done if s == "probe"]
    assert tcp_done == [("tcp", 1, 2), ("tcp", 2, 2)]
    assert probe_done == [("probe", 1, 1)]


def _parsed(raw: str = _RELAY) -> vless.VlessNode:
    return vless.parse_vless(raw)


class _Ctx:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def wrap_socket(self, sock: object, server_hostname: str | None = None) -> _Ctx:
        return self


def test_median() -> None:
    assert verify.median([]) == 0.0
    assert verify.median([10.0, 20.0, 30.0]) == 20.0
    assert verify.median([10.0, 20.0]) == 15.0


def test_tcp_reachable_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify.socket, "create_connection", lambda host, timeout=0: _Ctx()
    )
    assert verify.tcp_reachable("1.1.1.1", 80, 1.0) is True


def test_tcp_reachable_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(host: str, timeout: float = 0) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(verify.socket, "create_connection", refused)
    assert verify.tcp_reachable("1.1.1.1", 80, 1.0) is False


class _TlsCtx:
    check_hostname = False
    verify_mode = None

    def wrap_socket(self, sock: object, server_hostname: str | None = None) -> _Ctx:
        return _Ctx()


class _TlsFailCtx:
    check_hostname = False
    verify_mode = None

    def wrap_socket(self, sock: object, server_hostname: str | None = None) -> object:
        raise OSError("handshake failed")


def test_tls_handshake_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify.ssl, "create_default_context", lambda: _TlsCtx())
    monkeypatch.setattr(
        verify.socket, "create_connection", lambda host, timeout=0: _Ctx()
    )
    assert verify.tls_handshake("example.com", 443, "example.com", 1.0) is True


def test_tls_handshake_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify.ssl, "create_default_context", lambda: _TlsFailCtx())
    monkeypatch.setattr(
        verify.socket, "create_connection", lambda host, timeout=0: _Ctx()
    )
    assert verify.tls_handshake("example.com", 443, "example.com", 1.0) is False


def test_singbox_check_accepts_and_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    fake_path = tmp_path / "cfg.json"  # type: ignore[operator]
    fake_path.write_text("{}")
    monkeypatch.setattr(
        verify, "write_config", lambda cfg, prefix="openrot-": fake_path
    )
    rc = {"value": 0}

    def fake_run(*a: object, **k: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=rc["value"])

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    node = _parsed()
    assert verify.singbox_check(node, "sing-box") is True
    rc["value"] = 1
    assert verify.singbox_check(node, "sing-box") is False
    assert fake_path.exists() is False


def test_verify_vless_pool_runs_all_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    raws = [
        _RELAY,
        _RELAY,
        "vless://f8c2e8a0-0000-4000-8000-000000000002@other.example:8443?encryption=none#t2",
    ]
    monkeypatch.setattr(
        verify, "tcp_reachable", lambda host, port, timeout: host != "other.example"
    )
    monkeypatch.setattr(verify, "tls_handshake", lambda h, p, sn, timeout: True)
    monkeypatch.setattr(verify, "singbox_check", lambda n, bin: True)
    probe_calls = {"n": 0}

    def fake_probe(
        node: object, bin: str, timeout: float, url: str | None = None
    ) -> list[float]:
        probe_calls["n"] += 1
        return [12.0, 8.0] if probe_calls["n"] == 1 else [30.0]

    monkeypatch.setattr(verify, "_probe_latencies", fake_probe)

    stages: list[tuple[object, object, object]] = []
    result = verify.verify_vless_pool(
        raws,
        "sing-box",
        3.0,
        limit=1,
        on_stage=lambda s, k, t: stages.append((s, k, t)),
    )

    assert ("parse", 2, 3) in stages
    assert ("tcp", 1, 2) in stages
    assert ("probe", 1, 1) in stages
    assert len(result) == 1
    raw, latency = result[0]
    assert raw[0] == _RELAY
    assert latency == 10.0  # median of [12.0, 8.0]


def test_verify_vless_pool_fails_low_success_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "tcp_reachable", lambda host, port, timeout: True)
    monkeypatch.setattr(verify, "tls_handshake", lambda h, p, sn, timeout: True)
    monkeypatch.setattr(verify, "singbox_check", lambda n, bin: True)
    # no target reachable -> below the success gate
    monkeypatch.setattr(
        verify, "_probe_latencies", lambda node, bin, timeout, url=None: []
    )
    assert verify.verify_vless_pool([_RELAY], "sing-box", 3.0) == []


def test_verify_vless_pool_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    stages = []
    result = verify.verify_vless_pool(
        [], "sing-box", 3.0, on_stage=lambda s, k, t: stages.append((s, k, t))
    )
    assert result == []
    assert ("parse", 0, 0) in stages
    assert ("probe", 0, 0) in stages


def test_verify_proxy_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = [("http", "1.1.1.1", 80), ("socks5", "2.2.2.2", 1080)]
    monkeypatch.setattr(
        verify, "tcp_reachable", lambda host, port, timeout: host == "1.1.1.1"
    )
    monkeypatch.setattr(
        verify.free,
        "probe_targets",
        lambda p, h, port, timeout, url=None: [5.0],
    )

    stages: list[tuple[object, object, object]] = []
    result = verify.verify_proxy_pool(
        candidates, 3.0, on_stage=lambda s, k, t: stages.append((s, k, t))
    )
    assert ("tcp", 1, 2) in stages
    assert ("probe", 1, 1) in stages
    assert result == [(("http", "1.1.1.1", 80), 5.0)]


def test_verify_vless_pool_forwards_urltest_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(verify, "tcp_reachable", lambda host, port, timeout: True)
    monkeypatch.setattr(verify, "tls_handshake", lambda h, p, sn, timeout: True)
    monkeypatch.setattr(verify, "singbox_check", lambda n, bin: True)

    def fake_probe(
        node: object, bin: str, timeout: float, url: str | None = None
    ) -> list[float]:
        seen["url"] = url
        return [7.0]

    monkeypatch.setattr(verify, "_probe_latencies", fake_probe)
    result = verify.verify_vless_pool(
        [_RELAY], "sing-box", 3.0, urltest_url="https://probe.example/x"
    )
    assert seen["url"] == "https://probe.example/x"
    assert result[0][1] == 7.0


def test_verify_proxy_pool_forwards_urltest_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(verify, "tcp_reachable", lambda host, port, timeout: True)

    def fake_probe(
        p: str, h: str, port: int, timeout: float, url: str | None = None
    ) -> list[float]:
        seen["url"] = url
        return [5.0]

    monkeypatch.setattr(verify.free, "probe_targets", fake_probe)
    result = verify.verify_proxy_pool(
        [("http", "1.1.1.1", 80)],
        3.0,
        urltest_url="https://probe.example/x",
    )
    assert seen["url"] == "https://probe.example/x"
    assert result[0][1] == 5.0


def test_nodes_from_vless_survivors() -> None:
    survivors = [((_RELAY, _parsed()), 5.0), ((_RELAY2, _parsed(_RELAY2)), 3.0)]
    nodes = verify.nodes_from_vless_survivors(survivors)
    assert isinstance(nodes[0], Node)
    assert nodes[0].status == NodeStatus.ALIVE
    assert nodes[0].latency_ms == 5.0
    assert nodes[1].priority == 1
    assert nodes[0].protocol.value == "vless"


def test_nodes_from_proxy_survivors() -> None:
    survivors = [(("http", "1.1.1.1", 80), 7.0), (("socks5", "2.2.2.2", 1080), 3.0)]
    nodes = verify.nodes_from_proxy_survivors(survivors)
    assert nodes[0].protocol.value == "http"
    assert nodes[1].protocol.value == "socks5"
    assert nodes[0].latency_ms == 7.0
    assert nodes[1].priority == 1
    assert nodes[1].raw == "socks5://2.2.2.2:1080"
