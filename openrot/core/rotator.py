from openrot.config import Config, Node, NodeStatus, Strategy
from openrot.core import health


def chain(cfg: Config) -> list[Node]:
    """All nodes of enabled profiles, ordered by (profile.priority, node.priority)."""
    ordered: list[Node] = []
    for profile in sorted(
        (p for p in cfg.profiles if p.enabled), key=lambda p: p.priority
    ):
        ordered.extend(sorted(profile.nodes, key=lambda n: n.priority))
    return ordered


def _alive(cfg: Config) -> list[Node]:
    return [n for n in chain(cfg) if n.status == NodeStatus.ALIVE]


def first_node(cfg: Config) -> Node | None:
    """First alive node in chain order (start of the queue)."""
    alive = _alive(cfg)
    return alive[0] if alive else None


def next_node(cfg: Config, current_id: str | None = None) -> Node | None:
    """Next alive node after `current_id` in chain order, wrapping to the start."""
    alive = _alive(cfg)
    if not alive:
        return None
    if current_id is None:
        return alive[0]
    current_index = next((i for i, n in enumerate(alive) if n.id == current_id), None)
    if current_index is None:
        return alive[0]
    return alive[(current_index + 1) % len(alive)]


def pick(cfg: Config, exclude_id: str | None = None) -> Node | None:
    """Pick the next node by the configured strategy, skipping `exclude_id`."""
    strategy = cfg.strategy or Strategy.FALLBACK
    chain_nodes = chain(cfg)
    if strategy == Strategy.URLTEST:
        return health.select_node(chain_nodes, Strategy.URLTEST, exclude_id)
    return health.select_node(chain_nodes, Strategy.FALLBACK, exclude_id)


def index_of(cfg: Config, node: Node) -> int | None:
    """1-based position of `node` in the ordered chain, or None when absent."""
    try:
        return chain(cfg).index(node) + 1
    except ValueError:
        return None
