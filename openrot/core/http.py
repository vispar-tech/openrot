"""Shared httpx client factory and egress-IP helper."""

import contextlib

import httpx

from openrot.core.constants import IPIFY_URL


def make_client(
    *,
    proxy: str | None = None,
    timeout: float = 30,
    follow_redirects: bool = False,
) -> httpx.Client:
    """Build an ``httpx.Client`` with the common defaults."""
    kwargs: dict[str, object] = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def get_egress_ip(client: httpx.Client) -> str | None:
    """Fetch public IP from api.ipify.org through an existing proxy client."""
    with contextlib.suppress(Exception):
        resp = client.get(IPIFY_URL, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("ip")
    return None
