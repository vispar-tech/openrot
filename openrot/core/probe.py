"""Interactive step-by-step diagnostics for the active proxy stack.

``openrot probe <url>`` walks the "what is serving right now" decision tree and
prints each step: config, WARP status, direct connectivity, level selection,
and the request through the local proxy. The logic lives in ``core`` so the CLI
command stays a thin wrapper and the flow stays unit-testable.
"""

from operator import attrgetter

import httpx
from rich.console import Console

from openrot import config as cfg
from openrot.config import ActiveLevel, Config, NodeStatus, Strategy
from openrot.core import cascade, health, nodes, proxy, rotator
from openrot.providers import warp

console = Console()


def stage(title: str) -> None:
    """Print a bold section heading for one probe step."""
    console.print(f"\n[bold]▶ {title}[/bold]")


def node_desc(n: cfg.Node) -> str:
    """Human-readable description of a node (label, protocol, latency)."""
    ms = f"{n.latency_ms:.1f}ms" if n.latency_ms is not None else "-"
    return f"{nodes.node_label(n)} [{n.protocol.value}] {ms}"


def pipeline_stage(stage_name: str, kept: int, total: int) -> None:
    """Stage callback for ``verify``: print the final kept/total tally."""
    console.print(f"verify {stage_name}: {kept}/{total}")


def direct_ip() -> None:
    """Probe the egress IP without the proxy (direct connectivity baseline)."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get("https://api.ipify.org?format=json")
            resp.raise_for_status()
            ip = resp.json().get("ip", "?")
    except httpx.HTTPError:
        ip = "?"
    console.print(f"  direct ip: {ip}")


def choose_warp(cfg_obj: Config) -> bool:
    """Try to activate WARP for the probe; True when it came up."""
    if not cfg_obj.warp_enabled:
        console.print("  warp disabled in config -> skip")
        return False
    if not warp.is_installed():
        console.print("  warp-cli not found -> skip")
        return False
    console.print(f"  warp-cli: {warp.bin_path()} ({warp.status().value})")
    console.print("  connecting WARP (proxy mode)...")
    if cascade.start_warp(False):
        return True
    console.print("  warp connect failed -> fall back to node chain")
    return False


def chain(cfg_obj: Config) -> Config:
    """Describe and health-check the node chain, then pick a node."""
    for prof in sorted(cfg_obj.profiles, key=attrgetter("priority")):
        alive = sum(1 for n in prof.nodes if n.status == NodeStatus.ALIVE)
        console.print(
            f"  [{prof.name}] {prof.kind.value} prio={prof.priority}: "
            f"{alive}/{len(prof.nodes)} alive"
        )
    rule = (
        "lowest latency (urltest)"
        if cfg_obj.strategy == Strategy.URLTEST
        else "first alive in chain order (fallback)"
    )
    console.print(f"  strategy={cfg_obj.strategy.value} -> {rule}")
    if not any(n.status == NodeStatus.ALIVE for n in cfg_obj.all_nodes()):
        console.print("  no alive node yet -> running health check...")
        health.test_all(cfg_obj, on_stage=pipeline_stage)
        cfg.save_config(cfg_obj)
        cfg_obj = cfg.load_config()
        for prof in sorted(cfg_obj.profiles, key=attrgetter("priority")):
            alive = sum(1 for n in prof.nodes if n.status == NodeStatus.ALIVE)
            console.print(
                f"  [{prof.name}] after check: {alive}/{len(prof.nodes)} alive"
            )
    for n in cfg_obj.all_nodes():
        if n.status == NodeStatus.ALIVE:
            console.print(f"    alive: {node_desc(n)}")
    picked = rotator.pick(cfg_obj)
    if picked is not None:
        console.print(f"  picked: {node_desc(picked)}")
    else:
        console.print("[red]  no alive node to pick[/red]")
    return cfg_obj


def seek(cfg_obj: Config, url: str) -> None:
    """Request `url` through the local proxy and report the result."""
    if not cascade.level_serving(cfg_obj):
        console.print("  no active stack -> nothing to request through")
        return
    with httpx.Client(proxy=f"http://127.0.0.1:{cfg_obj.port}", timeout=20) as client:
        console.print(f"  requesting {url}")
        resp = client.get(url)
        console.print(
            f"  {resp.status_code} {resp.reason_phrase} "
            f"({len(resp.content)} bytes, {resp.elapsed.total_seconds():.2f}s)"
        )
        try:
            ip = resp.json().get("ip", "?")
        except Exception:
            ip = "?"
    console.print(f"  egress ip: {ip}")


def run(url: str) -> None:
    """Probe the active stack end-to-end, printing every step taken."""
    cfg_obj = cfg.load_config()
    stage("config")
    console.print(
        f"  port={cfg_obj.port}, strategy={cfg_obj.strategy.value}, "
        f"singbox={cfg_obj.singbox_bin}, warp_enabled={cfg_obj.warp_enabled}"
    )

    stage("warp status")
    if warp.is_installed():
        console.print(f"  warp-cli: {warp.bin_path()} ({warp.status().value})")
    else:
        console.print("  warp-cli: not found (host only)")

    direct_ip()

    stage("choose level")
    if cascade.level_serving(cfg_obj):
        if cfg_obj.active_level == ActiveLevel.WARP:
            console.print("  stack already up: WARP")
        else:
            current = nodes.current_node(cfg_obj)
            label = nodes.node_label(current) if current else "?"
            print_pid = proxy.load_pid()
            console.print(
                f"  stack already up: node {label}, pid {print_pid}, "
                f"127.0.0.1:{cfg_obj.port}"
            )
    elif choose_warp(cfg_obj):
        cfg_obj = cfg.load_config()
    else:
        cfg_obj = chain(cfg_obj)
        if not cfg_obj.all_nodes():
            console.print(
                "[red]  no nodes configured. add: 'openrot profile add'[/red]"
            )
            return
        console.print("  starting sing-box chain...")
        try:
            cascade.start_node(False)
        except SystemExit:
            console.print("[red]  chain failed[/red]")
            return
        cfg_obj = cfg.load_config()
        current = nodes.current_node(cfg_obj)
        label = nodes.node_label(current) if current else "?"
        pid = proxy.load_pid()
        console.print(f"  serving via {label}, pid {pid}, 127.0.0.1:{cfg_obj.port}")

    stage("request")
    seek(cfg_obj, url)
    stage("left running")
    console.print("  'openrot stop' to quit")
