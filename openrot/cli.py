import json
import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from openrot import __version__, log, opencode, self_update, signals
from openrot import config as cfg
from openrot.config import ActiveLevel
from openrot.core import bridge, cascade
from openrot.providers import warp

app = typer.Typer(
    help=(
        "Local WARP bridge: routes opencode traffic through a "
        "Cloudflare WARP connection"
    ),
    no_args_is_help=True,
    invoke_without_command=True,
    rich_markup_mode="rich",
)
console = Console()
events = log.get_logger()
warp_app = typer.Typer(
    help="Manage Cloudflare WARP", no_args_is_help=True, rich_markup_mode="rich"
)


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


@app.command()
def start(
    mode: str = typer.Argument(help="bridge (429-rotation server)"),
    as_daemon: bool = typer.Option(
        False, "--daemon", help="run in the background instead of the foreground"
    ),
) -> None:
    """Start the opencode bridge."""
    if mode != "bridge":
        console.print(f"[red]unknown mode '{mode}'[/red] (bridge)")
        raise typer.Exit(1)
    if as_daemon:
        bridge.daemonize()
    else:
        bridge.serve()


@app.command()
def rotate() -> None:
    """Rotate the WARP IP."""
    cascade.rotate()


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print the full stack diagnostics table"
    ),
) -> None:
    """Show WARP and bridge status (--verbose = full stack diagnostics)."""
    cfg_obj = cfg.load_config()
    bridge_running = bridge.running(cfg_obj)
    _print_warp_status(as_json, verbose, bridge_running)


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
        _print_tiers()


def _bridge_label(bridge_running: bool, cfg_obj: cfg.Config) -> str:
    if bridge_running:
        return f"[green]running[/green] ({bridge.base_url(cfg_obj)})"
    return "stopped"


def _print_tiers() -> None:
    """Print the diagnostics table: WARP tier."""
    rows: list[tuple[str, str, str]] = []
    with activity("running diagnostics..."):
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
                    st.value + (f", IP {ip}" if ip else ""),
                )
            )
        else:
            rows.append(
                (
                    "WARP",
                    "[yellow][/yellow]",
                    "not installed (host only)",
                )
            )

    table = Table(title="openrot status --verbose")
    table.add_column("Tier")
    table.add_column("Status")
    table.add_column("Detail")
    for row in rows:
        table.add_row(*row)
    console.print(table)


@app.command()
def stop(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Stop the running stack: WARP plus any bridge daemon."""
    _ensure_confirmed("Stop WARP and the bridge daemon?", yes)
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
    """Follow the openrot log file."""
    path = cfg.LOG_PATH
    if not path.exists():
        console.print(f"[yellow]no log yet: {path}[/yellow]")
        return
    if not follow:
        with path.open(errors="replace") as f:
            content = f.readlines()
        for line in content[-tail:]:
            console.print(line.rstrip())
        return
    signals.keyboard_on_sigterm()
    try:
        _follow_log(path, tail)
    except KeyboardInterrupt:
        console.print("\nstopped following logs")


def _follow_log(path: Path, tail: int) -> None:
    with path.open("rb") as f:
        position = f.seek(0, os.SEEK_END)
    lines = _read_last_lines(path, tail)
    for line in lines:
        console.print(line)
    while True:
        with path.open("rb") as f:
            f.seek(position)
            chunk = f.read()
            position = f.tell()
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines():
                console.print(line)
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
def run(command: Annotated[list[str], typer.Argument(help="Command to run")]) -> None:
    """Run a command through the active proxy (starts it if needed)."""
    if not command:
        console.print("[red]usage: openrot run -- <cmd>[/red]")
        raise typer.Exit(code=1)
    cfg_obj = cfg.load_config()
    if cfg_obj.active_level == ActiveLevel.NONE or not cascade.level_serving(cfg_obj):
        console.print("no active level, connecting warp...")
        if not cascade.start():
            console.print("[yellow]warp unavailable; the command may fail[/yellow]")
        cfg_obj = cfg.load_config()
    env = os.environ.copy()
    w_host, w_port = warp.proxy_address()
    proxy_url = f"socks5://{w_host}:{w_port}"
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["NO_PROXY"] = env.pop("NO_PROXY", "") + ",localhost,127.0.0.1,::1"
    console.print(f"running: {' '.join(command)} (level {cfg_obj.active_level.value})")
    proc = subprocess.run(command, env=env)  # noqa: S603
    raise typer.Exit(proc.returncode)


@app.command("config")
def config_edit() -> None:
    """Open the config file in $EDITOR (default vim)."""
    path = cfg.CONFIG_PATH
    if not path.exists():
        cfg.load_config()
    editor = os.environ.get("EDITOR") or "vim"
    console.print(f"editing {path}")
    subprocess.run([editor, str(path)])  # noqa: S603


@app.command("provider")
def provider_cmd(
    action: str = typer.Option("auto", help="Action: on, off, or auto (toggle)"),
    config: str = typer.Option(
        str(opencode.CONFIG_PATH), "--config", help="Path to opencode.jsonc"
    ),
) -> None:
    """Toggle opencode provider in opencode.jsonc."""
    if action not in ("on", "off", "auto"):
        console.print("[red]Invalid action: use 'on', 'off', or 'auto'[/red]")
        raise typer.Exit(code=1)
    new_state = opencode.toggle(action=action, path=Path(config))
    status = "[green]enabled[/green]" if new_state else "[red]disabled[/red]"
    console.print(f"Provider {status}")
    events.info("[provider] %s", "enabled" if new_state else "disabled")


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
    """Connect WARP."""
    if warp.connect():
        ip = warp.current_ip()
        suffix = f", IP {ip}" if ip else ""
        console.print(f"WARP [green]connected[/green]{suffix}")
        events.info("[warp] on: connected (ip=%s)", ip or "-")
    else:
        console.print("[red]WARP connect failed[/red] (is warp-cli installed?)")
        events.warning("[warp] on: connect failed")
        raise typer.Exit(code=1)


@warp_app.command("off")
def warp_off() -> None:
    """Disconnect WARP."""
    if warp.disconnect():
        console.print("WARP disconnected")
        events.info("[warp] off: disconnected")
    else:
        console.print("WARP already disconnected")
        events.info("[warp] off: already disconnected")


@warp_app.command("status")
def warp_status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show WARP status."""
    st = warp.status()
    if as_json:
        console.print(
            json.dumps(
                {
                    "status": st.value,
                    "ip": warp.current_ip(),
                },
                indent=2,
            )
        )
        return
    console.print(f"WARP status: {st.value}")
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
    events.info("[self-update] updating %s → %s", result.current, result.latest)

    def _progress(stage: str, done: int, total: int) -> None:
        if stage == "done":
            console.print("[green]install complete[/green]")
            events.info(
                "[self-update] installed %s → %s", result.current, result.latest
            )
        elif total:
            pct = int(done * 100 / total)
            console.print(f"  {stage}: {pct}%  ({done}/{total})")

    with activity("updating..."):
        result = self_update.perform_update(progress_fn=_progress)
    console.print(f"[green]{result.message}[/green]")
    console.print("restart your shell or run 'openrot --version' to verify")


if __name__ == "__main__":
    app()
