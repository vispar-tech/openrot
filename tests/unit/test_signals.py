import signal

import pytest

from openrot import signals


def test_keyboard_on_sigterm_registers_raise_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[tuple[int, object]] = []

    def fake_signal(signum: int, handler: object) -> None:
        installed.append((signum, handler))

    monkeypatch.setattr(signals.signal, "signal", fake_signal)
    signals.keyboard_on_sigterm()
    assert len(installed) == 1
    signum, handler = installed[0]
    assert signum == signal.SIGTERM
    with pytest.raises(KeyboardInterrupt):
        handler(None, None)  # type: ignore[arg-type]
