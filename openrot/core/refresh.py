import random
import time
from datetime import datetime
from functools import partial

from rich.console import Console

from openrot import config as cfg
from openrot.config import Config, Profile, ProfileKind
from openrot.core import nodes, verify
from openrot.core.verify import Stage
from openrot.providers import free, vless

console = Console()


def refresh_profile(
    prof: Profile,
    cfg_obj: Config,
    *,
    on_stage: Stage | None = None,
    on_progress: verify.ProgressFn | None = None,
) -> None:
    """Fetch and replace a profile's nodes from its URL according to its kind."""
    prof.nodes = fetch_profile_nodes(
        prof, cfg_obj, on_stage=on_stage, on_progress=on_progress
    )


def fetch_profile_nodes(
    prof: Profile,
    cfg_obj: Config,
    *,
    on_stage: Stage | None = None,
    on_progress: verify.ProgressFn | None = None,
) -> list[cfg.Node]:
    """Fetch a profile's nodes from its URL and verify them (no mutation).

    Relay pools run the full TCP -> TLS -> sing-box check -> HTTP probe
    pipeline; free proxy pools only TCP -> HTTP probe. The top vertices by
    urltest latency are published (capped at ``cfg.top_limit``).
    """
    text = nodes.fetch_text(prof.url)
    if prof.kind == ProfileKind.PROXY:
        candidates = free.fetch_candidates(text)
        proxy_survivors = verify.verify_proxy_pool(
            candidates,
            cfg_obj.health_timeout,
            urltest_url=cfg_obj.urltest_url,
            limit=cfg_obj.top_limit,
            on_stage=on_stage,
            on_progress=on_progress,
            max_workers=cfg_obj.max_workers,
            deduplicate_by_ip=cfg_obj.deduplicate_by_ip,
        )
        return verify.nodes_from_proxy_survivors(proxy_survivors)
    records = vless.extract_from_text(text)
    relay_survivors = verify.verify_vless_pool(
        records,
        cfg_obj.singbox_bin,
        cfg_obj.health_timeout,
        urltest_url=cfg_obj.urltest_url,
        limit=cfg_obj.top_limit,
        on_stage=on_stage,
        on_progress=on_progress,
        max_workers=cfg_obj.max_workers,
        deduplicate_by_ip=cfg_obj.deduplicate_by_ip,
    )
    return verify.nodes_from_vless_survivors(relay_survivors)


def _tick_interval(cfg_obj: Config) -> int:
    """Seconds until the next refresh: the smallest enabled refresh interval."""
    intervals = []
    for prof in cfg_obj.profiles:
        if not prof.enabled or not prof.url:
            continue
        interval = (
            prof.interval if prof.interval is not None else cfg_obj.update_interval
        )
        if interval > 0:
            intervals.append(interval)
    return min(intervals) if intervals else (cfg_obj.update_interval or 60)


def _commit_refresh(fresh: cfg.Config, results: dict[str, list[cfg.Node]]) -> None:
    """Apply freshly fetched nodes to a profile, keyed by name, on a fresh config."""
    for name, new_nodes in results.items():
        prof = nodes.find_profile(fresh, name)
        if prof is not None:
            prof.nodes = new_nodes
            prof.last_update = datetime.now()


def run_scheduler() -> None:
    """Periodically refresh enabled profiles when their refresh interval elapses."""
    time.sleep(random.random() * 60)  # noqa: S311
    while True:
        results: dict[str, list[cfg.Node]] = {}
        errors: dict[str, Exception] = {}
        cfg_obj = cfg.load_config()
        for prof in cfg_obj.profiles:
            if not prof.enabled or not prof.url:
                continue
            interval = (
                prof.interval if prof.interval is not None else cfg_obj.update_interval
            )
            if interval <= 0:
                continue
            if prof.last_update:
                elapsed = (datetime.now() - prof.last_update).total_seconds()
                if elapsed < interval:
                    continue
            try:
                results[prof.name] = fetch_profile_nodes(prof, cfg_obj)
            except Exception as exc:
                errors[prof.name] = exc

        if results:
            cfg.update_config(
                cfg.CONFIG_PATH, partial(_commit_refresh, results=results)
            )

        for name, err in errors.items():
            console.print(f"[yellow][{name}] refresh failed: {err}[/yellow]")
        time.sleep(_tick_interval(cfg.load_config()))
