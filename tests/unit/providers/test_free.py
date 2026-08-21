from typing import Self

import pytest

from openrot.models import Node, NodeProtocol
from openrot.providers import free


class _FakeClient:
    def __init__(self, response: object = None, error: object = None) -> None:
        self._response = response
        self._error = error

    def get(self, *a: object, **k: object) -> object:
        if self._error:
            raise self._error
        return self._response

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object, **k: object) -> bool:
        return False


class _Response:
    status_code = 204
    text = ""

    def raise_for_status(self) -> None:
        pass


class _BadResponse:
    status_code = 503
    text = ""


def test_parse_proxy() -> None:
    assert free.parse_proxy("http://1.2.3.4:8080") == ("http", "1.2.3.4", 8080)
    assert free.parse_proxy("socks5://host.example:1080") == (
        "socks5",
        "host.example",
        1080,
    )


def test_parse_proxy_invalid() -> None:
    assert free.parse_proxy("garbage") is None
    assert free.parse_proxy("http://no-port") is None
    assert free.parse_proxy("https://not-supported:443") is None


def test_check_proxy_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(free.httpx, "Client", lambda *a, **k: _FakeClient(_Response()))
    alive, latency = free.check_proxy("http", "1.2.3.4", 8080)
    assert alive is True
    assert isinstance(latency, float)


def test_check_proxy_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        free.httpx,
        "Client",
        lambda *a, **k: _FakeClient(error=free.httpx.HTTPError("boom")),
    )
    alive, latency = free.check_proxy("http", "1.2.3.4", 8080)
    assert alive is False
    assert latency is None


def test_fetch_candidates_dedupes() -> None:
    text = "http://1.1.1.1:8080\nhttp://1.1.1.1:8080\nsocks5://2.2.2.2:1080\njunk"
    assert free.fetch_candidates(text) == [
        ("http", "1.1.1.1", 8080),
        ("socks5", "2.2.2.2", 1080),
    ]


def test_probe_targets_records_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(free.httpx, "Client", lambda *a, **k: _FakeClient(_Response()))
    latencies = free.probe_targets("http", "1.2.3.4", 8080)
    assert len(latencies) == 1
    assert isinstance(latencies[0], float)


def test_probe_targets_uses_configured_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _RecordingClient:
        def get(self, *a: object, **k: object) -> object:
            seen["url"] = a[0]
            return _Response()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *a: object, **k: object) -> bool:
            return False

    monkeypatch.setattr(free.httpx, "Client", lambda *a, **k: _RecordingClient())
    latencies = free.probe_targets(
        "http", "1.2.3.4", 8080, url="https://alt.example/probe"
    )
    assert len(latencies) == 1
    assert seen["url"] == "https://alt.example/probe"


def test_probe_targets_rejects_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        free.httpx, "Client", lambda *a, **k: _FakeClient(_BadResponse())
    )
    assert free.probe_targets("http", "1.2.3.4", 8080) == []


def test_probe_targets_empty_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        free.httpx,
        "Client",
        lambda *a, **k: _FakeClient(error=free.httpx.HTTPError("boom")),
    )
    assert free.probe_targets("http", "1.2.3.4", 8080) == []


def test_check_node_uses_config_health_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openrot.config import Config

    seen: dict[str, object] = {}

    def fake_check_proxy(
        protocol: str, host: str, port: int, timeout: float
    ) -> tuple[bool, float | None]:
        seen["timeout"] = timeout
        return True, 1.0

    monkeypatch.setattr(free, "check_proxy", fake_check_proxy)

    node = Node(id="n", raw="http://1.2.3.4:8080", protocol=NodeProtocol.HTTP)
    cfg = Config(health_timeout=42)
    alive, _ = free.check_node(node, cfg)
    assert alive is True
    assert seen["timeout"] == 42


def test_check_node_dead_when_unparseable() -> None:
    from openrot.config import Config

    node = Node(id="n", raw="http://no-port", protocol=NodeProtocol.HTTP)
    assert free.check_node(node, Config()) == (False, None)
