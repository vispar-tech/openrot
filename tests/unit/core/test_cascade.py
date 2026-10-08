from __future__ import annotations

import threading

import pytest

from openrot.config import ActiveLevel
from openrot.core import cascade
from openrot.models import Config


def _patch_events(monkeypatch: pytest.MonkeyPatch) -> _Events:
    events = _Events()
    monkeypatch.setattr(cascade, "events", events)
    return events


def test_start_connects_warp(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = Config(active_level=ActiveLevel.NONE)
    saved: list[Config] = []
    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(
        cascade.cfg, "save_config", lambda c, path=None: saved.append(c)
    )
    monkeypatch.setattr(cascade.warp, "connect", lambda: True)
    monkeypatch.setattr(cascade.warp, "proxy_address", lambda: ("127.0.0.1", 40000))
    monkeypatch.setattr(cascade.warp, "current_ip", lambda: "9.9.9.9")

    assert cascade.start() is True
    assert conf.active_level == ActiveLevel.WARP
    assert saved == [conf]


def test_start_logs_and_returns_false_when_warp_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed connect logs a warning and returns False — never SystemExit."""
    conf = Config(active_level=ActiveLevel.NONE)
    events = _patch_events(monkeypatch)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(
        cascade.cfg, "save_config", lambda c, path=None: pytest.fail("must not save")
    )
    monkeypatch.setattr(cascade.warp, "connect", lambda: False)

    assert cascade.start() is False
    assert conf.active_level == ActiveLevel.NONE
    assert any(kind == "warning" for kind, _ in events.calls)


def test_level_serving_tracks_warp_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conf = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cascade.warp, "is_connected", lambda: True)
    assert cascade.level_serving(conf) is True

    monkeypatch.setattr(cascade.warp, "is_connected", lambda: False)
    assert cascade.level_serving(conf) is False

    conf_none = Config(active_level=ActiveLevel.NONE)
    assert cascade.level_serving(conf_none) is False


def test_rotate_warp(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.warp, "rotate", lambda: True)
    monkeypatch.setattr(cascade.warp, "current_ip", lambda: "9.9.9.9")
    _patch_events(monkeypatch)

    assert cascade.rotate() is True


def test_warp_health_loop_reconnects_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop retries the connect whenever WARP is down — it never returns."""
    started = {"n": 0}

    def fake_sleep(s: float) -> None:
        pass

    def fake_start() -> bool:
        started["n"] += 1
        raise SystemExit(0)  # leave the infinite loop

    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        cascade.cfg,
        "load_config",
        lambda: Config(health_interval=1),
    )
    monkeypatch.setattr(cascade.warp, "is_connected", lambda: False)
    monkeypatch.setattr(cascade, "start", fake_start)

    with pytest.raises(SystemExit):
        cascade.warp_health_loop()

    assert started["n"] == 1


def test_warp_health_loop_continues_while_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop keeps running while WARP is connected; exits via SystemExit."""
    call_count = {"n": 0}

    def fake_sleep(s: float) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise SystemExit(0)

    def fake_load() -> Config:
        return Config(health_interval=1)

    def fake_is_connected() -> bool:
        return True

    _patch_events(monkeypatch)
    monkeypatch.setattr(cascade.time, "sleep", fake_sleep)
    monkeypatch.setattr(cascade.cfg, "load_config", fake_load)
    monkeypatch.setattr(cascade.warp, "is_connected", fake_is_connected)

    with pytest.raises(SystemExit):
        cascade.warp_health_loop()

    assert call_count["n"] == 2


def test_background_starts_warp_health_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """background() starts only the warp health loop as a daemon thread."""
    started: list[dict[str, object]] = []

    class FakeThread:
        def __init__(self, **kwargs: object) -> None:
            started.append(kwargs)

        def start(self) -> None:
            pass

    monkeypatch.setattr(cascade.threading, "Thread", FakeThread)

    cascade.background()

    names = {t["name"] for t in started}
    assert names == {"openrot-warp-health"}
    assert all(t["daemon"] is True for t in started)
    warp = next(t for t in started if t["name"] == "openrot-warp-health")
    assert "kwargs" not in warp


def test_rotate_waits_when_already_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent rotate() waits for the running one instead of skipping."""
    rotate_started = threading.Event()
    rotate_proceed = threading.Event()
    call_count = {"n": 0}
    returns: list[bool] = []

    def slow_warp_rotate() -> bool:
        call_count["n"] += 1
        rotate_started.set()
        rotate_proceed.wait(timeout=5)
        return True

    conf = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(cascade.warp, "rotate", slow_warp_rotate)
    monkeypatch.setattr(cascade.warp, "current_ip", lambda: "1.1.1.1")
    _patch_events(monkeypatch)

    def first_rotate() -> None:
        returns.append(cascade.rotate())

    def second_rotate() -> None:
        rotate_started.wait(timeout=5)
        returns.append(cascade.rotate())

    t1 = threading.Thread(target=first_rotate)
    t2 = threading.Thread(target=second_rotate)
    t1.start()
    t2.start()
    rotate_proceed.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert call_count["n"] == 1
    assert sorted(returns) == [False, True]


def test_stop_disconnects_warp_and_resets_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    conf = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(
        cascade.warp, "disconnect", lambda: calls.append("warp") or True
    )
    monkeypatch.setattr(cascade.cfg, "load_config", lambda: conf)
    monkeypatch.setattr(
        cascade.cfg, "save_config", lambda c, path=None: calls.append("save")
    )

    cascade.stop()

    assert calls == ["warp", "save"]
    assert conf.active_level == ActiveLevel.NONE


class _Events:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def info(self, *args: object) -> None:
        self.calls.append(("info", args))

    def warning(self, *args: object) -> None:
        self.calls.append(("warning", args))
