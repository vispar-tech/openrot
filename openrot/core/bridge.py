"""Loopback HTTP bridge that routes opencode traffic through the cascade.

``openrot start bridge`` brings the cascade up and serves the loopback bridge
in the foreground (Ctrl-C stops it); ``openrot start bridge --daemon`` forks it
into a persistent background process. opencode is pointed at the bridge via the
manual ``provider.opencode.options.baseURL`` override in the README; ``status``
reports whether the bridge is currently listening.

Requests arrive as plain HTTP on 127.0.0.1 and are forwarded *through the
active cascade* (the sing-box proxy on ``127.0.0.1:{cascade_port}``) to
``bridge_upstream``. An upstream HTTP ``429`` rotates the cascade once
(``cascade.rotate()`` — next node / WARP) and retries the request, so a
rate-limited node is swapped out transparently. Only the upstream leg goes over
TLS (handled by ``httpx``); the client leg is plain HTTP — unlike a CONNECT
tunnel — so the 429 is visible here without any MITM.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

import httpx

from openrot import config as cfg
from openrot import signals
from openrot.config import ActiveLevel, Config
from openrot.core import cascade, daemon
from openrot.log import get_logger
from openrot.models.config import DEFAULT_BRIDGE_UPSTREAM

events = get_logger()


def _log(msg: str) -> None:
    """Log a message through the events logger with consistent formatting."""
    events.info(msg)


# Hop-by-hop headers never forwarded upstream.
_HOP_BY_HOP = {
    "connection",
    "content-length",
    "transfer-encoding",
    "host",
    "proxy-connection",
}


class UpstreamError(Exception):
    """Raised when the upstream leg of a bridge request fails."""


def upstream_url(cfg_obj: Config, path: str) -> str:
    """Join the configured upstream base with the incoming request path."""
    base = cfg_obj.bridge_upstream or DEFAULT_BRIDGE_UPSTREAM
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


@dataclass(frozen=True)
class _Request:
    """A single HTTP request the bridge forwards upstream."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


def _client(cfg_obj: Config) -> httpx.Client:
    proxy_url = f"http://127.0.0.1:{cfg_obj.port}"
    return httpx.Client(proxy=proxy_url, timeout=300)


def _forward_headers(headers: dict[str, str], host: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        out[key] = value
    if "content-type" not in {k.lower() for k in out}:
        out["Content-Type"] = "application/json"
    out["Host"] = host
    return out


def _fetch(
    client: httpx.Client,
    cfg_obj: Config,
    request: _Request,
) -> httpx.Response:
    url = upstream_url(cfg_obj, request.path)
    host_header = httpx.URL(url).host  # type: ignore[attr-defined]
    out_headers = _forward_headers(request.headers, host_header or "")
    try:
        return client.request(
            request.method, url, headers=out_headers, content=request.body
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(str(exc)) from exc


def _is_rate_limited(resp: httpx.Response, statuses: list[int]) -> bool:
    return resp.status_code in statuses


def _rotate_and_retry(cfg_obj: Config, request: _Request) -> httpx.Response:
    """Rotate the cascade once, then retry the request once."""
    events.warning("upstream retryable status, rotating cascade")
    t0 = time.monotonic()
    with contextlib.suppress(SystemExit):  # no alive node available
        cascade.rotate()
    elapsed = time.monotonic() - t0
    _log(f"[warp] rotation took {elapsed:.1f}s")
    with _client(cfg_obj) as client:
        return _fetch(client, cfg_obj, request)


def forward(
    cfg_obj: Config,
    request: _Request,
    *,
    rotate_on_429: bool = True,
) -> tuple[httpx.Response, httpx.Client]:
    """Send one logical request through the cascade, rotating on retryable status.

    On an HTTP status in ``cfg.bridge_retry_statuses`` (default ``[429]``) the
    bridge rotates the cascade and retries the same request up to
    ``cfg.bridge_retry_attempts`` (default 1) times before giving up. Returns
    ``(response, client)``; the client is the owner of ``response`` and the
    caller must close it once the response body has been consumed.
    """
    client = _client(cfg_obj)
    resp = _fetch(client, cfg_obj, request)
    statuses = cfg_obj.bridge_retry_statuses
    attempts = cfg_obj.bridge_retry_attempts
    if rotate_on_429 and attempts > 0 and resp.status_code in statuses:
        client.close()
        left = attempts
        while left > 0:
            left -= 1
            resp = _rotate_and_retry(cfg_obj, request)
            if resp.status_code not in statuses:
                break
        client = _client(cfg_obj)
    return resp, client


def _respond(handler: BaseHTTPRequestHandler, resp: httpx.Response) -> None:
    """Write an upstream response back to the connected client, streaming the body."""
    try:
        handler.send_response(resp.status_code)
        for key, value in resp.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            with contextlib.suppress(ValueError, OSError):
                handler.send_header(key, value)
        handler.end_headers()
        for chunk in resp.iter_bytes():
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except BrokenPipeError, ConnectionResetError, OSError:
        pass
    finally:
        resp.close()


def _is_loopback(host: str) -> bool:
    """True for loopback bind hosts (local names and 127.0.0.1 / ::1)."""
    return host in {"localhost", "127.0.0.1", "::1"}


def warn_if_exposed(host: str) -> None:
    """Print a loud warning when the bridge binds beyond the loopback.

    The bridge proxies raw requests (incl. Authorization headers) to the
    upstream, so binding to 0.0.0.0 or a host interface makes other machines
    able to reach it. Only ``OPENROT_LISTEN`` does that — the default is the
    loopback. Warn loudly so an accidental Docker port publish is obvious.
    """
    if _is_loopback(host):
        return
    _log(
        f"SECURITY: bridge binds to {host!r}, reachable from other machines. "
        "It forwards your Authorization headers, so keep it on 127.0.0.1 "
        "(the default). Open ports to the outside world only if you truly "
        "intend to (Docker port publish / OPENROT_LISTEN)."
    )


def _running_level(cfg_obj: cfg.Config, check: Callable[[cfg.Config], bool]) -> bool:
    return cfg_obj.active_level != ActiveLevel.NONE and check(cfg_obj)


def base_url(cfg_obj: cfg.Config) -> str:
    """Return the loopback baseURL opencode should target for the bridge."""
    return f"http://127.0.0.1:{cfg_obj.bridge_port}/v1"


def running(cfg_obj: cfg.Config) -> bool:
    """Return True when the loopback bridge is currently listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", cfg_obj.bridge_port)) == 0


def serve() -> None:
    """Ensure the cascade is up and run the bridge server in the foreground.

    Standalone bridge for testing (``openrot start bridge``): start it, then
    point any OpenAI-compatible client at the loopback URL and watch it route
    through the cascade with 429 self-rotation. Ctrl-C stops it.
    """
    cfg_obj = cfg.load_config()
    if not _running_level(cfg_obj, cascade.level_serving):
        _log("no active level, starting cascade...")
        cascade.start(False, False)
        cfg_obj = cfg.load_config()

    url = base_url(cfg_obj)
    _log(
        f"bridge: listening on {url} "
        f"(upstream {cfg_obj.bridge_upstream}, level {cfg_obj.active_level.value})"
    )
    if sys.stdout.isatty():
        _log("Ctrl-C to stop.")
    host = cfg.listen_address()
    warn_if_exposed(host)
    signals.keyboard_on_sigterm()
    server = Bridge(host, cfg_obj.bridge_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("\nstopping bridge...")
    finally:
        server.server_close()


def daemonize() -> None:
    """Fork the bridge server into a persistent background daemon."""
    daemon.start(
        name="bridge",
        pid_path=cfg.BRIDGE_PID_PATH,
        log_path=cfg.BRIDGE_LOG_PATH,
    )


def stop_daemon() -> bool:
    """Terminate a background bridge daemon if it is running."""
    return daemon.stop(cfg.BRIDGE_PID_PATH)


class BridgeHandler(BaseHTTPRequestHandler):
    """Forward every request to the configured upstream through the cascade."""

    server_version = "openrot-bridge/1"

    def _log_request_console(self, method: str) -> None:
        elapsed_s = self._elapsed_ms / 1000
        parts = [f"{method} {self.path}"]
        model = getattr(self, "_model", "")
        if model:
            parts.append(model)
        prompt_chars = getattr(self, "_prompt_chars", 0)
        if prompt_chars:
            parts.append(
                f"prompt={prompt_chars / 1000:.1f}k"
                if prompt_chars >= 1000
                else f"prompt={prompt_chars}"
            )
        if elapsed_s >= 1:
            parts.append(f"{elapsed_s:.1f}s")
        else:
            parts.append(f"{self._elapsed_ms:.0f}ms")
        _log(f"[bridge] {' '.join(parts)}")

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        model = ""
        prompt_chars = 0
        if body:
            try:
                parsed = json.loads(body)
                model = parsed.get("model", "")
                messages = parsed.get("messages", [])
                if isinstance(messages, list):
                    prompt_chars = sum(
                        len(m.get("content", "")) if isinstance(m, dict) else 0
                        for m in messages
                    )
            except json.JSONDecodeError, AttributeError:
                pass

        self._model = model
        self._prompt_chars = prompt_chars

        cfg_obj = cfg.load_config()
        request = _Request(method, self.path, dict(self.headers.items()), body)
        t0 = time.monotonic()
        try:
            resp, client = forward(cfg_obj, request)
        except UpstreamError as exc:
            events.warning("bridge upstream error, rotating and retrying: %s", exc)
            t_rotate = time.monotonic()
            with contextlib.suppress(SystemExit):
                cascade.rotate()
            elapsed_rot = time.monotonic() - t_rotate
            _log(f"[warp] rotation took {elapsed_rot:.1f}s")
            try:
                cfg_obj = cfg.load_config()
                resp, client = forward(cfg_obj, request, rotate_on_429=False)
            except UpstreamError as retry_exc:
                self._elapsed_ms = (time.monotonic() - t0) * 1000
                self._log_request_console(method)
                msg = {"error": {"message": str(retry_exc), "type": "upstream"}}
                payload = json.dumps(msg).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        try:
            _respond(self, resp)
        finally:
            self._elapsed_ms = (time.monotonic() - t0) * 1000
            client.close()
            self._log_request_console(method)

    def do_GET(self) -> None:  # noqa: D102
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: D102
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: D102
        self._handle("OPTIONS")

    def do_PUT(self) -> None:  # noqa: D102
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: D102
        self._handle("DELETE")

    def do_PATCH(self) -> None:  # noqa: D102
        self._handle("PATCH")

    def do_HEAD(self) -> None:  # noqa: D102
        self._handle("HEAD")

    def log_message(self, fmt: str, *args: object) -> None:
        """Log a request line to the events log."""
        parts = [fmt % args]

        model = getattr(self, "_model", "")
        if model:
            parts.append(f"model={model}")

        prompt_chars = getattr(self, "_prompt_chars", 0)
        if prompt_chars:
            if prompt_chars >= 1000:
                parts.append(f"prompt={prompt_chars / 1000:.1f}k")
            else:
                parts.append(f"prompt={prompt_chars}")

        elapsed = getattr(self, "_elapsed_ms", 0)
        if elapsed:
            if elapsed >= 1000:
                parts.append(f"{elapsed / 1000:.1f}s")
            else:
                parts.append(f"{elapsed:.0f}ms")

        events.info("bridge %s", " ".join(parts))


class Bridge(ThreadingHTTPServer):
    """A threaded loopback server exposing the openrot bridge."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), BridgeHandler)
