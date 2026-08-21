from datetime import datetime

import pytest

from openrot.config import Config, Node, Profile, ProfileKind
from openrot.core import refresh

_RELAY = (
    "vless://f8c2e8a0-0000-4000-8000-000000000001@example.com:443?encryption=none#t"
)


def test_tick_interval_uses_smallest_profile_interval() -> None:
    c = Config(
        update_interval=3600,
        profiles=[
            Profile(name="a", url="http://x", interval=60),
            Profile(name="b", url="http://y", interval=None),
        ],
    )
    assert refresh._tick_interval(c) == 60


def test_tick_interval_ignores_disabled_and_no_url() -> None:
    c = Config(
        update_interval=3600,
        profiles=[
            Profile(name="off", url="http://x", interval=10, enabled=False),
            Profile(name="nourl", interval=5),
        ],
    )
    assert refresh._tick_interval(c) == 3600


def test_tick_interval_falls_back_without_profiles() -> None:
    assert refresh._tick_interval(Config(update_interval=7200)) == 7200


def _cfg_for(prof: Profile) -> Config:
    return Config(profiles=[prof])


def test_fetch_profile_nodes_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    prof = Profile(name="p", kind=ProfileKind.RELAY, url="https://x.test/sub")
    monkeypatch.setattr(
        refresh.nodes, "fetch_text", lambda url: "# comment\nvless://a@1.1.1.1:80\n"
    )
    monkeypatch.setattr(
        refresh.vless, "extract_from_text", lambda t: ["vless://a@1.1.1.1:80"]
    )
    monkeypatch.setattr(
        refresh.verify,
        "verify_vless_pool",
        lambda *a, **k: [(("vless://a@1.1.1.1:80", None), 12.0)],
    )
    result = refresh.fetch_profile_nodes(prof, _cfg_for(prof))
    assert result and result[0].protocol.value == "vless"
    assert result[0].status.value == "alive"


def test_fetch_profile_nodes_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    prof = Profile(name="p", kind=ProfileKind.PROXY, url="https://x.test/list")
    monkeypatch.setattr(refresh.nodes, "fetch_text", lambda url: "http://h:1\n")
    monkeypatch.setattr(
        refresh.free, "fetch_candidates", lambda text: [("http", "h", 1)]
    )
    monkeypatch.setattr(
        refresh.verify,
        "verify_proxy_pool",
        lambda *a, **k: [(("http", "h", 1), 20.0)],
    )
    result = refresh.fetch_profile_nodes(prof, _cfg_for(prof))
    assert result and result[0].protocol.value == "http"


def test_fetch_profile_nodes_forwards_urltest_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prof = Profile(name="p", kind=ProfileKind.RELAY, url="https://x.test/sub")
    cfg_obj = _cfg_for(prof)
    cfg_obj.urltest_url = "https://probe.example/x"
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        refresh.nodes, "fetch_text", lambda url: "vless://a@1.1.1.1:80\n"
    )
    monkeypatch.setattr(
        refresh.vless, "extract_from_text", lambda t: ["vless://a@1.1.1.1:80"]
    )

    def fake_verify(*a: object, **k: object) -> list[object]:
        seen["urltest_url"] = k.get("urltest_url")
        return []

    monkeypatch.setattr(refresh.verify, "verify_vless_pool", fake_verify)
    refresh.fetch_profile_nodes(prof, cfg_obj)
    assert seen["urltest_url"] == "https://probe.example/x"


def test_commit_refresh_updates_nodes_and_last_update() -> None:
    old = datetime(2020, 1, 1)
    prof = Profile(
        name="p", nodes=[Node(id="old", raw="vless://a@1.1.1.1:80")], last_update=old
    )
    fresh = Config(profiles=[prof])
    new_node = Node(id="new", raw="vless://b@2.2.2.2:80")
    refresh._commit_refresh(fresh, {"p": [new_node]})
    assert fresh.profiles[0].nodes == [new_node]
    assert fresh.profiles[0].last_update is not None
    assert fresh.profiles[0].last_update != old


def test_commit_refresh_ignores_missing_profile() -> None:
    fresh = Config()
    refresh._commit_refresh(
        fresh, {"ghost": [Node(id="x", raw="vless://a@1.1.1.1:80")]}
    )
    assert fresh.profiles == []


def test_run_scheduler_fetches_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openrot import config as cfg

    prof = Profile(
        name="p",
        url="https://x.test/sub",
        interval=1,
    )
    recorded: list[Config] = []

    def fake_fetch(prof: Profile, cfg_obj: Config) -> list[Node]:
        del cfg_obj
        return [Node(id="n1", raw="vless://a@1.1.1.1:80")]

    def fake_update_config(path: object, mutator: object) -> None:
        del path
        c = cfg.Config(profiles=[Profile(name="p", url="https://x.test/sub")])
        mutator(c)  # type: ignore[operator]
        recorded.append(c)

    sleep_calls = {"n": 0}

    def fake_sleep(_s: float) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 1:
            raise SystemExit(0)

    monkeypatch.setattr(refresh, "fetch_profile_nodes", fake_fetch)
    monkeypatch.setattr(refresh.cfg, "update_config", fake_update_config)
    monkeypatch.setattr(refresh.cfg, "load_config", lambda: cfg.Config(profiles=[prof]))
    monkeypatch.setattr(refresh.time, "sleep", fake_sleep)
    monkeypatch.setattr(refresh.random, "random", lambda: 0.0)

    with pytest.raises(SystemExit):
        refresh.run_scheduler()

    assert recorded and recorded[0].profiles[0].nodes[0].id == "n1"
    assert recorded[0].profiles[0].last_update is not None
