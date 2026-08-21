from enum import StrEnum


class ProfileKind(StrEnum):
    """Type of source profile: relay (vless list) or proxy (http/socks5 list)."""

    RELAY = "relay"
    PROXY = "proxy"


class NodeProtocol(StrEnum):
    """Protocol of a single node."""

    VLESS = "vless"
    HTTP = "http"
    SOCKS5 = "socks5"
    # follow-up: HYSTERIA2 = "hysteria2", VMESS = "vmess", SS = "ss"


class NodeStatus(StrEnum):
    """Health state of a node."""

    UNKNOWN = "unknown"
    ALIVE = "alive"
    DEAD = "dead"


class ActiveLevel(StrEnum):
    """What the cascade is currently serving on the local port."""

    NONE = "none"
    WARP = "warp"
    NODE = "node"


class Strategy(StrEnum):
    """Selection strategy for the next node."""

    FALLBACK = "fallback"
    URLTEST = "urltest"
