from datetime import datetime

from openrot import log
from openrot.config import Config, Node, NodeProtocol, NodeStatus, Strategy
from openrot.core import verify
from openrot.core.singbox import probe_vless
from openrot.providers import free
from openrot.providers.vless import ParseError, parse_vless


def check_node(node: Node, cfg: Config) -> tuple[bool, float | None]:
    """Probe one node by protocol. Returns (alive, latency_ms)."""
    if node.protocol in (NodeProtocol.HTTP, NodeProtocol.SOCKS5):
        return free.check_node(node, cfg)
    try:
        venode = parse_vless(node.raw)
    except ParseError:
        return False, None
    alive, latency, _egress_ip = probe_vless(
        venode, cfg.singbox_bin, cfg.health_timeout
    )
    return alive, latency


def test_all(
    cfg: Config,
    on_stage: verify.Stage | None = None,
    on_progress: verify.ProgressFn | None = None,
) -> int:
    """Re-verify every node through the pipeline. Returns the number alive.

    Dead nodes accumulate `fails` and are only marked DEAD once they cross
    `cfg.fail_threshold`.
    """
    alive_count = 0
    vless_survivors = []
    proxy_survivors = []
    vless_nodes = [n for n in cfg.all_nodes() if n.protocol == NodeProtocol.VLESS]
    proxy_nodes = [
        n
        for n in cfg.all_nodes()
        if n.protocol in (NodeProtocol.HTTP, NodeProtocol.SOCKS5)
    ]

    if vless_nodes:
        vless_survivors = verify.verify_vless_pool(
            [n.raw for n in vless_nodes],
            cfg.singbox_bin,
            cfg.health_timeout,
            limit=None,
            urltest_url=cfg.urltest_url,
            on_stage=on_stage,
            on_progress=on_progress,
            max_workers=cfg.max_workers,
            deduplicate_by_ip=cfg.deduplicate_by_ip,
        )
        okay = {
            raw: (latency, egress_ip)
            for (raw, _vnode), latency, egress_ip in vless_survivors
        }
        for node in vless_nodes:
            entry = okay.get(node.raw)
            alive_count += _apply_result(
                node,
                entry[0] if entry else None,
                cfg,
                entry[1] if entry else None,
            )

    if proxy_nodes:
        candidates = free.fetch_candidates("\n".join(n.raw for n in proxy_nodes))
        proxy_survivors = verify.verify_proxy_pool(
            candidates,
            cfg.health_timeout,
            limit=None,
            urltest_url=cfg.urltest_url,
            on_stage=on_stage,
            on_progress=on_progress,
            max_workers=cfg.max_workers,
            deduplicate_by_ip=cfg.deduplicate_by_ip,
        )
        proxy_ok = {
            (proto, host, port): (latency, egress_ip)
            for (proto, host, port), latency, egress_ip in proxy_survivors
        }
        for node in proxy_nodes:
            parsed = free.parse_proxy(node.raw)
            entry = None if parsed is None else proxy_ok.get(parsed)
            alive_count += _apply_result(
                node,
                entry[0] if entry else None,
                cfg,
                entry[1] if entry else None,
            )

    total_input = len(vless_nodes) + len(proxy_nodes)
    total_output = len(vless_survivors) + len(proxy_survivors)
    dedup_dropped = total_input - total_output
    if dedup_dropped > 0:
        log.get_logger().info("dedup: removed %d duplicate-IP nodes", dedup_dropped)

    return alive_count


def _apply_result(
    node: Node,
    latency: float | None,
    cfg: Config,
    egress_ip: str | None = None,
) -> int:
    """Record one health result for a node; return 1 if it survived.

    Survivors are marked ALIVE with their latency; everyone else accumulates a
    `fails` counter and is marked DEAD once it crosses `cfg.fail_threshold`.
    """
    node.last_check = datetime.now()
    if latency is not None:
        node.status = NodeStatus.ALIVE
        node.latency_ms = latency
        node.fails = 0
        if egress_ip is not None:
            node.egress_ip = egress_ip
        return 1
    node.fails += 1
    if node.fails >= cfg.fail_threshold:
        node.status = NodeStatus.DEAD
    return 0


def select_node(
    nodes: list[Node], strategy: Strategy, exclude_id: str | None = None
) -> Node | None:
    """Pick a node by strategy among alive ones (nodes expected in priority order)."""
    alive = [
        n
        for n in nodes
        if n.status == NodeStatus.ALIVE and (exclude_id is None or n.id != exclude_id)
    ]
    if not alive:
        return None
    if strategy == Strategy.URLTEST:
        valid = [(n.latency_ms, n) for n in alive if n.latency_ms is not None]
        if valid:
            return min(valid, key=lambda item: item[0])[1]
        return None
    return alive[0]
