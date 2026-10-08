"""PID file handling: a small class plus process-liveness helpers."""

import os
import tempfile
from pathlib import Path

from openrot import config as cfg


class PidFile:
    """A pid file on disk: save, load, and remove the stored pid.

    Uses atomic writes with 0600 permissions, and treats a missing or
    non-integer file as "no pid".
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, pid: int) -> None:
        """Write `pid` atomically with 0600 permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(str(pid))
        Path(tmp).replace(self.path)

    def load(self) -> int | None:
        """Read the stored pid, or None when absent or invalid."""
        if not self.path.exists():
            return None
        try:
            return int(self.path.read_text().strip())
        except ValueError:
            return None

    def remove(self) -> None:
        """Delete the pid file if present."""
        self.path.unlink(missing_ok=True)


def is_running(pid: int) -> bool:
    """Return True when a process with the given pid is alive.

    Uses ``os.kill(pid, 0)`` (no ``kill`` binary needed, so it works in minimal
    containers), treating permission errors as "alive" since the process exists.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def save_daemon_pid(pid: int, path: Path = cfg.DAEMON_PID_PATH) -> None:
    """Write the daemon pid to `path`."""
    PidFile(path).save(pid)


def load_daemon_pid(path: Path = cfg.DAEMON_PID_PATH) -> int | None:
    """Read the daemon pid from `path`, or None when absent or invalid."""
    return PidFile(path).load()
