import os
import shutil
import subprocess
import time
from enum import StrEnum

import httpx
from rich.console import Console

WARP_BIN = "warp-cli"
WARP_PROXY_HOST = "127.0.0.1"
WARP_PROXY_PORT = 40000
IPIFY_URL = "https://api.ipify.org?format=json"

console = Console()


class WarpStatus(StrEnum):
    """Normalized state of the WARP connection."""

    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"
    NOT_INSTALLED = "not-installed"
    UNKNOWN = "unknown"


def bin_path() -> str | None:
    """Resolve the warp-cli binary (env OPENROT_WARP, else PATH lookup)."""
    name = os.environ.get("OPENROT_WARP") or WARP_BIN
    return shutil.which(name) or None


def is_installed() -> bool:
    """Return True when warp-cli is available."""
    return bin_path() is not None


def _run(*args: str, timeout: float = 10) -> str:
    b = bin_path()
    if b is None:
        return ""
    try:
        proc = subprocess.run(  # noqa: S603
            [b, *args], capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout
    except OSError, subprocess.TimeoutExpired:
        return ""


def status() -> WarpStatus:
    """Normalized warp-cli status."""
    if not is_installed():
        return WarpStatus.NOT_INSTALLED
    for line in _run("status").splitlines():
        line = line.strip()
        if line.startswith(("Status update:", "Status:")):
            raw = line.split(":", 1)[1].strip().lower()
            try:
                return WarpStatus(raw)
            except ValueError:
                return WarpStatus.UNKNOWN
    return WarpStatus.UNKNOWN


def is_connected() -> bool:
    """Return True when WARP reports a connected state."""
    return status() == WarpStatus.CONNECTED


def proxy_address() -> tuple[str, int]:
    """Return (host, port) where the WARP SOCKS5 proxy listens."""
    host = os.environ.get("OPENROT_WARP_HOST") or WARP_PROXY_HOST
    port = os.environ.get("OPENROT_WARP_PORT")
    if port and port.isdigit():
        return host, int(port)
    return host, WARP_PROXY_PORT


def connect() -> bool:
    """Bring WARP up in proxy mode and wait until it reports connected."""
    if not is_installed():
        return False
    console.print("warp: connecting (proxy mode)...")
    _, port = proxy_address()
    _run("mode", "proxy")
    _run("proxy", "port", str(port))
    _run("connect")
    console.print("waiting for WARP connection...")
    for _ in range(120):
        if is_connected():
            return True
        time.sleep(0.5)
    return False


def disconnect() -> bool:
    """Tear WARP down and confirm it is no longer connected."""
    if not is_installed():
        return False
    _run("disconnect")
    return not is_connected()


def current_ip() -> str | None:
    """Fetch the egress IP through the WARP SOCKS proxy, or None on failure."""
    host, port = proxy_address()
    try:
        with httpx.Client(proxy=f"socks5://{host}:{port}", timeout=10) as client:
            resp = client.get(IPIFY_URL)
            resp.raise_for_status()
            return resp.json().get("ip")
    except httpx.HTTPError, ValueError:
        return None


def rotate() -> bool:
    """Reconnect WARP to obtain a fresh IP."""
    if not is_installed():
        return False
    disconnect()
    return connect()


def install() -> None:
    """Raise a helpful error when warp-cli is missing so blocking works."""
    if is_installed():
        return
    raise RuntimeError(
        "warp-cli not found on PATH. Install the Cloudflare WARP client: "
        "macOS: 'brew install --cask cloudflare-warp', Linux: via Cloudflare package. "
        "Then run 'openrot warp on'."
    )
