import pytest

from openrot.core.health import check_node, select_node
from openrot.models import Node, NodeProtocol, NodeStatus, Strategy


def _node(
    id_: str,
    status: NodeStatus = NodeStatus.ALIVE,
    latency: float | None = None,
) -> Node:
    return Node(id=id_, raw=f"vless://x@{id_}:80", status=status, latency_ms=latency)


def test_fallback_picks_first_alive() -> None:
    nodes = [
        _node("n1", NodeStatus.DEAD),
        _node("n2", latency=50),
        _node("n3", latency=10),
    ]
    assert select_node(nodes, Strategy.FALLBACK).id == "n2"


def test_urltest_picks_min_latency() -> None:
    nodes = [_node("n1", latency=50), _node("n2", latency=10), _node("n3", latency=25)]
    assert select_node(nodes, Strategy.URLTEST).id == "n2"


def test_urltest_equal_latency_no_crash() -> None:
    nodes = [_node("n1", latency=10), _node("n2", latency=10)]
    assert select_node(nodes, Strategy.URLTEST).id == "n1"


def test_select_excludes_current() -> None:
    nodes = [_node("n1", latency=10), _node("n2", latency=50)]
    assert select_node(nodes, Strategy.FALLBACK, exclude_id="n1").id == "n2"


def test_select_returns_none_when_no_alive() -> None:
    nodes = [_node("n1", NodeStatus.DEAD), _node("n2", NodeStatus.UNKNOWN)]
    assert select_node(nodes, Strategy.FALLBACK) is None
    assert select_node(nodes, Strategy.URLTEST) is None


def test_all_updates_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Config, Profile
    from openrot.core.health import test_all

    c = Config(
        profiles=[
            Profile(
                name="a",
                nodes=[Node(id="n1", raw="r1"), Node(id="n2", raw="r2")],
            ),
        ]
    )

    def fake_pool(
        *a: object, **k: object
    ) -> list[tuple[tuple[str, object], float, str | None]]:
        return [(("r1", object()), 10.0, None)]

    monkeypatch.setattr("openrot.core.health.verify.verify_vless_pool", fake_pool)
    assert test_all(c) == 1
    assert c.all_nodes()[0].status == NodeStatus.ALIVE
    assert c.all_nodes()[0].latency_ms == 10.0
    assert c.all_nodes()[1].status == NodeStatus.UNKNOWN
    assert c.all_nodes()[1].fails == 1


def test_all_passes_on_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Config, Profile
    from openrot.core.health import test_all

    c = Config(profiles=[Profile(name="a", nodes=[Node(id="n1", raw="r1")])])
    recorded: list[tuple[str, object, object]] = []

    def fake_pool(*a: object, **k: object) -> list[object]:
        on_stage = k.get("on_stage")
        if callable(on_stage):
            on_stage("tcp", 1, 1)
        return []

    monkeypatch.setattr("openrot.core.health.verify.verify_vless_pool", fake_pool)
    assert test_all(c, on_stage=lambda *x: recorded.append(x)) == 0
    assert recorded == [("tcp", 1, 1)]


def test_all_forwards_urltest_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Config, Profile
    from openrot.core.health import test_all

    c = Config(profiles=[Profile(name="a", nodes=[Node(id="n1", raw="r1")])])
    c.urltest_url = "https://probe.example/x"
    seen: dict[str, object] = {}

    def fake_pool(
        *a: object, **k: object
    ) -> list[tuple[tuple[str, object], float, str | None]]:
        seen["urltest_url"] = k.get("urltest_url")
        return []

    monkeypatch.setattr("openrot.core.health.verify.verify_vless_pool", fake_pool)
    test_all(c)
    assert seen["urltest_url"] == "https://probe.example/x"


def test_select_urltest_returns_none_without_latencies() -> None:
    nodes = [_node("n1"), _node("n2")]
    assert select_node(nodes, Strategy.URLTEST) is None


def test_check_node_http_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Config

    node = _node("n1")
    node.protocol = NodeProtocol.HTTP
    monkeypatch.setattr("openrot.core.health.free.check_node", lambda n, c: (True, 7.0))
    assert check_node(node, Config()) == (True, 7.0)


def test_check_node_socks5_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Config

    node = _node("n1")
    node.protocol = NodeProtocol.SOCKS5
    monkeypatch.setattr(
        "openrot.core.health.free.check_node", lambda n, c: (False, None)
    )
    assert check_node(node, Config()) == (False, None)


def test_check_node_vless_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    from openrot.config import Config

    node = _node("n1")
    parsed = object()
    monkeypatch.setattr("openrot.core.health.parse_vless", lambda raw: parsed)
    monkeypatch.setattr(
        "openrot.core.health.probe_vless", lambda vn, sb, to: (True, 3.0, None)
    )
    assert check_node(node, Config()) == (True, 3.0)


def test_check_node_malformed_returns_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openrot.config import Config
    from openrot.providers.vless import ParseError

    node = _node("n1")
    node.protocol = NodeProtocol.VLESS
    monkeypatch.setattr(
        "openrot.core.health.parse_vless",
        lambda raw: (_ for _ in ()).throw(ParseError("bad")),
    )
    assert check_node(node, Config()) == (False, None)


def test_apply_result_marks_dead_on_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openrot.config import Config, Profile
    from openrot.core.health import test_all

    c = Config(
        fail_threshold=3,
        profiles=[Profile(name="a", nodes=[Node(id="n1", raw="r1", fails=2)])],
    )
    monkeypatch.setattr(
        "openrot.core.health.verify.verify_vless_pool", lambda *a, **k: []
    )
    assert test_all(c) == 0
    assert c.all_nodes()[0].status == NodeStatus.DEAD
    assert c.all_nodes()[0].fails == 3
