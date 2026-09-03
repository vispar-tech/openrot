from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from openrot import self_update as su


class TestVersionTuple:
    def test_strips_v_prefix(self) -> None:
        assert su._version_tuple("v1.2.3") == (1, 2, 3)

    def test_without_v(self) -> None:
        assert su._version_tuple("0.1.1") == (0, 1, 1)

    def test_truncates_on_non_int(self) -> None:
        assert su._version_tuple("1.2.3-beta") == (1, 2)

    def test_empty_string(self) -> None:
        assert su._version_tuple("") == ()

    def test_single_number(self) -> None:
        assert su._version_tuple("v5") == (5,)


class TestCurrentOs:
    def test_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        assert su._current_os() == "macos"

    def test_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert su._current_os() == "linux"

    def test_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        with pytest.raises(RuntimeError, match="unsupported OS"):
            su._current_os()


class TestCurrentArch:
    def test_x86_64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        assert su._current_arch() == "x86_64"

    def test_amd64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "amd64")
        assert su._current_arch() == "x86_64"

    def test_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        assert su._current_arch() == "aarch64"

    def test_aarch64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "aarch64")
        assert su._current_arch() == "aarch64"

    def test_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform, "machine", lambda: "mips")
        with pytest.raises(RuntimeError, match="unsupported arch"):
            su._current_arch()


class TestBinDir:
    def test_returns_parent_of_executable(self) -> None:
        result = su._bin_dir()
        assert result.is_dir()


class TestCheckForUpdate:
    def test_up_to_date(self) -> None:
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.json.return_value = {"tag_name": "v0.0.0"}
        client.get.return_value = resp
        result = su.check_for_update(client)
        assert result.updated is False
        assert "already up to date" in result.message

    def test_update_available(self) -> None:
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.json.return_value = {"tag_name": "v99.0.0"}
        client.get.return_value = resp
        result = su.check_for_update(client)
        assert result.updated is False
        assert "update available" in result.message
        assert result.latest == "99.0.0"

    def test_network_error(self) -> None:
        client = MagicMock(spec=httpx.Client)
        client.get.side_effect = httpx.ConnectError("timeout")
        result = su.check_for_update(client)
        assert result.updated is False
        assert "failed to check" in result.message


class TestPerformUpdate:
    def test_already_up_to_date(self) -> None:
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.json.return_value = {"tag_name": "v0.0.0"}
        client.get.return_value = resp
        result = su.perform_update(client)
        assert result.updated is False

    def test_download_archive(self, tmp_path: Path) -> None:
        dest = tmp_path / "test.tar.gz"
        client = MagicMock(spec=httpx.Client)
        stream_ctx = MagicMock()
        stream_ctx.__enter__ = MagicMock(return_value=stream_ctx)
        stream_ctx.__exit__ = MagicMock(return_value=False)
        stream_ctx.raise_for_status = MagicMock()
        stream_ctx.headers = {"content-length": "100"}
        stream_ctx.iter_bytes = MagicMock(return_value=iter([b"chunk1", b"chunk2"]))
        client.stream.return_value = stream_ctx

        su._download_archive(client, "http://example.com/test.tar.gz", dest, None)
        assert dest.exists()


class TestInstallExtracted:
    def test_missing_binary(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        err = su._install_extracted(src, dest, "1.0.0")
        assert err is not None
        assert "did not contain" in err

    def test_copies_binary(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "openrot").write_text("#!/bin/sh\necho ok")
        dest = tmp_path / "dest"
        dest.mkdir()
        err = su._install_extracted(src, dest, "1.0.0")
        assert err is None
        assert (dest / "openrot").exists()
        assert (dest / "openrot").read_text() == "#!/bin/sh\necho ok"

    def test_copies_internal_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "openrot").write_text("#!/bin/sh\necho ok")
        internal = src / "_internal"
        internal.mkdir()
        (internal / "lib.py").write_text("x = 1")
        dest = tmp_path / "dest"
        dest.mkdir()
        err = su._install_extracted(src, dest, "1.0.0")
        assert err is None
        assert (dest / "_internal" / "lib.py").read_text() == "x = 1"

    def test_removes_old_internal(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "openrot").write_text("new")
        internal = src / "_internal"
        internal.mkdir()
        (internal / "new.py").write_text("new")
        dest = tmp_path / "dest"
        dest.mkdir()
        old_internal = dest / "_internal"
        old_internal.mkdir()
        (old_internal / "old.py").write_text("old")
        err = su._install_extracted(src, dest, "1.0.0")
        assert err is None
        assert not (dest / "_internal" / "old.py").exists()
        assert (dest / "_internal" / "new.py").read_text() == "new"


class TestDoUpdate:
    def test_up_to_date_returns_early(self) -> None:
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.json.return_value = {"tag_name": "v0.0.0"}
        client.get.return_value = resp
        result = su._do_update(client, None)
        assert result.updated is False


class TestUpdateResult:
    def test_fields(self) -> None:
        r = su.UpdateResult(current="1.0.0", latest="2.0.0", updated=True, message="ok")
        assert r.current == "1.0.0"
        assert r.latest == "2.0.0"
        assert r.updated is True
        assert r.message == "ok"


class TestServiceState:
    def test_fields(self) -> None:
        s = su.ServiceState(daemon_running=True, proxy_running=False)
        assert s.daemon_running is True
        assert s.proxy_running is False


class TestCheckServices:
    def test_nothing_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(su.daemon, "load_daemon_pid", lambda _: None)
        monkeypatch.setattr(su.proxy, "load_pid", lambda _: None)
        state = su._check_services()
        assert state.daemon_running is False
        assert state.proxy_running is False

    def test_daemon_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(su.daemon, "load_daemon_pid", lambda _: 123)
        monkeypatch.setattr(su.proxy, "load_pid", lambda _: None)
        monkeypatch.setattr(su.proxy, "is_running", lambda _: True)
        state = su._check_services()
        assert state.daemon_running is True
        assert state.proxy_running is False

    def test_proxy_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(su.daemon, "load_daemon_pid", lambda _: None)
        monkeypatch.setattr(su.proxy, "load_pid", lambda _: 456)
        monkeypatch.setattr(su.proxy, "is_running", lambda _: True)
        state = su._check_services()
        assert state.daemon_running is False
        assert state.proxy_running is True

    def test_both_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(su.daemon, "load_daemon_pid", lambda _: 123)
        monkeypatch.setattr(su.proxy, "load_pid", lambda _: 456)
        monkeypatch.setattr(su.proxy, "is_running", lambda _: True)
        state = su._check_services()
        assert state.daemon_running is True
        assert state.proxy_running is True


class TestStopServices:
    def test_stops_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stopped: list[str] = []
        monkeypatch.setattr(
            su.proxy, "stop_proxy", lambda: stopped.append("proxy") or True
        )
        monkeypatch.setattr(
            su.daemon,
            "stop_and_wait",
            lambda p: stopped.append(str(p)) or True,
        )
        su._stop_services()
        assert "proxy" in stopped
        assert len(stopped) == 3  # proxy + daemon PID + bridge PID


class TestRestartServices:
    def test_restarts_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[str] = []
        monkeypatch.setattr(
            su.daemon,
            "daemon_start_background",
            lambda name, pid: started.append(name),
        )
        su._restart_services(su.ServiceState(daemon_running=True, proxy_running=False))
        assert started == ["cascade"]

    def test_no_restart_when_not_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started: list[str] = []
        monkeypatch.setattr(
            su.daemon,
            "daemon_start_background",
            lambda name, pid: started.append(name),
        )
        su._restart_services(su.ServiceState(daemon_running=False, proxy_running=False))
        assert started == []
