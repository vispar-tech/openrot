"""Shared daemon lifecycle: re-run a ``start <command>`` loop detached.

Both ``cascade`` and ``bridge`` daemonize the same way — fork a background
process detached from the terminal, remember the pid, and terminate it on
demand — and only differ in the subcommand name and the pid path.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from openrot.core.proxy import is_running, load_daemon_pid, save_daemon_pid

console = Console()


def command(subcommand: str) -> list[str]:
    """Command that re-runs ``start <subcommand>`` as a detached process."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "start", subcommand]
    return [sys.executable, "-m", "openrot", "start", subcommand]


def start(*, name: str, pid_path: Path) -> None:
    """Fork the ``start <name>`` loop into a background daemon process.

    Refuses to start while the recorded pid is still alive; otherwise writes
    the fresh pid to ``pid_path``. Output goes through the events logger
    (timestamped) rather than being captured to a raw file.
    """
    existing = load_daemon_pid(pid_path)
    if existing is not None:
        if is_running(existing):
            console.print(f"[yellow]{name} daemon already running[/yellow]")
            return
        pid_path.unlink(missing_ok=True)
    proc = subprocess.Popen(  # noqa: S603
        command(name),
        start_new_session=True,
    )
    save_daemon_pid(proc.pid, path=pid_path)
    console.print(f"{name} daemon started (pid {proc.pid})")


def stop(pid_path: Path) -> bool:
    """Terminate a background daemon and remove its pid file."""
    pid = load_daemon_pid(pid_path)
    if pid is None or not is_running(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    pid_path.unlink(missing_ok=True)
    return True


def stop_and_wait(pid_path: Path) -> bool:
    """Terminate a background daemon and wait for it to exit.

    Returns True if the daemon was running and has been stopped.
    """
    pid = load_daemon_pid(pid_path)
    if pid is None or not is_running(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(50):
        if not is_running(pid):
            pid_path.unlink(missing_ok=True)
            return True
        time.sleep(0.1)
    return False


def daemon_start_background(name: str, pid_path: Path) -> None:
    """Start a daemon in a detached background process (for restart after update)."""
    proc = subprocess.Popen(  # noqa: S603
        command(name),
        start_new_session=True,
    )
    save_daemon_pid(proc.pid, path=pid_path)
