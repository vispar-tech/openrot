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
        log_path=cfg.LOG_PATH,
        rotate_log=True,
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
        console.print("[yellow]warp disabled in config, using node chain[/yellow]")
        return False
    if not warp.is_installed():
        console.print("WARP: available on host only (not found). Skipping.")
        return False
    if not warp.connect():
        events.warning("warp connect failed; falling back to node chain")
        console.print("[red][warp] failed to connect, falling back to node chain[/red]")
        return False

    w_host, w_port = warp.proxy_address()
    try:
        pid = proxy.start_free_proxy(
            "socks5", w_host, w_port, cfg_obj.port, cfg_obj.singbox_bin
        )
    except FileNotFoundError:
        events.warning(
            "warp in proxy mode: sing-box not found (%s)", cfg_obj.singbox_bin
        )
        console.print(
            f"[red]sing-box not found[/red] (looked for '{cfg_obj.singbox_bin}'). "
            "Install it: brew install sing-box"
        )
        return False
    except RuntimeError as exc:
        events.warning("warp listener failed to start: %s", exc)
        console.print(f"[red]{exc}[/red]")
        return False
    proxy.save_pid(pid)
    cfg_obj.active_level = ActiveLevel.WARP
    cfg.save_config(cfg_obj)
    ip = warp.current_ip()
    suffix = f" (IP {ip})" if ip else ""
    events.info(
        "warp active in proxy mode (127.0.0.1:%d -> socks5://%s:%d, ip=%s)",
        cfg_obj.port,
        w_host,
        w_port,
        ip or "-",
    )
    console.print(
        f"[warp] active via 127.0.0.1:{cfg_obj.port} → "
        f"socks5://{w_host}:{w_port}{suffix}"
    )
    if foreground:
        warp_health_loop()
    return True


def start_node(foreground: bool) -> None:
    """Start the node chain: pick from profiles by priority."""
    cfg_obj = cfg.load_config()
    if not cfg_obj.all_nodes():
        console.print(
            "[red]no nodes configured. Add a profile via 'openrot profile add'[/red]"
        )
        raise SystemExit(1)

    if not any(n.status == NodeStatus.ALIVE for n in cfg_obj.all_nodes()):
        console.print(
            "[yellow]node: no alive node yet, running health check...[/yellow]"
        )
        health.test_all(cfg_obj)
        cfg_obj = cfg.load_config()
        cfg.save_config(cfg_obj)

    node = rotator.pick(cfg_obj)
    if node is None:
        console.print(
            "[red][node] no alive node available. Run 'openrot test' or "
            "'openrot update'[/red]"
        )
        raise SystemExit(1)

    cfg_obj.active_level = ActiveLevel.NODE
    cfg.save_config(cfg_obj)
    try:
        pid = launch(cfg_obj, node)
    except FileNotFoundError:
        console.print(
            f"[red]sing-box not found[/red] (looked for '{cfg_obj.singbox_bin}'). "
            "Install it: brew install sing-box"
        )
        raise SystemExit(1) from None
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    if foreground:
        console.print(
            f"proxy: 127.0.0.1:{cfg_obj.port} (pid {pid}), "
            f"node: {nodes.node_label(node)}. Ctrl-C to stop."
        )
        node_health_loop()
        return

    console.print(f"Proxy started, pid {pid}, 127.0.0.1:{cfg_obj.port}")


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
                console.print(
                    "[yellow][node] current node no longer in the pool, "
                    "rotating[/yellow]"
                )
                with contextlib.suppress(SystemExit):
                    rotate()
            continue

        alive, _ = health.check_node(current, snapshot)
        reached, fails = cfg.update_config(
            cfg.CONFIG_PATH, partial(_commit_health, node_id=current.id, alive=alive)
        ) or (False, 0)
        if reached:
            label = nodes.node_label(current)
            console.print(
                f"[yellow]node {label} failed {fails} times, rotating[/yellow]"
            )
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
        console.print("[yellow][warp] dropped, falling back to node chain[/yellow]")
        proxy.stop_proxy()
        start_node(True)
        return


def rotate(first: bool = False) -> None:
    """Rotate WARP IP, or pick the next node by priority in the chain.

    ``first=True`` resets the queue to the start: serve the first node of the
    chain (no exclusion of the current one).

    Thread-safe: concurrent calls are serialized; when a rotation is already
    in progress the caller returns immediately without blocking.
    """
    if not _rotate_lock.acquire(blocking=False):
        events.warning("rotate skipped: another rotation already in progress")
        return
    try:
        _rotate_inner(first)
    finally:
        _rotate_lock.release()


def _rotate_inner(first: bool) -> None:
    cfg_obj = cfg.load_config()
    if cfg_obj.active_level == ActiveLevel.WARP:
        if warp.rotate():
            ip = warp.current_ip()
            events.info("warp rotated, ip=%s", ip or "-")
            suffix = f" (IP {ip})" if ip else ""
            console.print(f"[warp] Rotated WARP{suffix}")
        else:
            events.warning("warp rotation failed")
            console.print("[red][warp] WARP rotation failed[/red]")
        return

    current = nodes.current_node(cfg_obj)
    current_id = current.id if current else None
    proxy.stop_proxy()
    cfg_obj.current_node_id = None

    if not any(n.status == NodeStatus.ALIVE for n in cfg_obj.all_nodes()):
        console.print("[yellow]node: no alive node, running health check...[/yellow]")
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
        console.print(
            "[yellow]node: no alive alternative, re-running health check...[/yellow]"
        )
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
        console.print("[red][node] no alive node available[/red]")
        raise SystemExit(1)

    try:
        pid = launch(cfg_obj, node)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    events.info("rotated to node %s (pid %d)", nodes.node_label(node), pid)
    idx = rotator.index_of(cfg_obj, node)
    total = len(rotator.chain(cfg_obj))
    pos = f", {idx}/{total}" if idx is not None else ""
    console.print(
        f"[node] rotated to {nodes.node_label(node)}, pid {pid}, "
        f"127.0.0.1:{cfg_obj.port}{pos}"
    )


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
