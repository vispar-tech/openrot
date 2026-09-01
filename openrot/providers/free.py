import contextlib
import time
from urllib.parse import urlparse

import httpx

from openrot.config import Config, Node

HEALTH_URL = "https://www.gstatic.com/generate_204"
CHECK_TIMEOUT = 4


def parse_proxy(raw: str) -> tuple[str, str, int] | None:
    """Parse a 'proto://host:port' line. Returns (protocol, host, port)."""
    stripped = raw.strip()
    if not stripped or not stripped.startswith(("http://", "socks5://")):
        return None
    parsed = urlparse(stripped)
    if not parsed.hostname or not parsed.port:
        return None
    try:
        return parsed.scheme, parsed.hostname, parsed.port
    except ValueError:
        return None


def check_proxy(
    protocol: str, host: str, port: int, timeout: float = CHECK_TIMEOUT
) -> tuple[bool, float | None]:
    """Health-check a forward proxy, returning (alive, latency_ms)."""
    start = time.monotonic()
    try:
        with httpx.Client(
            proxy=f"{protocol}://{host}:{port}", timeout=timeout, follow_redirects=True
        ) as client:
            resp = client.get(HEALTH_URL)
            alive = resp.status_code == 204
    except httpx.HTTPError:
        return False, None
    latency = round((time.monotonic() - start) * 1000, 1) if alive else None
    return alive, latency


def probe_targets(
    protocol: str,
    host: str,
    port: int,
    timeout: float = CHECK_TIMEOUT,
    url: str | None = None,
) -> tuple[list[float], str | None]:
    """Probe ``url`` (default HEALTH_URL) through the forward proxy.

    Returns (latencies, egress_ip) where latencies is [latency] on a 2xx
    response or [] when the proxy cannot route traffic at all.
    """
    target = url or HEALTH_URL
    start = time.monotonic()
    try:
        with httpx.Client(
            proxy=f"{protocol}://{host}:{port}",
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = client.get(target)
    except httpx.HTTPError:
        return [], None
    if 200 <= resp.status_code < 300:
        latency = round((time.monotonic() - start) * 1000, 1)
        egress_ip = _get_egress_ip(client)
        return [latency], egress_ip
    return [], None


def _get_egress_ip(client: httpx.Client) -> str | None:
    """Fetch public IP from api.ipify.org through an existing proxy client."""
    with contextlib.suppress(Exception):
        resp = client.get("https://api.ipify.org?format=json", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("ip")
    return None


def fetch_candidates(text: str) -> list[tuple[str, str, int]]:
    """Parse unique (protocol, host, port) tuples from a proxy list body."""
    candidates: dict[tuple[str, str, int], None] = {}
    for line in text.splitlines():
        parsed = parse_proxy(line)
        if parsed:
            candidates.setdefault(parsed, None)
    return list(candidates)


def host_port(raw: str) -> tuple[str | None, int | None]:
    """Return (host, port) parsed from a proxy URI."""
    parsed = urlparse(raw)
    return parsed.hostname, parsed.port


def check_node(node: Node, cfg: Config) -> tuple[bool, float | None]:
    """Health-check a proxy node (protocol http|socks5) using cfg.health_timeout."""
    host, port = host_port(node.raw)
    if host is None or port is None:
        return False, None
    return check_proxy(node.protocol.value, host, port, cfg.health_timeout)
