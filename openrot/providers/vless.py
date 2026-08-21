import base64
import binascii
import urllib.parse
from dataclasses import dataclass


class ParseError(Exception):
    """Raised when a string cannot be parsed as a vless:// link."""


@dataclass
class VlessNode:
    """Parsed fields of a single vless:// link."""

    address: str
    port: int
    uuid: str
    name: str = ""
    flow: str = ""
    security: str = "none"
    network: str = "tcp"
    tls: bool = False
    servername: str = ""
    ws_path: str = ""
    ws_host: str = ""
    public_key: str = ""
    short_id: str = ""


def parse_vless(raw: str) -> VlessNode:
    """Parse a vless:// link into a VlessNode, raising ParseError on bad input."""
    stripped = raw.strip()
    if not stripped.startswith("vless://"):
        raise ParseError("not a vless:// link")

    rest = stripped[len("vless://") :]
    hash_idx = rest.find("#")
    fragment = ""
    if hash_idx != -1:
        fragment = urllib.parse.unquote(rest[hash_idx + 1 :])
        rest = rest[:hash_idx]

    parts = rest.split("@")
    if len(parts) != 2:
        raise ParseError("malformed vless link")
    uuid = parts[0]
    addr_port, _, params = parts[1].partition("?")
    try:
        addr_port = addr_port.rstrip("/")
        address, port_str = addr_port.rsplit(":", 1)
        port = int(port_str)
    except ValueError as err:
        raise ParseError(f"bad address/port: {addr_port}") from err

    query = urllib.parse.parse_qs(params)
    servername = query.get("sni", [""])[0]
    flow = query.get("flow", [""])[0]
    security = query.get("security", ["none"])[0]
    network = query.get("type", ["tcp"])[0]
    ws_path = query.get("path", [""])[0]
    ws_host = query.get("host", [""])[0]
    public_key = query.get("pbk", [""])[0]
    short_id = query.get("sid", [""])[0]

    tls = security in ("tls", "reality")
    return VlessNode(
        uuid=uuid,
        address=address,
        port=port,
        name=fragment,
        flow=flow,
        security=security,
        network=network,
        tls=tls,
        servername=servername,
        ws_path=ws_path,
        ws_host=ws_host,
        public_key=public_key,
        short_id=short_id,
    )


def is_vless_line(line: str) -> bool:
    """Return True if the line starts with vless://."""
    return line.strip().startswith("vless://")


def extract_vless_records(text: str) -> list[str]:
    """Return all non-empty vless:// lines from a text body."""
    return [line.strip() for line in text.splitlines() if is_vless_line(line)]


def decode_base64_subscription(text: str) -> str:
    """Try to base64-decode a subscription body; return it unchanged on failure."""
    compact = "".join(text.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
        return decoded.decode("utf-8")
    except binascii.Error, ValueError, UnicodeDecodeError:
        return text


def extract_from_text(text: str) -> list[str]:
    """Extract vless records directly or via base64 decoding of a subscription."""
    records = extract_vless_records(text)
    if records:
        return records
    decoded = decode_base64_subscription(text)
    if decoded != text:
        return extract_vless_records(decoded)
    return []
