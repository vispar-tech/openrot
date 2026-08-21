"""Integration tests: full CLI loop against real sources and real sing-box.

The happy path exercises `profile add` -> `update` -> `test` -> `probe`
(assert a real egress IP), then fakes a node failure (kill the serving
sing-box + mark the current node DEAD) and verifies `rotate`. External
sources and the network are not under our control, so the test skips
gracefully when sing-box is missing or no node stays alive.
"""

import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from openrot.config import (
    Config,
    Node,
    NodeStatus,
    ProfileKind,
    load_config,
    save_config,
)

pytestmark = pytest.mark.integration

EGRESS_URL = "https://api.ipify.org?format=json"
RELAY_WHITE = (
    "https://raw.githack.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-all.txt"
)
RELAY_BLACK = (
    "https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/BLACK_VLESS_RUS.txt"
)
PROXY_PROXIFLY = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/http/data.txt"
TRIM_PER_PROFILE = 6
EGRESS_RE = re.compile(r"egress ip: (\d+\.\d+\.\d+\.\d+)")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(basedir: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENROT_DIR"] = str(basedir)
    env["OPENROT_PORT"] = str(port)
    return env


def _openrot(
    basedir: Path, port: int, *argv: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "openrot", *argv],
        cwd=REPO_ROOT,
        env=_env(basedir, port),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def isolated(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    port = _free_port()
    basedir = tmp_path / ".openrot"
    yield basedir, port
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        _openrot(basedir, port, "stop", "--yes", timeout=30)


def _add_profiles(basedir: Path, port: int) -> None:
    for name, url, kind in (
        ("white", RELAY_WHITE, "relay"),
        ("black", RELAY_BLACK, "relay"),
        ("proxifly", PROXY_PROXIFLY, "proxy"),
    ):
        proc = _openrot(
            basedir, port, "profile", "add", name, url, "--kind", kind, timeout=60
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


def _disable_warp(cfg_path: Path) -> None:
    cfg_obj = load_config(path=cfg_path)
    cfg_obj.warp_enabled = False
    save_config(cfg_obj, cfg_path)


TRANS_NET_ERRORS = (
    "TimeoutException",
    "ConnectError",
    "ReadTimeout",
    "NetworkError",
    "ProxyError",
    "RemoteProtocolError",
    "ConnectionResetError",
    "BrokenPipeError",
)


def _skip_on_transient_error(proc: subprocess.CompletedProcess[str]) -> None:
    """Skip when the subprocess failed only due to a transient network error."""
    if proc.returncode == 0:
        return
    blob = (proc.stdout or "") + (proc.stderr or "")
    if any(err in blob or err.lower() in blob.lower() for err in TRANS_NET_ERRORS):
        pytest.skip(f"transient network error in integration step: {blob[-400:]}")


def _update_and_trim(basedir: Path, port: int, cfg_path: Path) -> None:
    proc = _openrot(basedir, port, "update", timeout=240)
    _skip_on_transient_error(proc)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cfg_obj = load_config(path=cfg_path)
    if not cfg_obj.all_nodes():
        pytest.skip("no nodes fetched from external sources right now")
    for prof in cfg_obj.profiles:
        if prof.kind == ProfileKind.RELAY:
            prof.nodes = prof.nodes[:TRIM_PER_PROFILE]
    save_config(cfg_obj, cfg_path)


def _alive_nodes(cfg_path: Path) -> list[Node]:
    cfg_obj: Config = load_config(path=cfg_path)
    return [n for n in cfg_obj.all_nodes() if n.status == NodeStatus.ALIVE]


def _expect_egress(basedir: Path, port: int) -> str:
    proc = _openrot(basedir, port, "probe", EGRESS_URL, timeout=120)
    _skip_on_transient_error(proc)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    match = EGRESS_RE.search(proc.stdout)
    assert match, proc.stdout
    return match.group(1)


def _fail_and_rotate(
    basedir: Path, port: int, cfg_path: Path, alive: list[Node]
) -> None:
    if len(alive) < 2:
        pytest.skip("fewer than 2 alive nodes; cannot verify rotation")
    pid_path = basedir / "openrot.pid"
    assert pid_path.exists(), "no proxy pid file after probe"
    os.kill(int(pid_path.read_text().strip()), signal.SIGKILL)

    cfg_obj = load_config(path=cfg_path)
    assert cfg_obj.current_node_id is not None
    killed_id = cfg_obj.current_node_id
    current = next(n for n in cfg_obj.all_nodes() if n.id == killed_id)
    current.status = NodeStatus.DEAD
    save_config(cfg_obj, cfg_path)

    proc = _openrot(basedir, port, "rotate", timeout=60)
    _skip_on_transient_error(proc)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "rotated to" in proc.stdout, proc.stdout

    cfg_obj = load_config(path=cfg_path)
    assert cfg_obj.current_node_id not in (None, killed_id)


def test_full_loop_through_real_sources(isolated: tuple[Path, int]) -> None:
    if shutil.which("sing-box") is None:
        pytest.skip("sing-box is not installed; skipping integration test")
    basedir, port = isolated
    cfg_path = basedir / "config.yaml"

    _add_profiles(basedir, port)
    _disable_warp(cfg_path)
    _update_and_trim(basedir, port, cfg_path)
    proc = _openrot(basedir, port, "test", timeout=300)
    _skip_on_transient_error(proc)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    alive = _alive_nodes(cfg_path)
    if not alive:
        pytest.skip("no alive nodes from external sources right now")

    _expect_egress(basedir, port)
    _fail_and_rotate(basedir, port, cfg_path, alive)
    _expect_egress(basedir, port)
