"""Shared httpx client factory, port probe, and egress-IP helper."""

import contextlib
import socket

import httpx

from openrot.core.constants import IPIFY_URL


def make_client(
    *,
    proxy: str | None = None,
    timeout: float = 30,
    follow_redirects: bool = False,
) -> httpx.Client:
    """Build an ``httpx.Client`` with the common defaults."""
    if proxy:
        return httpx.Client(
            proxy=proxy, timeout=timeout, follow_redirects=follow_redirects
        )
    return httpx.Client(timeout=timeout, follow_redirects=follow_redirects)


def get_egress_ip(client: httpx.Client) -> str | None:
    """Fetch public IP from api.ipify.org through an existing proxy client."""
    with contextlib.suppress(Exception):
        resp = client.get(IPIFY_URL, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("ip")
    return None


def port_in_use(host: str, port: int) -> bool:
    """Return True when something is already listening on `host:port`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0
