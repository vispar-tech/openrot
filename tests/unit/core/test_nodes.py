from pathlib import Path
from typing import Any

import pytest

from openrot.core import nodes
from openrot.models import Config, Node, NodeProtocol, Profile


def _profile(name: str, **kw: Any) -> Profile:
    return Profile(name=name, **kw)


def test_node_from_records_dedupes_and_sets_protocol() -> None:
    a = "vless://aaa@example.com:443#A"
    records = [a, a, "vless://bbb@other.com:80#B"]
    result = nodes.node_from_records(records, NodeProtocol.VLESS)
    assert [n.raw for n in result] == [a, "vless://bbb@other.com:80#B"]
    assert all(n.id.startswith("node-") for n in result)
    assert all(n.protocol == NodeProtocol.VLESS for n in result)


def test_current_node_by_id() -> None:
    prof = _profile("x", nodes=[Node(id="n1", raw="r1"), Node(id="n2", raw="r2")])
    c = Config(profiles=[prof], current_node_id="n2")
    assert nodes.current_node(c).id == "n2"


def test_current_node_none() -> None:
    c = Config(profiles=[_profile("x", nodes=[Node(id="n1", raw="r1")])])
    assert nodes.current_node(c) is None


def test_find_profile() -> None:
    c = Config(profiles=[_profile("a"), _profile("b")])
    assert nodes.find_profile(c, "b") is not None
    assert nodes.find_profile(c, "missing") is None


def test_fetch_text_file(tmp_path: Path) -> None:
    f = tmp_path / "nodes.txt"
    f.write_text("vless://a@1.1.1.1:80#X")
    assert nodes.fetch_text(str(f)).strip().startswith("vless://")


def test_fetch_text_raises_on_unknown_source() -> None:
    with pytest.raises(ValueError):
        nodes.fetch_text("not-a-path")


def test_node_label_vless_prefers_name() -> None:
    node = Node(id="n1", raw="vless://aaa@example.com:443#MyNode")
    assert nodes.node_label(node) == "MyNode"
    assert (
        nodes.node_label(Node(id="n2", raw="vless://aaa@example.com:443"))
        == "example.com"
    )


def test_node_label_fallback_to_id_on_parse_error() -> None:
    node = Node(id="abc", raw="garbage")
    assert nodes.node_label(node) == "abc"


def test_node_label_non_vless_returns_raw() -> None:
    node = Node(id="p1", raw="http://1.2.3.4:8080", protocol=NodeProtocol.HTTP)
    assert nodes.node_label(node) == "http://1.2.3.4:8080"


def test_node_address_by_protocol() -> None:
    proxy_node = Node(
        id="p1", raw="socks5://5.6.7.8:1080", protocol=NodeProtocol.SOCKS5
    )
    vless_node = Node(id="v1", raw="vless://aaa@example.com:443#X")
    bad = Node(id="b1", raw="garbage")
    assert nodes.node_address(proxy_node) == "5.6.7.8:1080"
    assert nodes.node_address(vless_node) == "example.com:443"
    assert nodes.node_address(bad) == "?"


def test_node_address_missing_proxy_port() -> None:
    bad = Node(id="p2", raw="http://no-port", protocol=NodeProtocol.HTTP)
    assert nodes.node_address(bad) == "?"
