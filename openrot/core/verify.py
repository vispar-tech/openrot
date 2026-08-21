"""Aggregation-time verification pipeline for node pools.

A node is ALIVE only if it survives every stage:

    TCP reachability -> (TLS handshake) -> sing-box config check
    -> HTTP-over-proxy probe of the configured urltest URL

The probe must reach the urltest URL (any 2xx) for the node to survive;
its latency is the measured request time in ms. Survivors are ranked by
latency and the top ``limit`` are published. Progress is reported per
finished node through ``on_progress``.
"""

import contextlib
import socket
import ssl
import statistics
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import TypeVar

from openrot.config import TOP_LIMIT, Node, NodeProtocol, NodeStatus, node_id
from openrot.core.singbox import (
    generate_singbox_config,
    probe_vless,
    write_config,
)
from openrot.providers import free, vless

MAX_WORKERS = 50

CHECK_LISTEN_PORT = 1

Stage = Callable[[str, int, int], None]
ProgressFn = Callable[[str, int, int], None]
RelayCandidate = tuple[str, vless.VlessNode]
ProxyKey = tuple[str, str, int]  # (protocol, host, port)
T = TypeVar("T")


def median(values: list[float]) -> float:
    """Median of the observed latencies (0.0 for an empty sample)."""
    return statistics.median(values) if values else 0.0


def tcp_reachable(host: str, port: int, timeout: float) -> bool:
    """Open a TCP connection to host:port, dropping dead hosts and blocked ports."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tls_handshake(host: str, port: int, servername: str, timeout: float) -> bool:
    """Complete a TLS handshake without verifying the certificate.

    Fails on expired certs, SNI mismatches and unreachable servers. The outer
    TLS layer of Reality nodes is a real TLS handshake too, so this is a valid
    gate for them as well.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=servername or host),
        ):
            pass
        return True
    except OSError:
        return False


def singbox_check(node: vless.VlessNode, singbox_bin: str) -> bool:
    """Run ``sing-box check`` on a generated config for the node.

    Drops nodes whose config sing-box rejects (bad UUIDs, unsupported flow,
    broken Reality short-ids) before they consume a probe slot.
    """
    cfg_path = write_config(generate_singbox_config(node, CHECK_LISTEN_PORT))
    try:
        proc = subprocess.run(  # noqa: S603
            [singbox_bin, "check", "-c", str(cfg_path)],
            capture_output=True,
        )
        return proc.returncode == 0
    except OSError:
        return False
    finally:
        cfg_path.unlink(missing_ok=True)


def _run_stage[T](
    items: list[T],
    fn: Callable[[T], bool],
    on_stage: Stage | None,
    on_progress: ProgressFn | None,
    stage: str,
    *,
    max_workers: int = MAX_WORKERS,
) -> list[T]:
    """Filter `items` in a worker pool, reporting realtime + final stage counts.

    ``on_progress(stage, done, total)`` fires after every finished node;
    ``on_stage(stage, kept, total)`` fires once with the final tally.
    """
    total = len(items)
    if total == 0:
        if on_stage is not None:
            on_stage(stage, 0, 0)
        return []
    survivors: list[T] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futures):
            done += 1
            try:
                is_ok = fut.result()
            except Exception:
                is_ok = False
            if is_ok:
                survivors.append(futures[fut])
            if on_progress is not None:
                on_progress(stage, done, total)
    if on_stage is not None:
        on_stage(stage, len(survivors), total)
    return survivors


def _probe_latencies(
    node: vless.VlessNode, singbox_bin: str, timeout: float, url: str | None
) -> list[float]:
    """Probe one node; return [latency_ms] on a 2xx, [] otherwise."""
    alive, latency = probe_vless(node, singbox_bin, timeout, url)
    return [latency] if alive and latency is not None else []


def _run_probe[T](
    items: list[T],
    fn: Callable[[T], list[float]],
    on_stage: Stage | None,
    on_progress: ProgressFn | None,
    max_workers: int = MAX_WORKERS,
) -> list[tuple[T, float]]:
    """Probe `items` in a worker pool; keep survivors that reached the urltest URL.

    A survivor needs the configured urltest URL reachable (non-empty latency
    sample). ``on_progress`` fires after every finished node; ranks by median
    latency.
    """
    total = len(items)
    if total == 0:
        if on_stage is not None:
            on_stage("probe", 0, 0)
        return []
    needed = 1
    results: list[tuple[T, float]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futures):
            done += 1
            try:
                latencies = fut.result()
            except Exception:
                latencies = []
            if len(latencies) >= needed:
                results.append((futures[fut], median(latencies)))
            if on_progress is not None:
                on_progress("probe", done, total)
    if on_stage is not None:
        on_stage("probe", len(results), total)
    results.sort(key=lambda entry: entry[1])
    return results


def _dedupe(records: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for rec in records:
        if rec in seen:
            continue
        seen.add(rec)
        result.append(rec)
    return result


def _parse_relay_candidates(records: list[str]) -> list[RelayCandidate]:
    parsed: list[RelayCandidate] = []
    for rec in records:
        with contextlib.suppress(vless.ParseError):
            parsed.append((rec, vless.parse_vless(rec)))
    return parsed


def verify_vless_pool(
    records: list[str],
    singbox_bin: str,
    timeout: float,
    *,
    limit: int | None = TOP_LIMIT,
    urltest_url: str | None = None,
    on_stage: Stage | None = None,
    on_progress: ProgressFn | None = None,
    max_workers: int = MAX_WORKERS,
) -> list[tuple[RelayCandidate, float]]:
    """Run the full pipeline over vless records; return (raw, parsed, latency) pairs.

    Latency of a survivor is the request time to ``urltest_url`` (default
    HEALTH_URL).
    """
    candidates = _parse_relay_candidates(_dedupe(records))
    if on_stage is not None:
        on_stage("parse", len(candidates), len(records))

    tcp_alive = _run_stage(
        candidates,
        lambda item: tcp_reachable(item[1].address, item[1].port, timeout),
        on_stage,
        on_progress,
        "tcp",
        max_workers=max_workers,
    )
    tls_alive = _run_stage(
        tcp_alive,
        lambda item: (
            (not item[1].tls)
            or tls_handshake(item[1].address, item[1].port, item[1].servername, timeout)
        ),
        on_stage,
        on_progress,
        "tls",
        max_workers=max_workers,
    )
    cfg_alive = _run_stage(
        tls_alive,
        lambda item: singbox_check(item[1], singbox_bin),
        on_stage,
        on_progress,
        "config",
        max_workers=max_workers,
    )

    results = _run_probe(
        cfg_alive,
        lambda item: _probe_latencies(item[1], singbox_bin, timeout, urltest_url),
        on_stage,
        on_progress,
        max_workers,
    )
    return results[:limit] if limit is not None else results


def verify_proxy_pool(
    candidates: list[ProxyKey],
    timeout: float,
    *,
    limit: int | None = TOP_LIMIT,
    urltest_url: str | None = None,
    on_stage: Stage | None = None,
    on_progress: ProgressFn | None = None,
    max_workers: int = MAX_WORKERS,
) -> list[tuple[ProxyKey, float]]:
    """TCP-check proxy candidates and probe the survivors.

    Returns (key, latency) pairs, best latency first. Latency is the request
    time to ``urltest_url`` (default HEALTH_URL).
    """
    if on_stage is not None:
        on_stage("parse", len(candidates), len(candidates))
    tcp_alive = _run_stage(
        candidates,
        lambda key: tcp_reachable(key[1], key[2], timeout),
        on_stage,
        on_progress,
        "tcp",
        max_workers=max_workers,
    )
    results = _run_probe(
        tcp_alive,
        lambda key: free.probe_targets(key[0], key[1], key[2], timeout, urltest_url),
        on_stage,
        on_progress,
        max_workers,
    )
    return results[:limit] if limit is not None else results


def nodes_from_vless_survivors(
    survivors: list[tuple[RelayCandidate, float]],
) -> list[Node]:
    """Turn verified (raw, vnode, latency) pairs into published ALIVE Nodes."""
    now = datetime.now()
    return [
        Node(
            id=node_id(raw),
            raw=raw,
            protocol=NodeProtocol.VLESS,
            status=NodeStatus.ALIVE,
            latency_ms=latency,
            priority=i,
            last_check=now,
        )
        for i, ((raw, _vnode), latency) in enumerate(survivors)
    ]


def nodes_from_proxy_survivors(
    survivors: list[tuple[ProxyKey, float]],
) -> list[Node]:
    """Turn verified (protocol, host, port, latency) pairs into ALIVE Nodes."""
    now = datetime.now()
    result = []
    for i, ((protocol, host, port), latency) in enumerate(survivors):
        raw = f"{protocol}://{host}:{port}"
        result.append(
            Node(
                id=node_id(raw),
                raw=raw,
                protocol=NodeProtocol(protocol),
                status=NodeStatus.ALIVE,
                latency_ms=latency,
                priority=i,
                last_check=now,
            )
        )
    return result
