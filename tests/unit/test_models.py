from openrot.models import TOP_LIMIT, Config, Node, Profile, node_id, profile_id


def test_config_top_limit_defaults_to_pool_cap() -> None:
    assert Config().top_limit == TOP_LIMIT == 20


def test_node_id_stable_and_prefixed() -> None:
    raw = "vless://a@1.1.1.1:80#A"
    assert node_id(raw) == node_id(raw)
    assert node_id(raw).startswith("node-")
    assert node_id(raw) != node_id("vless://a@1.1.1.1:80#B")


def test_profile_id_stable_generated_from_name() -> None:
    assert profile_id("x") == profile_id("x")
    assert profile_id("x").startswith("prof-")
    assert profile_id("x") != profile_id("y")
    assert Profile(name="x").id == profile_id("x")
    assert Profile(name="x", id="custom").id == "custom"


def test_config_all_nodes_flattens_profiles() -> None:
    c = Config(
        profiles=[
            Profile(name="a", nodes=[Node(id="n1", raw="r1")]),
            Profile(name="b", nodes=[Node(id="n2", raw="r2")]),
        ]
    )
    assert [n.id for n in c.all_nodes()] == ["n1", "n2"]
