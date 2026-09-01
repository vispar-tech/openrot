import contextlib
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from openrot import config as cfg
from openrot import log
from openrot.providers.vless import VlessNode

HEALTH_URL = "https://www.gstatic.com/generate_204"


class RealityTLSOptions(BaseModel):
    """TLS reality (public key + short id) options."""

    enabled: bool = True
    public_key: str = ""
    short_id: str = ""


class UTLSOptions(BaseModel):
    """uTLS client fingerprint options."""

    enabled: bool = True
    fingerprint: str = "chrome"


class TLSOptions(BaseModel):
    """TLS options for a VLESS outbound."""

    enabled: bool = True
    server_name: str = ""
    reality: RealityTLSOptions | None = None
    utls: UTLSOptions | None = None


class TransportOptions(BaseModel):
    """WebSocket transport options."""

    type: str = "ws"
    path: str = "/"
    headers: dict[str, str] = {}


class VLESSOutbound(BaseModel):
    """sing-box outbound pointing at a single VLESS server."""

    type: str = "vless"
    tag: str = "proxy"
    server: str
    server_port: int
    uuid: str
    flow: str = ""
    tls: TLSOptions | None = None
    transport: TransportOptions | None = None


class MixedInbound(BaseModel):
    """Local mixed (http+socks5) inbound."""

    type: str = "mixed"
    tag: str = "mixed-in"
    listen: str = "127.0.0.1"
    listen_port: int


class LogOptions(BaseModel):
    """sing-box logging options."""

    level: str = "warn"


class SingBoxConfig(BaseModel):
    """Minimal sing-box config for a local mixed inbound + vless outbound."""

    log: LogOptions = LogOptions()
    inbounds: list[MixedInbound]
    outbounds: list[VLESSOutbound]


def _strip_none(value: Any) -> Any:
    """Recursively drop keys whose value is None (and prune empties in dicts)."""
    if isinstance(value, dict):
        return {
            k: _strip_none(v)
            for k, v in value.items()
            if v is not None and (_strip_none(v) != {} if isinstance(v, dict) else True)
        }
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


def generate_singbox_config(node: VlessNode, port: int) -> dict[str, object]:
    """Build the sing-box JSON for a VLESS outbound listening on `port`."""
    tls: TLSOptions | None = None
    if node.security == "reality":
        tls = TLSOptions(
            server_name=node.servername or node.address,
            reality=RealityTLSOptions(
                public_key=node.public_key, short_id=node.short_id
            ),
            utls=UTLSOptions(),
        )
    elif node.tls:
        tls = TLSOptions(server_name=node.servername or node.address)

    transport: TransportOptions | None = None
    if node.network == "ws":
        transport = TransportOptions(
            path=node.ws_path or "/",
            headers={"Host": node.ws_host or node.address},
        )

    outbound = VLESSOutbound(
        server=node.address,
        server_port=node.port,
        uuid=node.uuid,
        flow=node.flow,
        tls=tls,
        transport=transport,
    )

    cfg_model = SingBoxConfig(
        inbounds=[MixedInbound(listen=cfg.listen_address(), listen_port=port)],
        outbounds=[outbound],
    )
    return _strip_none(cfg_model.model_dump(mode="json"))


def generate_free_config(
    protocol: str, host: str, port: int, listen_port: int
) -> dict[str, object]:
    """sing-box JSON with an 'http' or 'socks' outbound to a forward proxy."""
    out_type = {"http": "http", "socks5": "socks"}.get(protocol, "http")
    return _strip_none(
        {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": cfg.listen_address(),
                    "listen_port": listen_port,
                }
            ],
            "outbounds": [
                {"type": out_type, "tag": "proxy", "server": host, "server_port": port}
            ],
        }
    )


def write_config(config_data: dict[str, object], prefix: str = "openrot-") -> Path:
    """Write a sing-box config to a temp file and return its path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=prefix, delete=False
    ) as f:
        json.dump(config_data, f)
    return Path(f.name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float, step: float = 0.05) -> bool:
    """Poll until `host:port` accepts connections or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(step)
    return False


EgressResult = tuple[bool, float | None, str | None]


def probe_vless(
    node: VlessNode, singbox_bin: str, timeout: float, url: str | None = None
) -> EgressResult:
    """Probe ``url`` (default HEALTH_URL) through one throwaway sing-box.

    Waits until the local listener is up (instead of a fixed sleep); when
    sing-box exits during the wait, its stderr is logged.

    Returns (alive, latency_ms, egress_ip): ``alive`` is True when the node
    routed the request and got a 2xx response; ``latency_ms`` is the request
    time in ms; ``egress_ip`` is the public IP seen by the target.
    """
    target = url or HEALTH_URL
    port = _free_port()
    cfg_path = write_config(generate_singbox_config(node, port), prefix="openrot-heal-")
    with tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(  # noqa: S603
            [singbox_bin, "run", "-c", str(cfg_path)],
            stdout=subprocess.DEVNULL,
            stderr=err_f,
        )
        try:
            ready = _wait_for_port("127.0.0.1", port, max(timeout, 2.0))
            if not ready:
                if proc.poll() is not None:
                    err_f.seek(0)
                    stderr_text = err_f.read().decode("utf-8", errors="replace")
                    log.get_logger().warning(
                        "sing-box exited during probe (%s): %s",
                        proc.returncode,
                        stderr_text.strip(),
                    )
                return False, None, None
            with httpx.Client(
                proxy=f"http://127.0.0.1:{port}", timeout=timeout
            ) as client:
                start = time.monotonic()
                try:
                    resp = client.get(target)
                except httpx.HTTPError:
                    return False, None, None
                if 200 <= resp.status_code < 300:
                    latency = round((time.monotonic() - start) * 1000, 1)
                    egress_ip = _get_egress_ip(client)
                    return True, latency, egress_ip
                return False, None, None
        finally:
            proc.terminate()
            cfg_path.unlink(missing_ok=True)


def _get_egress_ip(client: httpx.Client) -> str | None:
    """Fetch public IP from api.ipify.org through an existing proxy client."""
    with contextlib.suppress(Exception):
        resp = client.get("https://api.ipify.org?format=json", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("ip")
    return None
