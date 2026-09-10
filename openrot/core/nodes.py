from pathlib import Path

from openrot import config as cfg
from openrot.config import Node, NodeProtocol, Profile
from openrot.core.http import make_client
from openrot.providers import proxy, vless


def fetch_text(source: str) -> str:
    """Read a URL or a local file; raise on anything else."""
    if source.startswith(("http://", "https://")):
        with make_client(timeout=30, follow_redirects=True) as client:
            resp = client.get(source)
            resp.raise_for_status()
            return resp.text
    path = Path(source)
    if path.exists():
        return path.read_text()
    raise ValueError(f"{source!r} is neither an http(s) URL nor a local file")


def find_profile(cfg: cfg.Config, name: str) -> Profile | None:
    """Return the profile with the given name, or None."""
    return next((p for p in cfg.profiles if p.name == name), None)


def current_node(cfg: cfg.Config) -> Node | None:
    """Return the node the cascade is currently serving, or None."""
    if cfg.current_node_id is None:
        return None
    return next((n for n in cfg.all_nodes() if n.id == cfg.current_node_id), None)


def node_label(node: Node) -> str:
    """Return a human-readable label for a node."""
    if node.protocol == NodeProtocol.VLESS:
        try:
            vnode = vless.parse_vless(node.raw)
            return vnode.name or vnode.address
        except vless.ParseError:
            return node.id
    return node.raw


def node_address(node: Node) -> str:
    """Return host:port for a node, or '?' when unparseable."""
    if node.protocol in (NodeProtocol.HTTP, NodeProtocol.SOCKS5):
        host, port = proxy.host_port(node.raw)
        if host is None or port is None:
            return "?"
        return f"{host}:{port}"
    try:
        vnode = vless.parse_vless(node.raw)
        return f"{vnode.address}:{vnode.port}"
    except vless.ParseError:
        return "?"
