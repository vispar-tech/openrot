"""Self-update: download and install the latest release binary from GitHub."""

from __future__ import annotations

import platform
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import NamedTuple

import httpx

from openrot import __version__

GH_REPO = "vispar-tech/openrot"
RELEASE_URL = f"https://api.github.com/repos/{GH_REPO}/releases/latest"
DOWNLOAD_BASE = f"https://github.com/{GH_REPO}/releases/download"


class UpdateResult(NamedTuple):
    """Result of a self-update check or install operation."""

    current: str
    latest: str
    updated: bool
    message: str


def _current_os() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    raise RuntimeError(f"unsupported OS: {system}")


def _current_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise RuntimeError(f"unsupported arch: {machine}")


def _bin_dir() -> Path:
    """Return the directory containing the running binary."""
    return Path(sys.executable).resolve().parent


def _fetch_latest_tag(client: httpx.Client) -> str:
    resp = client.get(RELEASE_URL, timeout=15)
    resp.raise_for_status()
    return resp.json()["tag_name"]


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse 'v0.1.1' or '0.1.1' into (0, 1, 1)."""
    v = v.lstrip("v")
    parts: list[int] = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)


def check_for_update(client: httpx.Client | None = None) -> UpdateResult:
    """Check if a newer release is available."""
    close = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True)
    try:
        latest_tag = _fetch_latest_tag(client)
    except Exception as exc:
        return UpdateResult(
            current=__version__,
            latest=__version__,
            updated=False,
            message=f"failed to check for updates: {exc}",
        )
    finally:
        if close:
            client.close()

    current = _version_tuple(__version__)
    latest = _version_tuple(latest_tag)

    if latest <= current:
        return UpdateResult(
            current=__version__,
            latest=latest_tag.lstrip("v"),
            updated=False,
            message=f"already up to date ({__version__})",
        )

    return UpdateResult(
        current=__version__,
        latest=latest_tag.lstrip("v"),
        updated=False,
        message=f"update available: {__version__} -> {latest_tag.lstrip('v')}",
    )


def perform_update(
    client: httpx.Client | None = None,
    *,
    progress_fn: object | None = None,
) -> UpdateResult:
    """Download and install the latest release, replacing the current binary."""
    close = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True)
    try:
        return _do_update(client, progress_fn)
    finally:
        if close:
            client.close()


def _download_archive(
    client: httpx.Client,
    url: str,
    dest: Path,
    progress_fn: object | None,
) -> None:
    if progress_fn and callable(progress_fn):
        progress_fn("downloading", 0, 1)
    with client.stream("GET", url, timeout=120) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_fn and callable(progress_fn) and total:
                    progress_fn("downloading", downloaded, total)


def _install_extracted(src_dir: Path, dest_dir: Path, version_str: str) -> str | None:
    """Replace the running binary. Returns an error message or None."""
    launcher = src_dir / "openrot"
    if not launcher.exists():
        return "downloaded archive did not contain an openrot binary"

    new_internal = src_dir / "_internal"
    old_internal = dest_dir / "_internal"
    if new_internal.is_dir():
        if old_internal.exists():
            shutil.rmtree(old_internal)
        shutil.copytree(new_internal, old_internal)
        shutil.copystat(new_internal, old_internal)

    target = dest_dir / "openrot"
    perms = launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    launcher.chmod(perms)
    shutil.copy2(launcher, target)
    return None


def _do_update(
    client: httpx.Client,
    progress_fn: object | None,
) -> UpdateResult:
    latest_tag = _fetch_latest_tag(client)
    current = _version_tuple(__version__)
    latest = _version_tuple(latest_tag)

    if latest <= current:
        return UpdateResult(
            current=__version__,
            latest=latest_tag.lstrip("v"),
            updated=False,
            message=f"already up to date ({__version__})",
        )

    version_str = latest_tag.lstrip("v")
    filename = f"openrot-{latest_tag}-{_current_os()}-{_current_arch()}.tar.gz"
    url = f"{DOWNLOAD_BASE}/{latest_tag}/{filename}"
    dest_dir = _bin_dir()
    tmp = tempfile.mkdtemp(prefix="openrot-update-")
    try:
        archive_path = Path(tmp) / filename
        _download_archive(client, url, archive_path, progress_fn)

        if progress_fn and callable(progress_fn):
            progress_fn("extracting", 0, 1)

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(tmp, filter="data")

        src_dir = Path(tmp)
        inner = src_dir / "openrot"
        if inner.is_dir() and (inner / "openrot").exists():
            src_dir = inner

        err = _install_extracted(src_dir, dest_dir, version_str)
        if err:
            return UpdateResult(
                current=__version__,
                latest=version_str,
                updated=False,
                message=err,
            )

        if progress_fn and callable(progress_fn):
            progress_fn("done", 1, 1)

        return UpdateResult(
            current=__version__,
            latest=version_str,
            updated=True,
            message=f"updated {__version__} -> {version_str}",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
