"""PID file handling: a small class shared by proxy, daemon, and bridge."""

import os
import tempfile
from pathlib import Path


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
