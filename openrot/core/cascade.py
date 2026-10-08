import threading
import time

from rich.console import Console

from openrot import config as cfg
from openrot import log
from openrot.config import ActiveLevel, Config
from openrot.providers import warp

console = Console()
events = log.get_logger()
_rotate_lock = threading.Lock()
_rotate_in_progress = threading.Event()


def start() -> bool:
    """Connect WARP — the only serving level.

    Never raises SystemExit: the bridge daemon must survive WARP being
    down at startup (the health loop keeps retrying). Returns True when
    WARP ends up connected.
    """
    cfg_obj = cfg.load_config()
    if not warp.connect():
        events.warning("[cascade] warp connect failed; will retry in the health loop")
        return False
    cfg_obj.active_level = ActiveLevel.WARP
    cfg.save_config(cfg_obj)
    w_host, w_port = warp.proxy_address()
    events.info(
        "[cascade] warp active (socks5://%s:%d, ip=%s)",
        w_host,
        w_port,
        warp.current_ip() or "-",
    )
    return True


def warp_health_loop() -> None:
    """Keep WARP connected forever.

    Reconnects when the connection drops and also retries the initial
    connect (WARP may be down when the bridge starts).
    """
    while True:
        cfg_obj = cfg.load_config()
        time.sleep(cfg_obj.health_interval)
        if warp.is_connected():
            continue
        events.warning("[cascade] warp not connected; (re)connecting")
        start()


def background() -> None:
    """Run the WARP health loop in a daemon thread.

    Used by the bridge daemon (``serve()``): without this, a dropped WARP
    connection would leave the bridge serving 502s forever.
    """
    threading.Thread(
        target=warp_health_loop, name="openrot-warp-health", daemon=True
    ).start()


def rotate() -> bool:
    """Rotate the WARP IP.

    Thread-safe: concurrent calls are serialized. When a rotation is already
    in progress the caller waits for it to finish and returns ``False``
    (the rotation has already been performed by someone else). Returns ``True``
    when this caller actually performed the rotation.
    """
    with _rotate_lock:
        if _rotate_in_progress.is_set():
            waiting = True
        else:
            _rotate_in_progress.set()
            waiting = False
    if waiting:
        _rotate_in_progress.wait()
        return False
    try:
        _rotate_inner()
        return True
    finally:
        _rotate_in_progress.clear()


def _rotate_inner() -> None:
    if warp.rotate():
        ip = warp.current_ip()
        events.info("[cascade] warp rotated, ip=%s", ip or "-")
        console.print(f"warp rotated, ip {ip or '-'}")
    else:
        events.warning("[cascade] warp rotation failed")
        console.print("[red]warp rotation failed[/red]")


def stop() -> None:
    """Disconnect WARP and reset the persisted active state."""
    warp.disconnect()
    cfg_obj = cfg.load_config()
    cfg_obj.active_level = ActiveLevel.NONE
    cfg.save_config(cfg_obj)
    console.print("WARP disconnected")
    events.info("[cascade] WARP disconnected")


def level_serving(cfg_obj: Config) -> bool:
    """Return True when the current active level is actually serving."""
    return warp.is_connected()
