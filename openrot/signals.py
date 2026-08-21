"""Signal helpers shared by the long-running foreground commands.

Installing an explicit ``SIGTERM`` handler that raises ``KeyboardInterrupt``
makes the foreground loops (health, log following, bridge serving) exit cleanly
on ``SIGTERM`` — which is what Docker sends on ``docker stop``. Without it some
environments (e.g. CPython 3.14 inside containers) can leave PID 1 running
until Docker escalates to ``SIGKILL`` after the grace period.
"""

from __future__ import annotations

import signal


def _raise_keyboard_interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def keyboard_on_sigterm() -> None:
    """Turn ``SIGTERM`` into a ``KeyboardInterrupt`` for the current process."""
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
