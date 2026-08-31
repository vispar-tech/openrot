import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from openrot import __version__, self_update, signals
from openrot import config as cfg
from openrot.config import (
    ActiveLevel,
    NodeStatus,
    Profile,
    ProfileKind,
)
from openrot.core import bridge, cascade, health, proxy, refresh, rotator, verify
from openrot.core import nodes as node_ops
from openrot.core import probe as probe_core
from openrot.providers import warp

app = typer.Typer(
    help=(
        "Local proxy rotator: routes traffic through node profiles, "
        "WARP as top priority"
    ),
    no_args_is_help=True,
    invoke_without_command=True,
    rich_markup_mode="rich",
)
profile_app = typer.Typer(
    help="Manage source profiles ([bold]relay[/bold] / [bold]proxy[/bold])",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(profile_app, name="profile")
console = Console()


@contextmanager
def activity(message: str) -> Iterator[None]:
    """Run a block under a transient spinner with `message`."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    )
    progress.add_task(message, total=None)
    with progress:
        yield


def _ensure_confirmed(prompt: str, yes: bool) -> None:
    if yes:
        return
    if not typer.confirm(prompt):
        console.print("cancelled")
        raise typer.Exit(code=1)


@app.callback()
def cli_main(
    _ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Root CLI entry point; handles --version."""
    if version:
        console.print(f"openrot {__version__}")
        raise typer.Exit


@profile_app.command("add")
def profile_add(
    name: str,
    url: str,
    *,
    kind: ProfileKind = typer.Option(ProfileKind.RELAY, "--kind", help="relay | proxy"),
    priority: int = typer.Option(0, "--priority", help="lower = higher priority"),
    interval: int | None = typer.Option(
        None, "--interval", help="refresh seconds; None = use config update_interval"
    ),
    disabled: bool = typer.Option(False, "--disabled", help="add as disabled"),
) -> None:
    """Add a source profile (node list config)."""
    cfg_obj = cfg.load_config()
    if node_ops.find_profile(cfg_obj, name):
        console.print(f"[red]profile '{name}' already exists[/red]")
        raise typer.Exit(code=1)
    cfg_obj.profiles.append(
        Profile(
            name=name,
            kind=kind,
            url=url,
            priority=priority,
            enabled=not disabled,
            interval=interval,
        )
    )
    cfg.save_config(cfg_obj)
    console.print(
        f"[green]Added profile '{name}' ({kind.value}, priority {priority})[/green]"
    )


@profile_app.command("list")
def profile_list(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List source profiles."""
    cfg_obj = cfg.load_config()
    if not cfg_obj.profiles:
        console.print(
            "No profiles. Use 'openrot profile add NAME URL --kind relay|proxy'"
        )
        return
    if as_json:
        rows = [
            {
                "id": p.id,
                "name": p.name,
                "kind": p.kind.value,
                "priority": p.priority,
                "enabled": p.enabled,
                "nodes_alive": sum(1 for n in p.nodes if n.status == NodeStatus.ALIVE),
                "nodes_total": len(p.nodes),
                "url": p.url,
            }
            for p in cfg_obj.profiles
        ]
        console.print(json.dumps(rows, indent=2, default=str))
        return
    table = Table(title="Profiles")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Priority")
    table.add_column("State")
    table.add_column("Nodes")
    table.add_column("URL")
    for p in cfg_obj.profiles:
        state = "[green]on[/green]" if p.enabled else "[red]off[/red]"
        n = sum(1 for node in p.nodes if node.status == NodeStatus.ALIVE)
        table.add_row(
            p.id,
            p.name,
            p.kind.value,
            str(p.priority),
            state,
            f"{n}/{len(p.nodes)}",
            p.url,
        )
    console.print(table)


@profile_app.command("remove")
def profile_remove(
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Remove a source profile and its nodes."""
    cfg_obj = cfg.load_config()
    if node_ops.find_profile(cfg_obj, name) is None:
        console.print(f"[red]profile '{name}' not found[/red]")
        raise typer.Exit(code=1)
    _ensure_confirmed(f"Remove profile '{name}' and its nodes?", yes)
    cfg_obj.profiles = [p for p in cfg_obj.profiles if p.name != name]
    cfg.save_config(cfg_obj)
    console.print(f"[green]Removed profile '{name}'[/green]")


@profile_app.command("set")
def profile_set(
    name: str,
    priority: int | None = typer.Option(
        None, "--priority", help="lower = higher priority"
    ),
    interval: int | None = typer.Option(
        None, "--interval", help="refresh seconds; None = use config update_interval"
    ),
    enabled: bool | None = typer.Option(
        None, "--enabled/--disabled", help="enable or disable the profile"
    ),
) -> None:
    """Update a profile: priority, refresh interval, enabled state."""
    cfg_obj = cfg.load_config()
    prof = node_ops.find_profile(cfg_obj, name)
    if prof is None:
        console.print(f"[red]profile '{name}' not found[/red]")
        raise typer.Exit(code=1)
    if priority is not None:
        prof.priority = priority
    if interval is not None:
        prof.interval = interval
    if enabled is not None:
        prof.enabled = enabled
    if priority is None and interval is None and enabled is None:
        console.print(
            "[yellow]nothing to change: pass --priority, --interval or "
            "--enabled/--disabled[/yellow]"
        )
        raise typer.Exit(code=1)
    cfg.save_config(cfg_obj)
    console.print(f"[green]Updated profile '{name}'[/green]")


@app.command("list")
def list_nodes(
    alive: bool = typer.Option(False, "--alive", help="Only show alive nodes"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List nodes across profiles."""
    cfg_obj = cfg.load_config()
    rows = []
    for prof in cfg_obj.profiles:
        for node in prof.nodes:
            if alive and node.status != NodeStatus.ALIVE:
                continue
            rows.append(
                {
                    "id": node.id,
                    "profile": prof.name,
                    "priority": node.priority,
                    "protocol": node.protocol.value,
                    "name": node_ops.node_label(node),
                    "address": node_ops.node_address(node),
                    "status": node.status.value,
                    "latency_ms": node.latency_ms,
                }
            )
    if not rows:
        if not cfg_obj.all_nodes():
            console.print(
                "No nodes configured. Use 'openrot profile add NAME URL "
                "--kind relay|proxy'"
            )
        else:
            console.print("No alive nodes. Run 'openrot test'")
        return
    if as_json:
        console.print(json.dumps(rows, indent=2, default=str))
        return
    table = Table(title="Nodes")
    table.add_column("#")
    table.add_column("Profile")
    table.add_column("Name")
    table.add_column("Address")
    table.add_column("Status")
    table.add_column("Latency")
    for i, row in enumerate(rows, 1):
        latency = f"{row['latency_ms']:.0f}ms" if row["latency_ms"] is not None else "-"
        table.add_row(
            str(i),
            str(row["profile"]),
            str(row["name"]),
            str(row["address"]),
            str(row["status"]),
            latency,
        )
    console.print(table)


@app.command()
def test(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Run health check on all nodes."""
    cfg_obj = cfg.load_config()
    if not cfg_obj.all_nodes():
        console.print(
            "No nodes configured. Use 'openrot profile add NAME URL --kind relay|proxy'"
        )
        raise typer.Exit(code=1)
    with activity("checking nodes..."):
        health.test_all(cfg_obj)
    cfg.save_config(cfg_obj)
    if as_json:
        rows = [
            {
                "profile": next(
                    (p.name for p in cfg_obj.profiles if node in p.nodes), "?"
                ),
                "name": node_ops.node_label(node),
                "status": node.status.value,
                "latency_ms": node.latency_ms,
                "fails": node.fails,
            }
            for node in cfg_obj.all_nodes()
        ]
        console.print(json.dumps(rows, indent=2, default=str))
        return
    table = Table(title="Health check")
    table.add_column("Profile")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Latency")
    table.add_column("Fails")
    for node in cfg_obj.all_nodes():
        latency = f"{node.latency_ms:.0f}ms" if node.latency_ms is not None else "-"
        profile_name = next((p.name for p in cfg_obj.profiles if node in p.nodes), "?")
        table.add_row(
            profile_name,
            node_ops.node_label(node),
            node.status.value,
            latency,
            str(node.fails),
        )
    console.print(table)


@app.command()
def start(
    mode: str = typer.Argument(help="cascade or bridge (429-rotation server)"),
    as_daemon: bool = typer.Option(
        False, "--daemon", help="run in the background instead of the foreground"
    ),
) -> None:
    """Start the cascade or the opencode bridge."""
    if mode == "bridge":
        if as_daemon:
            bridge.daemonize()
        else:
            bridge.serve()
        return
    if mode != "cascade":
        console.print(f"[red]unknown mode '{mode}'[/red] (cascade, bridge)")
        raise typer.Exit(1)
    cascade.start(not as_daemon, as_daemon)


@app.command()
def rotate(
    first: bool = typer.Option(
        False, "--first", help="Reset the node queue back to the start"
    ),
) -> None:
    """Rotate WARP IP, or pick the next node by priority in the chain."""
    cascade.rotate(first=first)


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print the full stack diagnostics table"
    ),
) -> None:
    """Show status of the active level (--verbose = full stack diagnostics)."""
    cfg_obj = cfg.load_config()
    level = cfg_obj.active_level
    bridge_running = bridge.running(cfg_obj)
    if level == ActiveLevel.WARP:
        _print_warp_status(as_json, verbose, bridge_running)
        return
    _print_node_status(cfg_obj, level, as_json, verbose, bridge_running)


def _print_warp_status(as_json: bool, verbose: bool, bridge_running: bool) -> None:
    ip = warp.current_ip()
    connected = ip is not None
    cfg_obj = cfg.load_config()
    if as_json:
        console.print(
            json.dumps(
                {
                    "active_level": ActiveLevel.WARP.value,
                    "warp_connected": connected,
                    "bridge_running": bridge_running,
                    "connectivity": (
                        {"ok": True, "ip": ip}
                        if connected
                        else {"ok": False, "error": "WARP not forwarding"}
                    ),
                },
                indent=2,
            )
        )
        return
    console.print(f"active level: [bold]{ActiveLevel.WARP.value}[/bold]")
    console.print(f"bridge: {_bridge_label(bridge_running, cfg_obj)}")
    if connected:
        console.print(f"connectivity: [green]OK[/green] (WARP connected, IP {ip})")
    else:
        console.print("connectivity: [red]FAIL[/red] (WARP not forwarding traffic)")
    if verbose:
        _print_tiers(cfg_obj)


def _print_node_status(
    cfg_obj: cfg.Config,
    level: ActiveLevel,
    as_json: bool,
    verbose: bool,
    bridge_running: bool,
) -> None:
    data: dict[str, object] = {"active_level": level.value}
    pid = proxy.load_pid()
    alive = pid is not None and proxy.is_running(pid)
    data["proxy_running"] = alive
    data["bridge_running"] = bridge_running
    if pid and alive:
        data["proxy_pid"] = pid

    current = node_ops.current_node(cfg_obj)
    if current is not None:
        data["node"] = {
            "id": current.id,
            "raw": current.raw,
            "protocol": current.protocol.value,
            "status": current.status.value,
            "latency_ms": current.latency_ms,
            "index": rotator.index_of(cfg_obj, current),
            "total": len(rotator.chain(cfg_obj)),
        }

    if as_json:
        if alive:
            data["connectivity"] = cascade.probe_connectivity(cfg_obj)
        console.print(json.dumps(data, indent=2, default=str))
        return

    if not warp.is_installed():
        console.print("WARP: available on host only (not found)")

    console.print(f"active level: [bold]{level.value}[/bold]")
    console.print(f"proxy: {f'running (pid {pid})' if alive else 'stopped'}")

    if current is not None:
        idx = rotator.index_of(cfg_obj, current)
        total = len(rotator.chain(cfg_obj))
        pos = f", {idx}/{total}" if idx is not None else ""
        console.print(
            f"node: {node_ops.node_label(current)} "
            f"({node_ops.node_address(current)}, {current.status.value}{pos})"
        )
    else:
        console.print("node: none")

    console.print(f"bridge: {_bridge_label(bridge_running, cfg_obj)}")

    if alive:
        result = cascade.probe_connectivity(cfg_obj)
        if result.get("ok"):
            console.print(f"connectivity: [green]OK[/green] ({result['ip']})")
        else:
            console.print(f"connectivity: [red]FAIL[/red] ({result['error']})")

    if verbose:
        _print_tiers(cfg_obj)


def _bridge_label(bridge_running: bool, cfg_obj: cfg.Config) -> str:
    if bridge_running:
        return f"[green]running[/green] ({bridge.base_url(cfg_obj)})"
    return "stopped"


def _print_tiers(cfg_obj: cfg.Config) -> None:
    """Print the full stack diagnostics table: sing-box, WARP, profiles, proxy."""
    rows: list[tuple[str, str, str]] = []
    with activity("running diagnostics..."):
        sb = shutil.which(cfg_obj.singbox_bin)
        if sb:
            rows.append(("sing-box", "[green]OK[/green]", sb))
        else:
            rows.append(
                (
                    "sing-box",
                    "[red]MISSING[/red]",
                    f"'{cfg_obj.singbox_bin}' not on PATH",
                )
            )

        if warp.is_installed():
            st = warp.status()
            status_txt = (
                "[green]connected[/green]"
                if st == warp.WarpStatus.CONNECTED
                else f"[yellow]{st.value}[/yellow]"
            )
            ip = warp.current_ip()
            rows.append(
                (
                    "WARP",
                    status_txt,
                    f"{st.value} (enabled={cfg_obj.warp_enabled})"
                    + (f", IP {ip}" if ip else ""),
                )
            )
        else:
            rows.append(
                (
                    "WARP",
                    "[yellow][/yellow]",
                    f"not installed (host only), enabled={cfg_obj.warp_enabled}",
                )
            )

        for prof in cfg_obj.profiles:
            alive = sum(1 for n in prof.nodes if n.status == NodeStatus.ALIVE)
            detail = (
                f"{prof.kind.value}, prio={prof.priority}, "
                f"{'on' if prof.enabled else 'off'}"
            )
            if prof.interval is not None:
                detail += f", refresh={prof.interval}s"
            rows.append(
                (f"profile: {prof.name}", f"{alive}/{len(prof.nodes)} alive", detail)
            )

    table = Table(title="openrot status --verbose")
    table.add_column("Tier")
    table.add_column("Status")
    table.add_column("Detail")
    for row in rows:
        table.add_row(*row)

    pid = proxy.load_pid()
    running = pid is not None and proxy.is_running(pid)
    table.add_row(
        "proxy",
        "[green]running[/green]" if running else "[yellow]stopped[/yellow]",
        f"level={cfg_obj.active_level.value}" + (f", pid {pid}" if running else ""),
    )
    console.print(table)

    if not sb:
        console.print(
            "[yellow]fix: install sing-box (brew install sing-box) and rerun[/yellow]"
        )
    if not cfg_obj.all_nodes():
        console.print(
            "[yellow]fix: add a profile — "
            "'openrot profile add NAME URL --kind relay|proxy'[/yellow]"
        )
    elif not any(n.status == NodeStatus.ALIVE for n in cfg_obj.all_nodes()):
        console.print("[yellow]fix: run 'openrot test' to health-check nodes[/yellow]")


@app.command()
def stop(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Stop the running stack: WARP or local proxy, plus any bridge daemon."""
    _ensure_confirmed(
        "Stop the current level (WARP or proxy) and the bridge daemon?", yes
    )
    cascade.stop()
    if bridge.stop_daemon():
        console.print("bridge daemon stopped")


@app.command()
def logs(
    follow: bool = typer.Option(
        True, "--follow/--no-follow", "-f/--follow", help="tail -f style output"
    ),
    tail: int = typer.Option(
        50, "--tail", "-n", min=0, help="lines shown from each file before following"
    ),
) -> None:
    """Follow the daemon, events and bridge logs, aggregated into one stream."""
    sources = {
        "daemon": cfg.LOG_PATH,
        "events": cfg.EVENT_LOG_PATH,
        "bridge": cfg.BRIDGE_LOG_PATH,
    }
    if not follow:
        for name, path in sources.items():
            _print_log_tail(name, path, tail)
        return
    signals.keyboard_on_sigterm()
    try:
        _follow_logs(sources, tail)
    except KeyboardInterrupt:
        console.print("\nstopped following logs")


def _print_log_tail(name: str, path: Path, lines: int) -> None:
    if not path.exists():
        console.print(f"[yellow]no {name} log yet: {path}[/yellow]")
        return
    with path.open(errors="replace") as f:
        content = f.readlines()
    for line in content[-lines:]:
        console.print(f"[bold]{name}[/bold] | {line.rstrip()}")


def _follow_logs(sources: dict[str, Path], tail: int) -> None:
    positions: dict[str, int] = {}
    for name, path in sources.items():
        if not path.exists():
            console.print(f"[yellow]no {name} log yet: {path}[/yellow]")
            positions[name] = -1
            continue
        with path.open("rb") as f:
            positions[name] = f.seek(0, os.SEEK_END)
        lines = _read_last_lines(path, tail)
        for line in lines:
            console.print(f"[bold]{name}[/bold] | {line}")
    while True:
        for name, path in sources.items():
            pos = positions.get(name)
            if pos is None or pos < 0 or not path.exists():
                continue
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
                positions[name] = f.tell()
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    console.print(f"[bold]{name}[/bold] | {line}")
        time.sleep(0.5)


def _read_last_lines(path: Path, count: int) -> list[str]:
    if count <= 0 or not path.exists():
        return []
    with path.open("rb") as f:
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-count:]


@app.command()
def update(
    relay: bool = typer.Option(False, "--relay", help="only update relay profiles"),
    proxy: bool = typer.Option(False, "--proxy", help="only update proxy profiles"),
    name: str | None = typer.Option(None, "--name", help="only update this profile"),
) -> None:
    """Fetch nodes for all enabled profiles (filter with --relay/--proxy/--name)."""
    cfg_obj = cfg.load_config()
    targets = [
        p
        for p in cfg_obj.profiles
        if p.enabled
        and (name is None or p.name == name)
        and (
            (not relay and not proxy)
            or (relay and p.kind == ProfileKind.RELAY)
            or (proxy and p.kind == ProfileKind.PROXY)
        )
    ]
    if not targets:
        console.print("[yellow]No matching enabled profiles[/yellow]")
        raise typer.Exit(code=1)

    for prof in targets:
        console.print(f"\n[{prof.kind.value}] refreshing '{prof.name}' ({prof.url})...")
        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        )
        bar = progress.add_task("verify", total=None)
        try:
            with progress:
                report_stage, report_live = _progress_reports(progress, bar)
                refresh.refresh_profile(
                    prof,
                    cfg_obj,
                    on_stage=report_stage,
                    on_progress=report_live,
                )
            prof.last_update = datetime.now()
            alive = sum(1 for n in prof.nodes if n.status == NodeStatus.ALIVE)
            console.print(
                f"[{prof.kind.value}] {prof.name}: {len(prof.nodes)} node(s), "
                f"{alive} alive, peak {cfg_obj.top_limit} (config limit)"
            )
        except Exception as exc:
            console.print(f"[red]'{prof.name}' refresh failed: {exc}[/red]")
    cfg.save_config(cfg_obj)


@app.command()
def run(command: Annotated[list[str], typer.Argument(help="Command to run")]) -> None:
    """Run a command through the active proxy (starts it if needed)."""
    if not command:
        console.print("[red]usage: openrot run -- <cmd>[/red]")
        raise typer.Exit(code=1)
    cfg_obj = cfg.load_config()
    if cfg_obj.active_level == ActiveLevel.NONE or not cascade.level_serving(cfg_obj):
        console.print("no active level, starting cascade...")
        cascade.start(False, False)
        cfg_obj = cfg.load_config()
    env = os.environ.copy()
    proxy_url = f"http://127.0.0.1:{cfg_obj.port}"
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["NO_PROXY"] = env.pop("NO_PROXY", "") + ",localhost,127.0.0.1,::1"
    console.print(f"running: {' '.join(command)} (level {cfg_obj.active_level.value})")
    proc = subprocess.run(command, env=env)  # noqa: S603
    raise typer.Exit(proc.returncode)


def _progress_reports(
    progress: Progress, bar: TaskID
) -> tuple[verify.Stage, verify.ProgressFn]:
    """Bind rich-Progress callbacks for one profile's pipeline run."""

    def report_stage(stage: str, kept: int, total: int) -> None:
        progress.update(
            bar,
            description=f"verify {stage}: {kept}/{total}",
            total=total,
            completed=kept,
        )

    def report_live(stage: str, done: int, total: int) -> None:
        progress.update(
            bar,
            description=f"verify {stage}: {done}/{total}",
            total=total,
            completed=done,
        )

    return report_stage, report_live


@app.command("probe")
def probe(url: str) -> None:
    """Request url through the active stack (WARP or node chain), step by step."""
    probe_core.run(url)


@app.command("config")
def config_edit() -> None:
    """Open the config file in $EDITOR (default vim)."""
    path = cfg.CONFIG_PATH
    if not path.exists():
        cfg.load_config()
    editor = os.environ.get("EDITOR") or "vim"
    console.print(f"editing {path}")
    subprocess.run([editor, str(path)])  # noqa: S603


warp_app = typer.Typer(
    help="Manage Cloudflare WARP", no_args_is_help=True, rich_markup_mode="rich"
)


@warp_app.command("install")
def warp_install() -> None:
    """Install the Cloudflare WARP client (prints instructions)."""
    if warp.is_installed():
        console.print(f"warp-cli already installed: {warp.bin_path()}")
        return
    try:
        warp.install()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@warp_app.command("on")
def warp_on() -> None:
    """Enable and connect WARP."""
    cfg_obj = cfg.load_config()
    cfg_obj.warp_enabled = True
    cfg.save_config(cfg_obj)
    if warp.connect():
        ip = warp.current_ip()
        suffix = f", IP {ip}" if ip else ""
        console.print(f"WARP [green]enabled and connected[/green]{suffix}")
    else:
        console.print("[red]WARP connect failed[/red] (is warp-cli installed?)")
        raise typer.Exit(code=1)


@warp_app.command("off")
def warp_off() -> None:
    """Disable and disconnect WARP."""
    cfg_obj = cfg.load_config()
    cfg_obj.warp_enabled = False
    cfg.save_config(cfg_obj)
    if warp.disconnect():
        console.print("WARP disconnected and disabled")
    else:
        console.print("WARP disabled (already disconnected)")


@warp_app.command("status")
def warp_status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show WARP status."""
    st = warp.status()
    cfg_obj = cfg.load_config()
    if as_json:
        console.print(
            json.dumps(
                {
                    "status": st.value,
                    "enabled": cfg_obj.warp_enabled,
                    "ip": warp.current_ip(),
                },
                indent=2,
            )
        )
        return
    console.print(f"WARP status: {st.value} (enabled={cfg_obj.warp_enabled})")
    ip = warp.current_ip()
    if ip:
        console.print(f"IP: {ip}")


app.add_typer(warp_app, name="warp")


@app.command("self-update")
def self_update_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Update the openrot binary to the latest release."""
    with activity("checking for updates..."):
        result = self_update.check_for_update()
    if result.updated or result.current == result.latest:
        console.print(f"[green]{result.message}[/green]")
        raise typer.Exit

    console.print(f"[yellow]{result.message}[/yellow]")
    _ensure_confirmed("Download and install the update?", yes)

    def _progress(stage: str, done: int, total: int) -> None:
        if stage == "done":
            console.print("[green]install complete[/green]")
        elif total:
            pct = int(done * 100 / total)
            console.print(f"  {stage}: {pct}%  ({done}/{total})")

    with activity("updating..."):
        result = self_update.perform_update(progress_fn=_progress)
    console.print(f"[green]{result.message}[/green]")
    console.print("restart your shell or run 'openrot --version' to verify")


if __name__ == "__main__":
    app()
