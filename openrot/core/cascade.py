import contextlib
import threading
import time
from functools import partial

import httpx
from rich.console import Console

from openrot import config as cfg
from openrot import log, signals
from openrot.config import ActiveLevel, Config, Node, NodeStatus, Strategy
from openrot.core import daemon, health, nodes, proxy, refresh, rotator
from openrot.providers import warp

console = Console()
events = log.get_logger()
_rotate_lock = threading.Lock()
_rotate_in_progress = threading.Event()


def launch(cfg_obj: Config, node: Node) -> int:
    """Start the proxy for a node, record the pid and current node."""
    pid = proxy.start_node(node, cfg_obj.port, cfg_obj.singbox_bin)
    proxy.save_pid(pid)
    cfg_obj.current_node_id = node.id
    cfg.save_config(cfg_obj)
    return pid


def daemonize() -> None:
    """Fork the 'start cascade' loop into a background daemon process."""
    daemon.start(
        name="cascade",
        pid_path=cfg.DAEMON_PID_PATH,
    )


def _shutdown() -> None:
    """Stop the proxy and WARP, and reset the persisted active state."""
    proxy.stop_proxy()
    warp.disconnect()
    cfg_obj = cfg.load_config()
    cfg_obj.active_level = ActiveLevel.NONE
    cfg_obj.current_node_id = None
    cfg.save_config(cfg_obj)


def start(foreground: bool, daemon: bool) -> None:
    """Start the cascade: WARP (if enabled) → node chain by priority.

    ``foreground=True`` blocks in the health-check loop and echoes the events
    log to the terminal; ``daemon=True`` forks that loop into a detached
    background process.
    """
    if daemon and not foreground:
        daemonize()
        return
    if foreground:
        log.console_echo()
    already = proxy.load_pid()
    if already is not None and proxy.is_running(already):
        console.print(
            f"[yellow]proxy already running (pid {already}, "
            f"127.0.0.1:{cfg.load_config().port})[/yellow]"
        )
        return
    cfg_obj = cfg.load_config()
    if cfg_obj.update_interval > 0 and foreground:
        threading.Thread(
            target=refresh.run_scheduler, name="openrot-scheduler", daemon=True
        ).start()
    if foreground:
        signals.keyboard_on_sigterm()
        try:
            if not start_warp(foreground):
                start_node(foreground)
        except KeyboardInterrupt:
            console.print("\nstopping proxy...")
            _shutdown()
            raise SystemExit(0) from None
        return
    if not start_warp(foreground):
        start_node(foreground)


def start_warp(foreground: bool) -> bool:
    """Connect WARP in proxy mode and front it with the local mixed proxy."""
    cfg_obj = cfg.load_config()
    if not cfg_obj.warp_enabled:
        events.info("warp disabled in config, using node chain")
        return False
    if not warp.is_installed():
        events.info("WARP: available on host only (not found). Skipping.")
        return False
    if not warp.connect():
        events.warning("warp connect failed; falling back to node chain")
        return False

    w_host, w_port = warp.proxy_address()
    try:
        pid = proxy.start_free_proxy(
            "socks5", w_host, w_port, cfg_obj.port, cfg_obj.singbox_bin
        )
    except FileNotFoundError:
        events.warning(
            "sing-box not found (%s). Install it: brew install sing-box",
            cfg_obj.singbox_bin,
        )
        return False
    except RuntimeError as exc:
        events.warning("warp listener failed to start: %s", exc)
        return False
    proxy.save_pid(pid)
    cfg_obj.active_level = ActiveLevel.WARP
    cfg.save_config(cfg_obj)
    ip = warp.current_ip()
    events.info(
        "warp active in proxy mode (127.0.0.1:%d -> socks5://%s:%d, ip=%s)",
        cfg_obj.port,
        w_host,
        w_port,
        ip or "-",
    )
    if foreground:
        warp_health_loop()
    return True


def start_node(foreground: bool) -> None:
    """Start the node chain: pick from profiles by priority."""
    cfg_obj = cfg.load_config()
    if not cfg_obj.all_nodes():
        events.warning("no nodes configured. Add a profile via 'openrot profile add'")
        raise SystemExit(1)

    if not any(n.status == NodeStatus.ALIVE for n in cfg_obj.all_nodes()):
        events.info("node: no alive node yet, running health check...")
        health.test_all(cfg_obj)
        cfg_obj = cfg.load_config()
        cfg.save_config(cfg_obj)

    node = rotator.pick(cfg_obj)
    if node is None:
        events.warning(
            "no alive node available. Run 'openrot test' or 'openrot update'"
        )
        raise SystemExit(1)

    cfg_obj.active_level = ActiveLevel.NODE
    cfg.save_config(cfg_obj)
    try:
        pid = launch(cfg_obj, node)
    except FileNotFoundError:
        events.warning(
            "sing-box not found (%s). Install it: brew install sing-box",
            cfg_obj.singbox_bin,
        )
        raise SystemExit(1) from None
    except RuntimeError as exc:
        events.warning("%s", exc)
        raise SystemExit(1) from exc

    if foreground:
        console.print(
            f"proxy: 127.0.0.1:{cfg_obj.port} (pid {pid}), "
            f"node: {nodes.node_label(node)}. Ctrl-C to stop."
        )
        node_health_loop()
        return

    events.info("proxy started, pid %d, 127.0.0.1:%d", pid, cfg_obj.port)


def _commit_health(fresh: cfg.Config, node_id: str, alive: bool) -> tuple[bool, int]:
    """Record one health-sample result for a node on a fresh config.

    Returns (reached_fail_threshold, fails_after). The bool flags whether the
    caller should rotate; updating happens atomically under the config lock.
    """
    node = next((n for n in fresh.all_nodes() if n.id == node_id), None)
    if node is None:
        return False, 0
    if alive:
        node.fails = 0
        return False, 0
    node.fails += 1
    if node.fails >= fresh.fail_threshold:
        node.status = NodeStatus.DEAD
        return True, node.fails
    return False, node.fails


def node_health_loop() -> None:
    """Periodically check the current node and rotate once it keeps failing."""
    while True:
        snapshot = cfg.load_config()
        time.sleep(snapshot.health_interval)
        current = nodes.current_node(snapshot)
        if current is None:
            if snapshot.active_level == ActiveLevel.NODE:
                events.warning("current node no longer in the pool, rotating")
                with contextlib.suppress(SystemExit):
                    rotate()
            continue

        alive, _ = health.check_node(current, snapshot)
        reached, fails = cfg.update_config(
            cfg.CONFIG_PATH, partial(_commit_health, node_id=current.id, alive=alive)
        ) or (False, 0)
        if reached:
            label = nodes.node_label(current)
            events.warning("node %s failed %d times, rotating", label, fails)
            with contextlib.suppress(SystemExit):
                rotate()


def warp_health_loop() -> None:
    """Fall back to the node chain when WARP connectivity is lost."""
    while True:
        cfg_obj = cfg.load_config()
        time.sleep(cfg_obj.health_interval)
        if warp.is_connected():
            continue
        events.warning("warp dropped; falling back to node chain")
        proxy.stop_proxy()
        start_node(True)
        return


def rotate(first: bool = False) -> bool:
    """Rotate WARP IP, or pick the next node by priority in the chain.

    ``first=True`` resets the queue to the start: serve the first node of the
    chain (no exclusion of the current one).

    Thread-safe: concurrent calls are serialized. When a rotation is already
    in progress the caller waits for it to finish and returns ``False``
    (the cascade has already been rotated by someone else). Returns ``True``
    when this caller actually performed the rotation.
    """
    with _rotate_lock:
        if _rotate_in_progress.is_set():
            waiting = True
        else:
            _rotate_in_progress.set()
            waiting = False
    if waiting:
        _rotate_in_progress.wait()
        return False
    try:
        _rotate_inner(first)
        return True
    finally:
        _rotate_in_progress.clear()


def _rotate_inner(first: bool) -> None:
    cfg_obj = cfg.load_config()
    if cfg_obj.active_level == ActiveLevel.WARP:
        if warp.rotate():
            ip = warp.current_ip()
            events.info("warp rotated, ip=%s", ip or "-")
        else:
            events.warning("warp rotation failed")
        return

    current = nodes.current_node(cfg_obj)
    current_id = current.id if current else None
    proxy.stop_proxy()
    cfg_obj.current_node_id = None

    if not any(n.status == NodeStatus.ALIVE for n in cfg_obj.all_nodes()):
        events.info("node: no alive node, running health check...")
        health.test_all(cfg_obj)
        cfg_obj = cfg.load_config()
        cfg.save_config(cfg_obj)

    is_urltest = (cfg_obj.strategy or Strategy.FALLBACK) == Strategy.URLTEST
    if first:
        node = rotator.first_node(cfg_obj)
    elif is_urltest:
        node = rotator.pick(cfg_obj, exclude_id=current_id)
    else:
        node = rotator.next_node(cfg_obj, current_id)
    if node is None:
        events.info("node: no alive alternative, re-running health check...")
        health.test_all(cfg_obj)
        cfg_obj = cfg.load_config()
        node = (
            rotator.first_node(cfg_obj)
            if first
            else rotator.next_node(cfg_obj, current_id)
        )
        cfg.save_config(cfg_obj)
    if node is None:
        events.warning("rotation failed: no alive node available")
        raise SystemExit(1)

    try:
        pid = launch(cfg_obj, node)
    except RuntimeError as exc:
        events.warning("%s", exc)
        raise SystemExit(1) from exc
    events.info("rotated to node %s (pid %d)", nodes.node_label(node), pid)


def stop() -> None:
    """Stop the cascade daemon and the current level: WARP or local proxy."""
    if daemon.stop(cfg.DAEMON_PID_PATH):
        console.print("daemon stopped")
    cfg_obj = cfg.load_config()
    if cfg_obj.active_level == ActiveLevel.WARP:
        proxy.stop_proxy()
        warp.disconnect()
        cfg_obj.active_level = ActiveLevel.NONE
        cfg.save_config(cfg_obj)
        console.print("WARP disconnected")
        return
    if proxy.stop_proxy():
        cfg_obj.current_node_id = None
        cfg_obj.active_level = ActiveLevel.NONE
        cfg.save_config(cfg_obj)
        console.print("Proxy stopped")
    else:
        console.print("Proxy not running")


def level_serving(cfg_obj: Config) -> bool:
    """Return True when the current active level is actually serving."""
    if cfg_obj.active_level == ActiveLevel.WARP:
        pid = proxy.load_pid()
        return warp.is_connected() and pid is not None and proxy.is_running(pid)
    if cfg_obj.active_level == ActiveLevel.NODE:
        pid = proxy.load_pid()
        return pid is not None and proxy.is_running(pid)
    return False


def probe_connectivity(cfg_obj: Config) -> dict[str, object]:
    """Fetch the egress IP through the local proxy to verify connectivity."""
    try:
        with httpx.Client(
            proxy=f"http://127.0.0.1:{cfg_obj.port}", timeout=15
        ) as client:
            resp = client.get("https://api.ipify.org?format=json")
            resp.raise_for_status()
            ip = resp.json().get("ip", "?")
            return {"ok": True, "ip": ip}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": exc.__class__.__name__}
