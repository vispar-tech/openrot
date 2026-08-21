from openrot.core import rotator
from openrot.models import Config, Node, NodeStatus, Profile, Strategy


def _profile(
    name: str, nodes: list[Node], priority: int = 0, enabled: bool = True
) -> Profile:
    return Profile(name=name, nodes=nodes, priority=priority, enabled=enabled)


def _node(id_: str, priority: int = 0, status: NodeStatus = NodeStatus.UNKNOWN) -> Node:
    return Node(id=id_, raw=f"vless://x@{id_}:80", priority=priority, status=status)


def test_chain_orders_by_profile_then_node_priority() -> None:
    c = Config(
        profiles=[
            _profile("low", [_node("n3", priority=0)], priority=10),
            _profile(
                "high",
                [_node("n2", priority=5), _node("n1", priority=0)],
                priority=0,
            ),
        ]
    )
    assert [n.id for n in rotator.chain(c)] == ["n1", "n2", "n3"]


def test_chain_skips_disabled_profiles() -> None:
    c = Config(
        profiles=[
            _profile("off", [_node("n1")], priority=0, enabled=False),
            _profile("on", [_node("n2")], priority=1),
        ]
    )
    assert [n.id for n in rotator.chain(c)] == ["n2"]


def test_pick_fallback_uses_first_alive() -> None:
    c = Config(
        profiles=[
            _profile(
                "a",
                [
                    _node("n1", status=NodeStatus.DEAD),
                    _node("n2", status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    assert rotator.pick(c).id == "n2"


def test_index_of_returns_position_in_chain() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n3", priority=2),
                    _node("n1", priority=0),
                    _node("n2", priority=1),
                ],
                priority=0,
            ),
        ]
    )
    by_id = {n.id: n for n in c.all_nodes()}
    assert [n.id for n in rotator.chain(c)] == ["n1", "n2", "n3"]
    assert rotator.index_of(c, by_id["n1"]) == 1
    assert rotator.index_of(c, by_id["n2"]) == 2
    assert rotator.index_of(c, by_id["n3"]) == 3


def test_index_of_returns_none_for_unknown_node() -> None:
    c = Config(profiles=[_profile("p", [_node("n1")])])
    assert rotator.index_of(c, _node("ghost")) is None


def test_first_node_returns_start_of_queue() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n1", status=NodeStatus.DEAD),
                    _node("n2", status=NodeStatus.ALIVE),
                    _node("n3", status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    assert rotator.first_node(c).id == "n2"


def test_first_node_none_when_none_alive() -> None:
    assert rotator.first_node(Config(profiles=[_profile("p", [_node("n1")])])) is None


def test_next_node_walks_forward_and_wraps() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n1", status=NodeStatus.ALIVE),
                    _node("n2", status=NodeStatus.ALIVE),
                    _node("n3", status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    by_id = {n.id: n for n in c.all_nodes()}
    assert rotator.next_node(c, by_id["n1"].id).id == "n2"
    assert rotator.next_node(c, by_id["n2"].id).id == "n3"
    assert rotator.next_node(c, by_id["n3"].id).id == "n1"


def test_next_node_skips_dead_nodes() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n1", status=NodeStatus.DEAD),
                    _node("n2", status=NodeStatus.ALIVE),
                    _node("n3", status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    by_id = {n.id: n for n in c.all_nodes()}
    assert rotator.next_node(c, by_id["n2"].id).id == "n3"
    assert rotator.next_node(c, by_id["n3"].id).id == "n2"


def test_next_node_unknown_id_returns_first_alive() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n1", status=NodeStatus.ALIVE),
                    _node("n2", status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    assert rotator.next_node(c, "ghost").id == "n1"


def test_pick_urltest_uses_health_select() -> None:
    n1 = _node("n1", priority=2, status=NodeStatus.ALIVE)
    n1.latency_ms = 50
    n2 = _node("n2", priority=1, status=NodeStatus.ALIVE)
    n2.latency_ms = 10
    c = Config(
        strategy=Strategy.URLTEST,
        profiles=[_profile("p", [n1, n2], priority=0)],
    )
    picked = rotator.pick(c)
    assert picked is not None
    assert picked.id == "n2"


def test_next_node_defaults_to_first_alive() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n1", status=NodeStatus.ALIVE),
                    _node("n2", status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    assert rotator.next_node(c).id == "n1"


def test_pick_fallback_uses_first_alive_by_priority() -> None:
    c = Config(
        profiles=[
            _profile(
                "p",
                [
                    _node("n1", priority=5, status=NodeStatus.ALIVE),
                    _node("n2", priority=1, status=NodeStatus.ALIVE),
                ],
                priority=0,
            ),
        ]
    )
    assert rotator.pick(c).id == "n2"
