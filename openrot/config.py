import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml  # type: ignore[import-untyped]

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

from pydantic import ValidationError

from openrot.models import (
    TOP_LIMIT,
    ActiveLevel,
    Config,
    Node,
    NodeProtocol,
    NodeStatus,
    Profile,
    ProfileKind,
    Strategy,
    node_id,
)

__all__ = [
    "BRIDGE_LOG_PATH",
    "BRIDGE_PID_PATH",
    "CONFIG_PATH",
    "DAEMON_PID_PATH",
    "EVENT_LOG_PATH",
    "LOG_PATH",
    "PID_PATH",
    "TOP_LIMIT",
    "ActiveLevel",
    "Config",
    "ConfigError",
    "Node",
    "NodeProtocol",
    "NodeStatus",
    "Profile",
    "ProfileKind",
    "Strategy",
    "load_config",
    "node_id",
    "save_config",
    "update_config",
]


class ConfigError(ValueError):
    """Raised when the config file cannot be read or validated."""


def _base_dir() -> Path:
    env = os.environ.get("OPENROT_DIR")
    if env:
        return Path(env)
    return Path.home() / ".config" / "openrot"


def _config_path() -> Path:
    env = os.environ.get("OPENROT_CONFIG")
    if env:
        return Path(env)
    return _base_dir() / "config.yaml"


def _pid_path() -> Path:
    return _base_dir() / "openrot.pid"


def _daemon_pid_path() -> Path:
    return _base_dir() / "openrot.daemon.pid"


def _bridge_pid_path() -> Path:
    return _base_dir() / "openrot-bridge.daemon.pid"


def _bridge_log_path() -> Path:
    return _base_dir() / "openrot-bridge.log"


def _log_path() -> Path:
    return _base_dir() / "openrot.log"


def _event_log_path() -> Path:
    return _base_dir() / "openrot-events.log"


CONFIG_PATH = _config_path()
PID_PATH = _pid_path()
DAEMON_PID_PATH = _daemon_pid_path()
BRIDGE_PID_PATH = _bridge_pid_path()
BRIDGE_LOG_PATH = _bridge_log_path()
LOG_PATH = _log_path()
EVENT_LOG_PATH = _event_log_path()


def _lock_for(path: Path) -> Path:
    return path.parent / (path.name + ".lock")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    lock_path = _lock_for(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _env_int(name: str, attr: str, cfg: Config) -> None:
    """Apply an integer-valued env override, raising ``ConfigError`` on bad input."""
    raw = os.environ.get(name)
    if not raw:
        return
    try:
        setattr(cfg, attr, int(raw))
    except (ValueError, ValidationError) as exc:
        raise ConfigError(f"bad {name} {raw!r}: {exc}") from exc


def _env_int_list(name: str, attr: str, cfg: Config) -> None:
    """Apply a comma-separated int-list env override, raising on bad input."""
    raw = os.environ.get(name)
    if not raw:
        return
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    try:
        setattr(cfg, attr, [int(p) for p in parts])
    except ValueError as exc:
        raise ConfigError(f"bad {name} {raw!r}: {exc}") from exc


def _apply_env_overrides(cfg: Config) -> Config:
    _env_int("OPENROT_PORT", "port", cfg)
    singbox_bin = os.environ.get("OPENROT_SINGBOX_BIN")
    if singbox_bin:
        cfg.singbox_bin = singbox_bin
    _env_int("OPENROT_BRIDGE_PORT", "bridge_port", cfg)
    bridge_upstream = os.environ.get("OPENROT_UPSTREAM")
    if bridge_upstream:
        cfg.bridge_upstream = bridge_upstream
    _env_int_list("OPENROT_BRIDGE_RETRY_STATUSES", "bridge_retry_statuses", cfg)
    _env_int("OPENROT_BRIDGE_RETRY_ATTEMPTS", "bridge_retry_attempts", cfg)
    _env_int("OPENROT_MAX_WORKERS", "max_workers", cfg)
    return cfg


def listen_address() -> str:
    """Return the address the proxy and bridge listeners bind to.

    Defaults to the loopback; set ``OPENROT_LISTEN`` to e.g. ``0.0.0.0`` when the
    services must be reachable outside the host (Docker publishes ports).
    """
    return os.environ.get("OPENROT_LISTEN", "127.0.0.1")


def _load_unlocked(path: Path) -> Config:
    """Load configuration without taking the file lock (caller holds it)."""
    if not path.exists():
        cfg = Config()
        _save_unlocked(cfg, path)
        return _apply_env_overrides(cfg)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return _apply_env_overrides(Config(**data))
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {path}:\n{exc}") from exc


def _save_unlocked(cfg: Config, path: Path) -> None:
    """Write configuration to `path` without taking the file lock (caller holds it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(cfg.model_dump(mode="json"), f)
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load configuration, creating a default one (with env overrides) if missing."""
    with _file_lock(path):
        return _load_unlocked(path)


def save_config(cfg: Config, path: Path = CONFIG_PATH) -> None:
    """Atomically write configuration to `path` (temp file + rename)."""
    with _file_lock(path):
        _save_unlocked(cfg, path)


def update_config[T](path: Path, mutator: Callable[[Config], T]) -> T | None:
    """Atomically load the persisted config, apply `mutator`, and save it.

    The whole read-modify-write runs under a single file lock, so concurrent
    writers (scheduler and health loops in the daemon) cannot clobber each
    other's updates. `mutator` must be side-effect free apart from editing the
    Config it receives (no network or blocking I/O). Its return value is
    forwarded to the caller.
    """
    with _file_lock(path):
        cfg = _load_unlocked(path)
        result = mutator(cfg)
        _save_unlocked(cfg, path)
        return result
