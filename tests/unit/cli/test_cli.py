import pytest
from typer.testing import CliRunner

from openrot import cli
from openrot import config as cfg
from openrot.cli import app


def test_logs_no_follow_tails_log_file(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a[0]))
    log_path = cfg.CONFIG_PATH.parent / "test-openrot.log"
    monkeypatch.setattr(cli.cfg, "LOG_PATH", log_path)
    log_path.write_text("line 1\nline 2\nline 3\n")

    result = CliRunner().invoke(app, ["logs", "--no-follow", "-n", "2"])
    assert result.exit_code == 0
    assert any("line 2" in p for p in printed)
    assert any("line 3" in p for p in printed)


def test_logs_missing_file_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a[0]))
    monkeypatch.setattr(
        cli.cfg, "LOG_PATH", cfg.CONFIG_PATH.parent / "does-not-exist.log"
    )

    result = CliRunner().invoke(app, ["logs", "--no-follow"])
    assert result.exit_code == 0
    assert any("no log yet" in p for p in printed)


def test_logs_single_source(monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []

    def fake_print(msg: object, *a: object, **k: object) -> None:
        printed.append(str(msg))

    monkeypatch.setattr(cli.console, "print", fake_print)
    log_path = cfg.CONFIG_PATH.parent / "follow-openrot.log"
    monkeypatch.setattr(cli.cfg, "LOG_PATH", log_path)
    log_path.write_text("log tail\n")
    monkeypatch.setattr(cli, "_read_last_lines", lambda path, count: ["TAIL-A"])

    def fake_sleep(t: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        cli._follow_log(log_path, 2)

    assert any("TAIL-A" in p for p in printed)
