import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from openrot import config as cfg
from openrot.config import Node, NodeProtocol
from openrot.core.singbox import (
    generate_free_config,
    generate_singbox_config,
    write_config,
)
from openrot.providers import free
from openrot.providers.vless import VlessNode, parse_vless


def _launch(config_data: dict[str, object], singbox_bin: str) -> int:
    cfg_path = write_config(config_data)
    proc = subprocess.Popen(  # noqa: S603
        [singbox_bin, "run", "-c", str(cfg_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    if proc.poll() is not None:
        raise RuntimeError(
            f"sing-box exited immediately (returncode {proc.returncode}). "
            f"Check config or if port is free"
        )
    return proc.pid


def start_proxy(node: VlessNode, port: int, singbox_bin: str) -> int:
    """Start sing-box serving a vless relay node; return its pid."""
    return _launch(generate_singbox_config(node, port), singbox_bin)


def start_free_proxy(
    protocol: str, host: str, port: int, listen_port: int, singbox_bin: str
) -> int:
    """Start sing-box with an http/socks5 outbound to a forward proxy."""
    return _launch(generate_free_config(protocol, host, port, listen_port), singbox_bin)


def start_node(node: Node, port: int, singbox_bin: str) -> int:
    """Start any node by protocol: relay (sing-box) or proxy (http/socks5 outbound)."""
    if node.protocol in (NodeProtocol.HTTP, NodeProtocol.SOCKS5):
        host, remote_port = free.host_port(node.raw)
        if host is None or remote_port is None:
            raise RuntimeError(f"invalid proxy node address: {node.raw}")
        return start_free_proxy(
            node.protocol.value, host, remote_port, port, singbox_bin
        )
    return start_proxy(parse_vless(node.raw), port, singbox_bin)


def is_running(pid: int) -> bool:
    """Return True when a process with the given pid is alive.

    Uses ``os.kill(pid, 0)`` (no ``kill`` binary needed, so it works in minimal
    containers), treating permission errors as "alive" since the process exists.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_pid(pid: int, path: Path) -> None:
    """Write `pid` to `path` atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(str(pid))
    Path(tmp).replace(path)


def save_pid(pid: int, path: Path = cfg.PID_PATH) -> None:
    """Write the current proxy pid to `path`."""
    _write_pid(pid, path)


def load_pid(path: Path = cfg.PID_PATH) -> int | None:
    """Read a proxy pid from `path`, or None when absent or invalid."""
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def save_daemon_pid(pid: int, path: Path = cfg.DAEMON_PID_PATH) -> None:
    """Write the daemon pid to `path`."""
    _write_pid(pid, path)


def load_daemon_pid(path: Path = cfg.DAEMON_PID_PATH) -> int | None:
    """Read the daemon pid from `path`, or None when absent or invalid."""
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def stop_proxy(pid: int | None = None) -> bool:
    """Terminate the proxy process if it is running."""
    if pid is None:
        pid = load_pid()
    if pid is None or not is_running(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False
