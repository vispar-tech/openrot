import threading

import pytest

from openrot.config import ActiveLevel
from openrot.core import cascade
from openrot.models import Config, Node, NodeStatus, Profile


class _Events:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, *args: object) -> None:
        self.calls.append(("info", args))

    def warning(self, *args: object) -> None:
        self.calls.append(("warning", args))


def _patch_events(monkeypatch: pytest.MonkeyPatch) -> _Events:
    events = _Events()
    monkeypatch.setattr(cascade, "events", events)
    return events


def test_start_foreground_interrupt_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_start_warp(foreground: bool) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(cascade, "start_warp", fake_start_warp)
    monkeypatch.setattr(cascade.signals, "keyboard_on_sigterm", lambda: None)
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: calls.append("proxy"))
    monkeypatch.setattr(cascade.warp, "disconnect", lambda: calls.append("warp"))
    cfg_obj = Config(update_interval=0, active_level=ActiveLevel.NODE)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c: None)

    with pytest.raises(SystemExit) as exc:
        cascade.start(True, False)

    assert exc.value.code == 0
    assert calls == ["proxy", "warp"]
    assert cfg_obj.active_level == ActiveLevel.NONE
    assert cfg_obj.current_node_id is None


def test_start_foreground_interrupt_races_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C interacts safely with the refresh scheduler thread.

    Shutdown wins and no scheduler write may resurrect the active level.
    """
    conf = Config(
        update_interval=60,
        active_level=ActiveLevel.NODE,
        current_node_id="n1",
        profiles=[Profile(name="p", nodes=[])],
    )
    scheduler_ready = threading.Event()
    interrupt_now = threading.Event()
    shutdown_calls: list[Config] = []

    def fake_scheduler() -> None:
        scheduler_ready.set()
        interrupt_now.wait()

    def fake_start_warp(foreground: bool) -> bool:
        assert scheduler_ready.wait(timeout=5)
        interrupt_now.set()
        raise KeyboardInterrupt

    monkeypatch.setattr(cascade, "start_warp", fake_start_warp)
    monkeypatch.setattr(cascade.refresh, "run_scheduler", fake_scheduler)
    monkeypatch.setattr(cascade.signals, "keyboard_on_sigterm", lambda: None)
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: None)
    monkeypatch.setattr(cascade.warp, "disconnect", lambda: None)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(
        cascade.cfg, "save_config", lambda c, path=None: shutdown_calls.append(c)
    )

    with pytest.raises(SystemExit) as exc:
        cascade.start(True, False)

    assert exc.value.code == 0
    assert conf.active_level == ActiveLevel.NONE
    assert conf.current_node_id is None
    assert shutdown_calls[-1].active_level == ActiveLevel.NONE

    fresh_node = Node(id="n2", raw="vless://b@2.2.2.2:80", status=NodeStatus.ALIVE)
    cascade.refresh._commit_refresh(conf, {"p": [fresh_node]})
    assert conf.profiles[0].nodes == [fresh_node]
    assert conf.active_level == ActiveLevel.NONE


def test_start_foreground_echoes_events_to_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    echoed = {"n": 0}

    def fake_echo() -> None:
        echoed["n"] += 1

    monkeypatch.setattr(cascade.log, "console_echo", fake_echo)
    monkeypatch.setattr(cascade, "start_warp", lambda foreground: False)
    monkeypatch.setattr(cascade, "start_node", lambda foreground: None)
    monkeypatch.setattr(cascade.proxy, "load_pid", lambda: None)
    cfg_obj = Config(update_interval=0, port=1080)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: cfg_obj)

    cascade.start(True, False)
    assert echoed["n"] == 1


def test_start_with_daemon_flag_forks(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def fake_daemonize() -> None:
        called["n"] += 1

    monkeypatch.setattr(cascade, "daemonize", fake_daemonize)
    cascade.start(False, True)
    assert called["n"] == 1


def test_start_exits_when_proxy_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = {"node": 0, "warp": 0}

    def fake_load_pid() -> int:
        return 4242

    def fake_is_running(pid: int) -> bool:
        return True

    def fake_start_warp(foreground: bool) -> bool:
        started["warp"] += 1
        return False

    def fake_start_node(foreground: bool) -> None:
        started["node"] += 1

    monkeypatch.setattr(cascade.proxy, "load_pid", fake_load_pid)
    monkeypatch.setattr(cascade.proxy, "is_running", fake_is_running)
    monkeypatch.setattr(cascade, "start_warp", fake_start_warp)
    monkeypatch.setattr(cascade, "start_node", fake_start_node)
    cfg_obj = Config(update_interval=0, port=1080)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: cfg_obj)

    cascade.start(False, False)

    assert started["node"] == 0
    assert started["warp"] == 0


def test_node_health_loop_rotates_when_current_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotated = {"n": 0}
    load_calls = {"n": 0}

    def fake_rotate() -> None:
        rotated["n"] += 1

    def fake_load() -> Config:
        load_calls["n"] += 1
        if load_calls["n"] > 1:
            raise SystemExit(0)
        return Config(update_interval=0, active_level=ActiveLevel.NODE)

    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade, "rotate", fake_rotate)
    monkeypatch.setattr(cascade.nodes, "current_node", lambda c: None)
    monkeypatch.setattr(cascade.time, "sleep", lambda s: None)
    monkeypatch.setattr(cascade.cfg, "load_config", fake_load)

    with pytest.raises(SystemExit):
        cascade.node_health_loop()

    assert rotated["n"] == 1


def test_node_health_loop_skips_rotate_when_not_node_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotated = {"n": 0}

    def fake_rotate() -> None:
        rotated["n"] += 1

    def fake_sleep(s: float) -> None:
        fake_sleep.calls += 1
        if fake_sleep.calls > 1:
            raise SystemExit(0)

    fake_sleep.calls = 0

    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade, "rotate", fake_rotate)
    monkeypatch.setattr(cascade.nodes, "current_node", lambda c: None)
    monkeypatch.setattr(cascade.time, "sleep", fake_sleep)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: Config(update_interval=0))

    with pytest.raises(SystemExit):
        cascade.node_health_loop()

    assert rotated["n"] == 0


def test_rotate_node_stops_and_relaunches(monkeypatch: pytest.MonkeyPatch) -> None:
    node = Node(id="n1", raw="vless://a@1.1.1.1:80", status=NodeStatus.ALIVE)
    conf = Config(
        update_interval=0,
        active_level=ActiveLevel.NODE,
        current_node_id="n1",
        profiles=[Profile(name="p", nodes=[node])],
    )
    picks: list[object] = []
    launched: list[object] = []

    def fake_next(c: Config, current_id: str | None = None) -> Node:
        picks.append(current_id)
        return node

    def fake_launch(c: Config, n: Node) -> int:
        launched.append(n)
        c.current_node_id = n.id
        return 42

    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: None)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c: None)
    monkeypatch.setattr(cascade.rotator, "next_node", fake_next)
    monkeypatch.setattr(cascade.nodes, "current_node", lambda c: node)
    monkeypatch.setattr(cascade, "launch", fake_launch)

    cascade.rotate()

    assert picks == ["n1"]
    assert launched == [node]
    assert conf.current_node_id == "n1"


def test_rotate_first_resets_without_excluding_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = Node(id="n1", raw="vless://a@1.1.1.1:80", status=NodeStatus.ALIVE)
    conf = Config(
        update_interval=0,
        active_level=ActiveLevel.NODE,
        current_node_id="n1",
        profiles=[Profile(name="p", nodes=[node])],
    )
    picks: list[object] = []

    def fake_first(c: Config) -> Node:
        picks.append(None)
        return node

    def fake_launch(c: Config, n: Node) -> int:
        c.current_node_id = n.id
        return 42

    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: None)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c: None)
    monkeypatch.setattr(cascade.rotator, "first_node", fake_first)
    monkeypatch.setattr(cascade.nodes, "current_node", lambda c: node)
    monkeypatch.setattr(cascade, "launch", fake_launch)

    cascade.rotate(first=True)

    assert picks == [None]
    assert conf.current_node_id == "n1"


def test_level_serving_requires_pid_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = Config(update_interval=0, active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.warp, "is_connected", lambda: True)
    monkeypatch.setattr(cascade.proxy, "load_pid", lambda: 1)
    monkeypatch.setattr(cascade.proxy, "is_running", lambda pid: True)
    assert cascade.level_serving(conf) is True

    monkeypatch.setattr(cascade.warp, "is_connected", lambda: False)
    assert cascade.level_serving(conf) is False

    monkeypatch.setattr(cascade.cfg, "load_config", lambda: Config(update_interval=0))
    conf_node = Config(update_interval=0, active_level=ActiveLevel.NODE)
    monkeypatch.setattr(cascade.proxy, "load_pid", lambda: None)
    assert cascade.level_serving(conf_node) is False


def test_launch_saves_pid_and_node(monkeypatch: pytest.MonkeyPatch) -> None:
    node = Node(id="n1", raw="vless://a@1.1.1.1:80")
    conf = Config(update_interval=0)
    saved = []

    monkeypatch.setattr(cascade.proxy, "start_node", lambda n, port, sb: 99)
    monkeypatch.setattr(cascade.proxy, "save_pid", lambda pid: None)
    monkeypatch.setattr(
        cascade.cfg, "save_config", lambda c, path=None: saved.append(c)
    )

    pid = cascade.launch(conf, node)

    assert pid == 99
    assert conf.current_node_id == "n1"
    assert saved == [conf]


def test_shutdown_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    conf = Config(
        update_interval=0, active_level=ActiveLevel.NODE, current_node_id="n1"
    )
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: calls.append("proxy"))
    monkeypatch.setattr(cascade.warp, "disconnect", lambda: calls.append("warp"))
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c, path=None: None)

    cascade._shutdown()

    assert calls == ["proxy", "warp"]
    assert conf.active_level == ActiveLevel.NONE
    assert conf.current_node_id is None


def test_start_foreground_no_warp_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_start_warp(foreground: bool) -> bool:
        calls.append("warp")
        return False

    def fake_start_node(foreground: bool) -> None:
        calls.append("node")

    monkeypatch.setattr(cascade, "start_warp", fake_start_warp)
    monkeypatch.setattr(cascade, "start_node", fake_start_node)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: Config(update_interval=0))
    monkeypatch.setattr(cascade.threading, "Thread", lambda **kw: None)

    cascade.start(True, False)

    assert calls == ["warp", "node"]


def test_start_background_no_warp_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cascade, "start_warp", lambda f: calls.append("warp") or False)
    monkeypatch.setattr(cascade, "start_node", lambda f: calls.append("node"))
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: Config(update_interval=0))

    cascade.start(False, False)

    assert calls == ["warp", "node"]


def test_start_node_no_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: Config(update_interval=0))
    with pytest.raises(SystemExit):
        cascade.start_node(False)


def test_start_node_no_alive_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    node = Node(id="n1", raw="vless://a@1.1.1.1:80", status=NodeStatus.DEAD)
    conf = Config(update_interval=0, profiles=[Profile(name="p", nodes=[node])])
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cascade.health, "test_all", lambda c: 0)
    monkeypatch.setattr(cascade.rotator, "pick", lambda c, exclude_id=None: None)
    _patch_events(monkeypatch)

    with pytest.raises(SystemExit):
        cascade.start_node(False)


def test_start_node_success(monkeypatch: pytest.MonkeyPatch) -> None:
    node = Node(id="n1", raw="vless://a@1.1.1.1:80", status=NodeStatus.ALIVE)
    conf = Config(update_interval=0, profiles=[Profile(name="p", nodes=[node])])
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.rotator, "pick", lambda c, exclude_id=None: node)
    monkeypatch.setattr(cascade, "launch", lambda c, n: 7)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c, path=None: None)
    _patch_events(monkeypatch)

    cascade.start_node(False)

    assert conf.active_level == ActiveLevel.NODE


def test_start_node_no_alive_runs_health(monkeypatch: pytest.MonkeyPatch) -> None:
    node = Node(id="n1", raw="vless://a@1.1.1.1:80", status=NodeStatus.DEAD)
    conf = Config(update_interval=0, profiles=[Profile(name="p", nodes=[node])])
    calls: list[str] = []
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cascade.health, "test_all", lambda c: calls.append("test") or 0)
    monkeypatch.setattr(cascade.rotator, "pick", lambda c, exclude_id=None: node)
    monkeypatch.setattr(cascade.proxy, "start_node", lambda n, port, sb: 7)
    monkeypatch.setattr(cascade, "launch", lambda c, n: 7)
    _patch_events(monkeypatch)

    cascade.start_node(False)

    assert "test" in calls


def test_rotate_warp(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = Config(update_interval=0, active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.warp, "rotate", lambda: True)
    monkeypatch.setattr(cascade.warp, "current_ip", lambda: "9.9.9.9")
    _patch_events(monkeypatch)

    cascade.rotate()


def test_rotate_no_alive_node(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = Config(update_interval=0, active_level=ActiveLevel.NODE)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: None)
    monkeypatch.setattr(cascade.nodes, "current_node", lambda c: None)
    monkeypatch.setattr(cascade.rotator, "pick", lambda c, exclude_id=None: None)
    _patch_events(monkeypatch)

    with pytest.raises(SystemExit):
        cascade.rotate()


def test_rotate_node_missing_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    dead = Node(id="n1", raw="vless://a@1.1.1.1:80", status=NodeStatus.DEAD)
    alive = Node(id="n2", raw="vless://b@2.2.2.2:80", status=NodeStatus.ALIVE)

    def fake_pick(c: Config, exclude_id: str | None = None) -> Node | None:
        del c
        return alive if exclude_id is None else None

    conf = Config(
        update_interval=0,
        active_level=ActiveLevel.NODE,
        current_node_id="n1",
        profiles=[Profile(name="p", nodes=[dead, alive])],
    )
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cascade.health, "test_all", lambda c: 0)
    monkeypatch.setattr(cascade.proxy, "stop_proxy", lambda: None)
    monkeypatch.setattr(cascade.nodes, "current_node", lambda c: dead)
    monkeypatch.setattr(cascade.rotator, "pick", fake_pick)
    monkeypatch.setattr(
        cascade, "launch", lambda c, n: c.__setattr__("current_node_id", n.id) or 10
    )
    _patch_events(monkeypatch)

    cascade.rotate()

    assert conf.current_node_id == "n2"


def test_stop_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = Config(
        update_interval=0, active_level=ActiveLevel.NODE, current_node_id="n1"
    )
    calls: list[str] = []
    monkeypatch.setattr(
        cascade.daemon, "stop", lambda path: calls.append("daemon") or False
    )
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(
        cascade.proxy, "stop_proxy", lambda: calls.append("proxy") or True
    )
    monkeypatch.setattr(cascade.cfg, "save_config", lambda c, path=None: None)

    cascade.stop()

    assert "daemon" in calls
    assert "proxy" in calls
    assert conf.active_level == ActiveLevel.NONE


def test_probe_connectivity_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"ip": "1.2.3.4"}

    monkeypatch.setattr(cascade.httpx.Client, "get", lambda self, url: _Resp())
    result = cascade.probe_connectivity(Config(update_interval=0, port=9999))
    assert result == {"ok": True, "ip": "1.2.3.4"}


def test_commit_health() -> None:
    node = Node(id="n1", raw="x", fails=2, status=NodeStatus.ALIVE)
    conf = Config(
        update_interval=0, fail_threshold=3, profiles=[Profile(name="p", nodes=[node])]
    )
    reached, fails = cascade._commit_health(conf, "n1", False)
    assert reached is True and fails == 3
    assert node.status == NodeStatus.DEAD

    reached, fails = cascade._commit_health(conf, "n1", True)
    assert reached is False and fails == 0
    assert node.fails == 0
