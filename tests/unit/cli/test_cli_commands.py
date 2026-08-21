"""In-process CliRunner tests that exercise the CLI command bodies directly.

Unlike the subprocess-based CLI tests, invoking via `CliRunner` on the actual
command functions lets coverage count the executed lines. Dependencies are
monkeypatched at the `cli` module level so no external tools or network are
required.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from openrot import cli
from openrot.config import (
    ActiveLevel,
    Config,
    Node,
    NodeStatus,
    Profile,
    ProfileKind,
)
from openrot.providers.warp import WarpStatus

runner = CliRunner()


def _node(raw: str = "vless://uuid@host:443#node") -> Node:
    return Node(id="n1", raw=raw, status=NodeStatus.ALIVE, latency_ms=12.0)


def _profile(name: str = "p1", nodes: list[Node] | None = None) -> Profile:
    return Profile(
        name=name,
        kind=ProfileKind.RELAY,
        url="https://example.test/sub",
        nodes=nodes or [_node()],
    )


def test_version(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert "openrot" in result.output


def test_profile_add(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = []

    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: saved.append(c))
    result = runner.invoke(
        cli.app, ["profile", "add", "p1", "https://example.test/sub", "--kind", "relay"]
    )
    assert result.exit_code == 0
    assert "Added profile 'p1'" in result.output
    assert saved and saved[0].profiles[0].name == "p1"


def test_profile_add_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile()
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[prof]))
    result = runner.invoke(cli.app, ["profile", "add", "p1", "https://x.test"])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_profile_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[_profile()]))
    result = runner.invoke(cli.app, ["profile", "list"])
    assert result.exit_code == 0
    assert "p1" in result.output


def test_profile_list_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    result = runner.invoke(cli.app, ["profile", "list"])
    assert result.exit_code == 0
    assert "No profiles" in result.output


def test_profile_list_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[_profile()]))
    result = runner.invoke(cli.app, ["profile", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["name"] == "p1"
    assert data[0]["kind"] == "relay"
    assert data[0]["nodes_alive"] == 1
    assert data[0]["nodes_total"] == 1


def test_profile_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = []
    monkeypatch.setattr(cli.typer, "confirm", lambda prompt: True)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[_profile()]))
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: saved.append(c))
    result = runner.invoke(cli.app, ["profile", "remove", "p1"])
    assert result.exit_code == 0
    assert saved and saved[0].profiles == []


def test_profile_remove_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    result = runner.invoke(cli.app, ["profile", "remove", "nope"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_profile_set(monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile()
    saved = []
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[prof]))
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: saved.append(c))
    result = runner.invoke(
        cli.app, ["profile", "set", "p1", "--priority", "5", "--disabled"]
    )
    assert result.exit_code == 0
    assert saved
    assert saved[0].profiles[0].priority == 5
    assert saved[0].profiles[0].enabled is False


def test_profile_set_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[_profile()]))
    result = runner.invoke(cli.app, ["profile", "set", "p1"])
    assert result.exit_code == 1
    assert "nothing to change" in result.output


def test_list_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[_profile()]))
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0
    assert "host:443" in result.output


def test_list_nodes_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(profiles=[_profile()]))
    result = runner.invoke(cli.app, ["list", "--json"])
    assert result.exit_code == 0
    assert '"n1"' in result.output


def test_list_nodes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0
    assert "No nodes configured" in result.output


def test_test_command(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = Config(profiles=[_profile()])
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cli.health, "test_all", lambda c: 0)
    result = runner.invoke(cli.app, ["test"])
    assert result.exit_code == 0
    assert "Health check" in result.output


def test_test_command_no_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    result = runner.invoke(cli.app, ["test"])
    assert result.exit_code == 1
    assert "No nodes configured" in result.output


def test_test_command_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    node = _node()
    cfg_obj = Config(profiles=[_profile(nodes=[node])])
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cli.health, "test_all", lambda c: 1)
    result = runner.invoke(cli.app, ["test", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == [
        {
            "profile": "p1",
            "name": "node",
            "status": "alive",
            "latency_ms": 12.0,
            "fails": 0,
        }
    ]


def test_rotate_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_rotate(first: bool = True) -> None:
        seen["first"] = first

    monkeypatch.setattr(cli.cascade, "rotate", fake_rotate)
    result = runner.invoke(cli.app, ["rotate"])
    assert result.exit_code == 0
    assert seen["first"] is False

    result_first = runner.invoke(cli.app, ["rotate", "--first"])
    assert result_first.exit_code == 0
    assert seen["first"] is True


def test_stop_stops_cascade_and_bridge_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_stop_daemon() -> bool:
        calls.append("bridge")
        return True

    monkeypatch.setattr(cli.typer, "confirm", lambda prompt: True)
    monkeypatch.setattr(cli.cascade, "stop", lambda: calls.append("cascade"))
    monkeypatch.setattr(cli.bridge, "stop_daemon", fake_stop_daemon)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 0
    assert calls == ["cascade", "bridge"]
    assert "bridge daemon stopped" in result.output


def test_status_node(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = Config(
        active_level=ActiveLevel.NODE,
        current_node_id="n1",
        profiles=[_profile()],
    )
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.proxy, "load_pid", lambda: 1234)
    monkeypatch.setattr(cli.proxy, "is_running", lambda pid: True)
    monkeypatch.setattr(cli.warp, "is_installed", lambda: False)
    monkeypatch.setattr(
        cli.cascade, "probe_connectivity", lambda c: {"ok": True, "ip": "1.2.3.4"}
    )
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "node" in result.output
    assert "bridge: stopped" in result.output

    # current node reports its position in the queue (index/total)
    import json

    result_json = runner.invoke(cli.app, ["status", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert data["node"]["index"] == 1
    assert data["node"]["total"] == 1


def test_status_warp(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "warp" in result.output.lower()
    assert "bridge: stopped" in result.output


def test_status_warp_json_reports_real_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """warp_connected must reflect an actual probe through WARP, not the flag."""
    import json

    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: None)
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    ok_result = runner.invoke(cli.app, ["status", "--json"])
    assert ok_result.exit_code == 0
    data = json.loads(ok_result.output)
    assert data["warp_connected"] is False
    assert data["connectivity"]["ok"] is False
    assert data["bridge_running"] is False

    monkeypatch.setattr(cli.warp, "current_ip", lambda: "1.2.3.4")
    monkeypatch.setattr(cli.bridge, "running", lambda c: True)
    good_result = runner.invoke(cli.app, ["status", "--json"])
    data = json.loads(good_result.output)
    assert data["warp_connected"] is True
    assert data["connectivity"] == {"ok": True, "ip": "1.2.3.4"}
    assert data["bridge_running"] is True


def test_status_warp_text_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_obj = Config(active_level=ActiveLevel.WARP)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: None)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "FAIL" in result.output


def test_status_bridge_running_node_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live bridge shows its URL in the human output and true in JSON."""
    import json

    cfg_obj = Config(
        active_level=ActiveLevel.NODE,
        current_node_id="n1",
        profiles=[_profile()],
    )
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.proxy, "load_pid", lambda: 1)
    monkeypatch.setattr(cli.proxy, "is_running", lambda pid: True)
    monkeypatch.setattr(cli.warp, "is_installed", lambda: False)
    monkeypatch.setattr(
        cli.cascade, "probe_connectivity", lambda c: {"ok": True, "ip": "1.2.3.4"}
    )
    monkeypatch.setattr(cli.bridge, "running", lambda c: True)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "bridge: running (http://127.0.0.1:7891/v1)" in result.output

    result_json = runner.invoke(cli.app, ["status", "--json"])
    assert result_json.exit_code == 0
    data = json.loads(result_json.output)
    assert data["bridge_running"] is True


def test_update(monkeypatch: pytest.MonkeyPatch) -> None:
    prof = _profile()
    monkeypatch.setattr(
        cli.cfg, "load_config", lambda: Config(profiles=[prof], top_limit=10)
    )

    def fake_refresh(
        p: Profile, cfg_obj: object, on_stage: object = None, on_progress: object = None
    ) -> None:
        p.__setattr__("nodes", [])
        if callable(on_stage):
            on_stage("tcp", 0, 0)

    monkeypatch.setattr(cli.refresh, "refresh_profile", fake_refresh)
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: None)
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 0
    assert "p1" in result.output
    assert "refreshing 'p1'" in result.output
    assert "verify tcp: 0/0" in result.output
    assert "peak 10 (config limit)" in result.output


def test_update_no_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    result = runner.invoke(cli.app, ["update"])
    assert result.exit_code == 1
    assert "No matching enabled profiles" in result.output


def test_run_no_command(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(cli.app, ["run"])
    assert result.exit_code == 2


def test_run_starts_and_executes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_obj = Config(active_level=ActiveLevel.NONE)
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.cascade, "level_serving", lambda c: False)
    monkeypatch.setattr(cli.cascade, "start", lambda f, d: None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, env=None: type("P", (), {"returncode": 0})(),
    )
    result = runner.invoke(cli.app, ["run", "--", "true"])
    assert result.exit_code == 0
    assert "running:" in result.output


def test_status_verbose_shows_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_obj = Config(
        active_level=ActiveLevel.NODE, current_node_id="n1", profiles=[_profile()]
    )
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/sing-box" if name == "sing-box" else None,
    )
    monkeypatch.setattr(cli.warp, "is_installed", lambda: True)
    monkeypatch.setattr(cli.warp, "status", lambda: WarpStatus.CONNECTED)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    monkeypatch.setattr(cli.proxy, "load_pid", lambda: None)
    monkeypatch.setattr(cli.proxy, "is_running", lambda pid: False)
    monkeypatch.setattr(cli.bridge, "running", lambda c: False)
    result = runner.invoke(cli.app, ["status", "--verbose"])
    assert result.exit_code == 0
    assert "openrot status --verbose" in result.output
    assert "sing-box" in result.output
    assert "WARP" in result.output
    assert "proxy" in result.output


def test_probe_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_obj = Config(
        active_level=ActiveLevel.NODE, current_node_id="n1", profiles=[_profile()]
    )
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "is_installed", lambda: False)
    monkeypatch.setattr(cli.cascade, "level_serving", lambda c: True)
    monkeypatch.setattr(cli.proxy, "load_pid", lambda: 1)
    monkeypatch.setattr(cli.node_ops, "node_label", lambda n: "label")

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        content = b"{}"
        elapsed = type("E", (), {"total_seconds": lambda s: 0.1})()

        def json(self) -> dict[str, str]:
            return {"ip": "1.2.3.4"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(cli.probe_core.httpx.Client, "get", lambda self, url: _Resp())
    result = runner.invoke(cli.app, ["probe", "https://example.test"])
    assert result.exit_code == 0
    assert "egress ip: 1.2.3.4" in result.output


def test_probe_runs_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    node = _node()
    node.status = NodeStatus.UNKNOWN
    cfg_obj = Config(active_level=ActiveLevel.NODE, profiles=[_profile(nodes=[node])])
    monkeypatch.setattr(cli.cfg, "load_config", lambda: cfg_obj)
    monkeypatch.setattr(cli.warp, "is_installed", lambda: False)
    monkeypatch.setattr(cli.cascade, "level_serving", lambda c: False)
    monkeypatch.setattr(cli.cascade, "start_node", lambda foreground: None)
    monkeypatch.setattr(cli.node_ops, "node_label", lambda n: "label")
    monkeypatch.setattr(cli.proxy, "load_pid", lambda: 123)
    monkeypatch.setattr(cli.rotator, "pick", lambda c: node)
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: None)

    def fake_test_all(c: Config, on_stage: object = None) -> int:
        assert on_stage is not None
        for n in c.all_nodes():
            n.status = NodeStatus.ALIVE
            n.latency_ms = 5.0
        if callable(on_stage):
            on_stage("tcp", 1, 1)
            on_stage("probe", 1, 1)
        return 1

    monkeypatch.setattr(cli.health, "test_all", fake_test_all)
    result = runner.invoke(cli.app, ["probe", "https://example.test"])
    assert result.exit_code == 0
    assert "verify tcp: 1/1" in result.output
    assert "verify probe: 1/1" in result.output


def test_warp_install_already(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.warp, "is_installed", lambda: True)
    monkeypatch.setattr(cli.warp, "bin_path", lambda: "/usr/bin/warp-cli")
    result = runner.invoke(cli.app, ["warp", "install"])
    assert result.exit_code == 0
    assert "already installed" in result.output


def test_warp_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cli.warp, "connect", lambda: True)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    result = runner.invoke(cli.app, ["warp", "on"])
    assert result.exit_code == 0
    assert "enabled and connected" in result.output


def test_warp_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    monkeypatch.setattr(cli.cfg, "save_config", lambda c, path=None: None)
    monkeypatch.setattr(cli.warp, "disconnect", lambda: True)
    result = runner.invoke(cli.app, ["warp", "off"])
    assert result.exit_code == 0
    assert "disconnected and disabled" in result.output


def test_warp_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config())
    monkeypatch.setattr(cli.warp, "status", lambda: WarpStatus.DISCONNECTED)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: None)
    result = runner.invoke(cli.app, ["warp", "status"])
    assert result.exit_code == 0
    assert "WARP status" in result.output


def test_warp_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(cli.cfg, "load_config", lambda: Config(warp_enabled=True))
    monkeypatch.setattr(cli.warp, "status", lambda: WarpStatus.CONNECTED)
    monkeypatch.setattr(cli.warp, "current_ip", lambda: "9.9.9.9")
    result = runner.invoke(cli.app, ["warp", "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {"status": "connected", "enabled": True, "ip": "9.9.9.9"}
