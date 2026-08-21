import hashlib
from datetime import datetime

from pydantic import BaseModel

from .enums import NodeProtocol, NodeStatus


def node_id(raw: str) -> str:
    """Return a stable short id derived from the raw node string."""
    return "node-" + hashlib.sha256(raw.encode()).hexdigest()[:8]


class Node(BaseModel):
    """A single proxy endpoint (vless, http or socks5)."""

    id: str
    raw: str
    protocol: NodeProtocol = NodeProtocol.VLESS
    priority: int = 0
    status: NodeStatus = NodeStatus.UNKNOWN
    latency_ms: float | None = None
    fails: int = 0
    last_check: datetime | None = None
