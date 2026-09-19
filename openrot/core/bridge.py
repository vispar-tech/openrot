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
import os
import secrets
import socket
import string
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

import httpx
from rich.console import Console

from openrot import config as cfg
from openrot import signals
from openrot.config import ActiveLevel, Config
from openrot.core import cascade, daemon
from openrot.core.http import make_client
from openrot.core.singbox import port_in_use
from openrot.log import get_logger as _get_logger
from openrot.models.config import DEFAULT_BRIDGE_UPSTREAM


@dataclass(frozen=True)
class _Request:
    """A single HTTP request the bridge forwards upstream."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


# True hop-by-hop headers, stripped on both legs.
_HOP_BY_HOP = {"connection", "host", "proxy-connection"}

# Request leg: transport framing is re-created from the raw body by httpx.
# ``x-opencode-client`` is an opencode client marker the gateway does not
# inspect, so it is dropped rather than forwarded.
_REQUEST_STRIP = _HOP_BY_HOP | {
    "transfer-encoding",
    "content-length",
}

# Response leg: framing is re-created below (content-length or chunked).
_RESPONSE_STRIP = _HOP_BY_HOP | {"transfer-encoding"}

_ALNUM = string.ascii_letters + string.digits

# User-Agent the free tier gateway expects; injected when the client sends
# none or a non-opencode one (curl, python-requests, ...).
_OPENCODE_UA = "opencode/1.18.30 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"

# Stub tools injected into bodies that carry no (or too few) tools: the free
# tier gateway requires a ``tools`` array with at least two elements and
# validates the tool names against real opencode tools (bash, read, ...).
_STUB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run",
            "parameters": {
                "type": "object",
                "properties": {"c": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "read",
            "parameters": {
                "type": "object",
                "properties": {"p": {"type": "string"}},
            },
        },
    },
]

_RETRY_DELAY_DEFAULT = 5.0


events = _get_logger()
console = Console()

_shared_client: httpx.Client | None = None
_shared_client_lock = threading.Lock()

_request_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()
_last_start_monotonic: float = 0.0
_pacing_lock = threading.Lock()


def _log(msg: str) -> None:
    """Log a message through the events logger with consistent formatting."""
    events.info(msg)


def _mask_secret(value: str) -> str:
    """Mask a credential, keeping only a hint of its prefix and tail."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _debug_request(
    method: str, path: str, headers: dict[str, str], body: bytes
) -> None:
    """Log the incoming request in full to diagnose upstream differences.

    Opt-in via ``OPENROT_DEBUG_REQ=1``; secrets in auth headers are masked.
    """
    header_map = {k.lower(): v for k, v in headers.items()}
    for name in ("authorization", "openai-api-key", "proxy-authorization", "api-key"):
        if name in header_map:
            header_map[name] = _mask_secret(header_map[name])
    dump = " ".join(f"{k}={v}" for k, v in sorted(header_map.items()))
    events.debug("[bridge][debug] %s %s hdr: %s", method, path, dump)
    try:
        data = json.loads(body)
    except json.JSONDecodeError, AttributeError:
        events.debug("[bridge][debug] body: %r", body[:500])
        return
    messages = data.get("messages")
    messages = messages if isinstance(messages, list) else []
    tool_names = []
    for tool in data.get("tools", []) or []:
        if isinstance(tool, dict):
            fn = tool.get("function", {})
            fn_name: str | None = None
            if isinstance(fn, dict):
                tool_name = fn.get("name")
                fn_name = str(tool_name) if tool_name is not None else None
            if fn_name:
                tool_names.append(fn_name)
    messages_chars = sum(
        len(m.get("content", "")) if isinstance(m, dict) else 0 for m in messages
    )
    events.debug(
        "[bridge][debug] body: model=%s stream=%s max_tokens=%s temperature=%s "
        "n_messages=%s messages_chars=%s tools=%s",
        data.get("model"),
        data.get("stream"),
        data.get("max_tokens"),
        data.get("temperature"),
        len(messages),
        messages_chars,
        tool_names or None,
    )


def _send_502(handler: BaseHTTPRequestHandler, message: str) -> None:
    """Write a JSON 502 response to the connected client."""
    msg = {"error": {"message": message, "type": "upstream"}}
    payload = json.dumps(msg).encode()
    handler.send_response(502)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def upstream_url(cfg_obj: Config, path: str) -> str:
    """Join the configured upstream base with the incoming request path."""
    base = cfg_obj.bridge_upstream or DEFAULT_BRIDGE_UPSTREAM
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _client(cfg_obj: Config) -> httpx.Client:
    proxy_url = f"http://127.0.0.1:{cfg_obj.port}"
    return make_client(proxy=proxy_url, timeout=300)


def _random_id(prefix: str, length: int) -> str:
    """Return a random alnum identifier (``prefix`` + ``length`` chars).

    The request id ``X-Opencode-Request`` is ``msg_``+24 alnum. The server does
    not validate uniqueness or signature, so a fresh random id per request is
    enough.
    """
    return prefix + "".join(secrets.choice(_ALNUM) for _ in range(length))


def _random_session_id() -> str:
    r"""Return a session id the free tier gateway accepts.

    The gateway validates ``X-Opencode-Session`` by format only: ``ses_``
    followed by exactly 26 digits (``^ses_\\d{26}$``). Anything else — 22 alnum,
    shorter/longer digit runs, mixed alnum — is rejected with FreeTierError.
    """
    return "ses_" + "".join(secrets.choice("0123456789") for _ in range(26))


def _ensure_tools(body: bytes) -> bytes:
    """Append the stub tools to the request's tools array.

    The free tier gateway requires a ``tools`` array with at least two
    elements, so the two stub tools are appended to every JSON body —
    unless a tool with the same name is already present.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError, AttributeError:
        return body
    tools = data.get("tools")
    if not isinstance(tools, list):
        tools = []
    existing = {
        tool.get("function", {}).get("name") for tool in tools if isinstance(tool, dict)
    }
    data["tools"] = tools + [
        stub
        for stub in _STUB_TOOLS
        if stub.get("function", {}).get("name") not in existing
    ]
    return json.dumps(data).encode()


def _forward_headers(
    headers: dict[str, str], host: str, inject: bool = True
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _REQUEST_STRIP:
            continue
        out[key] = value
    if inject:
        known = {k.lower(): k for k in out}
        if "x-opencode-session" not in known:
            out["X-Opencode-Session"] = _random_session_id()
        if "x-opencode-request" not in known:
            out["X-Opencode-Request"] = _random_id("msg_", 24)
        ua_key = known.get("user-agent")
        if ua_key is not None and not out[ua_key].lower().startswith("opencode/"):
            del out[ua_key]
            ua_key = None
        if ua_key is None:
            out["User-Agent"] = _OPENCODE_UA
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
    out_headers = _forward_headers(
        request.headers, host_header or "", inject=cfg_obj.bridge_inject_session
    )
    # Ask for identity so httpx neither injects its own Accept-Encoding nor
    # transparently decompresses: the body is then passed through verbatim.
    out_headers["Accept-Encoding"] = "identity"
    if os.environ.get("OPENROT_DEBUG_REQ"):
        _debug_request("OUT", url, out_headers, request.body)
    try:
        req = httpx.Request(
            request.method, url, headers=out_headers, content=request.body
        )
        return client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise httpx.HTTPError(str(exc)) from exc


def _is_usage_limit_error(resp: httpx.Response) -> bool:
    """Return True when a 429 body contains FreeUsageLimitError."""
    if resp.status_code != 429:
        return False
    try:
        data = json.loads(resp.text)
        error = data.get("error", {})
        return isinstance(error, dict) and error.get("type") == "FreeUsageLimitError"
    except json.JSONDecodeError, AttributeError:
        return False


def _retry_delay(resp: httpx.Response) -> float:
    """Parse ``Retry-After`` header or fall back to a fixed delay."""
    if raw := resp.headers.get("Retry-After"):
        try:
            return float(raw)
        except ValueError:
            pass
    return _RETRY_DELAY_DEFAULT


# Shared httpx client — created once, reused across requests.
def _get_client(cfg_obj: Config) -> httpx.Client:
    """Return the shared ``httpx.Client``, creating it on first call."""
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    with _shared_client_lock:
        if _shared_client is not None:
            return _shared_client
        _shared_client = _client(cfg_obj)
        return _shared_client


def _reset_client() -> None:
    """Close and discard the shared client (used in tests and shutdown)."""
    global _shared_client
    with _shared_client_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


# Concurrency limiter — prevents too many parallel upstream requests.
def _get_semaphore(cfg_obj: Config) -> threading.Semaphore:
    """Return the request semaphore, creating it on first call."""
    global _request_semaphore
    if _request_semaphore is not None:
        return _request_semaphore
    with _semaphore_lock:
        if _request_semaphore is not None:
            return _request_semaphore
        _request_semaphore = threading.Semaphore(cfg_obj.bridge_max_concurrent)
        return _request_semaphore


def _pace_start(cfg_obj: Config) -> None:
    """Sleep until the next allowed upstream start, enforcing a global min gap.

    Pacing is global (across all request starts), not per-slot.
    """
    global _last_start_monotonic
    next_start = _last_start_monotonic + cfg_obj.bridge_min_interval
    sleep = max(0.0, next_start - time.monotonic())
    if sleep > 0:
        time.sleep(sleep)
    with _pacing_lock:
        _last_start_monotonic = time.monotonic()


# Rotation logging — shared between forward() and BridgeHandler.
def _log_rotation(rotated: bool, elapsed_s: float) -> None:
    if rotated:
        _log(f"[bridge] rotation took {elapsed_s:.1f}s")
    else:
        _log(f"[bridge] waited {elapsed_s:.1f}s for in-progress rotation")


def _rotate_and_retry(
    cfg_obj: Config,
    request: _Request,
    client: httpx.Client,
) -> httpx.Response:
    """Rotate on FreeUsageLimitError; wait on other 429s.

    Retries up to ``cfg.bridge_retry_attempts`` times before giving up.
    """
    t0 = time.monotonic()
    resp = _fetch(client, cfg_obj, request)
    statuses = cfg_obj.bridge_retry_statuses
    attempts = cfg_obj.bridge_retry_attempts

    for _ in range(attempts):
        if resp.status_code not in statuses:
            break

        if resp.status_code == 429 and _is_usage_limit_error(resp):
            rotated = False
            with contextlib.suppress(SystemExit):
                rotated = cascade.rotate()
            _log_rotation(rotated, time.monotonic() - t0)
            t0 = time.monotonic()
            resp.close()
            resp = _fetch(client, cfg_obj, request)
        else:
            delay = _retry_delay(resp)
            _log(f"[bridge] rate limited, waiting {delay:.1f}s")
            resp.close()
            time.sleep(delay)
            resp = _fetch(client, cfg_obj, request)

    return resp


def forward(
    cfg_obj: Config,
    request: _Request,
    *,
    rotate_on_429: bool = True,
) -> tuple[httpx.Response, httpx.Client]:
    """Send one request through the cascade, rotating on FreeUsageLimitError.

    Returns ``(response, client)`` where ``client`` is the shared singleton —
    callers must **not** close it.  The response is closed internally by
    ``_respond`` after the body has been streamed to the connected client.
    """
    client = _get_client(cfg_obj)
    if rotate_on_429 and cfg_obj.bridge_retry_attempts > 0:
        resp = _rotate_and_retry(cfg_obj, request, client)
    else:
        resp = _fetch(client, cfg_obj, request)
    return resp, client


def _respond(handler: BaseHTTPRequestHandler, resp: httpx.Response) -> int:
    """Write an upstream response back to the connected client, streaming the body.

    Bytes pass through verbatim: with ``Accept-Encoding: identity`` upstream the
    body matches its headers, so everything is forwarded as-is. Only when a
    server ignores identity and still sends a content-encoding does httpx
    decompress — then the stale encoding/length headers are dropped and the
    decoded body is sent chunked, so the client never sees mismatched framing.

    Returns the number of body bytes actually written (chunk framing excluded)
    so the request-summary log line reflects the real output size even for
    chunked/streamed responses, where ``content-length`` is absent.
    """
    encodings = list(resp.headers.get_list("content-encoding", split_commas=True))
    normalized = {enc.lower().strip() for enc in encodings}
    decoded = bool(normalized.intersection({"gzip", "deflate", "br", "zstd"}))
    sent = 0
    try:
        handler.send_response(resp.status_code)
        for key, value in resp.headers.items():
            if key.lower() in _RESPONSE_STRIP:
                continue
            if decoded and key.lower() in {"content-encoding", "content-length"}:
                continue
            with contextlib.suppress(ValueError, OSError):
                handler.send_header(key, value)
        if decoded or not resp.headers.get("content-length"):
            handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        if decoded or not resp.headers.get("content-length"):
            for chunk in resp.iter_bytes():
                handler.wfile.write(f"{len(chunk):x}\r\n".encode())
                handler.wfile.write(chunk)
                handler.wfile.write(b"\r\n")
                sent += len(chunk)
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        else:
            for chunk in resp.iter_bytes():
                handler.wfile.write(chunk)
                sent += len(chunk)
                handler.wfile.flush()
    except BrokenPipeError, ConnectionResetError, OSError:
        pass
    finally:
        resp.close()
    return sent


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
        f"[bridge] SECURITY: bridge binds to {host!r}, reachable from other machines. "
        "It forwards your Authorization headers, so keep it on 127.0.0.1 "
        "(the default). Open ports to the outside world only if you truly "
        "intend to (Docker port publish / OPENROT_LISTEN)."
    )


def base_url(cfg_obj: cfg.Config) -> str:
    """Return the loopback baseURL opencode should target for the bridge."""
    return f"http://127.0.0.1:{cfg_obj.bridge_port}/v1"


def running(cfg_obj: cfg.Config) -> bool:
    """Return True when the loopback bridge is currently listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", cfg_obj.bridge_port)) == 0


def serve() -> None:
    """Start the cascade and run the bridge server in the foreground.

    Standalone bridge for testing (``openrot start bridge``): start it, then
    point any OpenAI-compatible client at the loopback URL and watch it route
    through the cascade with 429 self-rotation. Ctrl-C stops it.
    """
    cfg_obj = cfg.load_config()
    host = cfg.listen_address()
    if port_in_use(host, cfg_obj.bridge_port):
        console.print(
            f"[red]bridge port {host}:{cfg_obj.bridge_port} is already in use; "
            "not starting[/red]"
        )
        raise SystemExit(1)
    if not (
        cfg_obj.active_level != ActiveLevel.NONE and cascade.level_serving(cfg_obj)
    ):
        console.print("no active level, starting cascade...")
        cascade.start(False, False)
        cfg_obj = cfg.load_config()
    cascade.background()

    url = base_url(cfg_obj)
    console.print(
        f"bridge: listening on {url} "
        f"(upstream {cfg_obj.bridge_upstream}, level {cfg_obj.active_level.value})"
    )
    if sys.stdout.isatty():
        console.print("[dim]Ctrl-C to stop.[/dim]")
    host = cfg.listen_address()
    warn_if_exposed(host)
    signals.keyboard_on_sigterm()
    server = Bridge(host, cfg_obj.bridge_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("[dim]stopping bridge...[/dim]")
    finally:
        server.server_close()
        _reset_client()


def daemonize() -> None:
    """Fork the bridge server into a persistent background daemon."""
    daemon.start(
        name="bridge",
        pid_path=cfg.BRIDGE_PID_PATH,
    )


def stop_daemon() -> bool:
    """Terminate a background bridge daemon if it is running."""
    return daemon.stop(cfg.BRIDGE_PID_PATH)


class BridgeHandler(BaseHTTPRequestHandler):
    """Forward every request to the configured upstream through the cascade."""

    server_version = "openrot-bridge/1"
    protocol_version = "HTTP/1.1"

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

    def log_message(self, format: str, *args: object) -> None:
        """No-op: request logging handled by _log_request_console."""

    def _log_request_console(self, method: str) -> None:
        elapsed_s = self._elapsed_ms / 1000
        parts = [f"{method} {self.path}"]
        model = getattr(self, "_model", "")
        if model:
            parts.append(model)
        prompt_chars = getattr(self, "_prompt_chars", 0)
        if prompt_chars:
            parts.append(
                f"input={prompt_chars / 1000:.1f}k"
                if prompt_chars >= 1000
                else f"input={prompt_chars}"
            )
        output_chars = getattr(self, "_output_chars", 0)
        if output_chars:
            parts.append(
                f"output={output_chars / 1000:.1f}k"
                if output_chars >= 1000
                else f"output={output_chars}"
            )
        if elapsed_s >= 1:
            parts.append(f"time={elapsed_s:.1f}s")
        else:
            parts.append(f"time={self._elapsed_ms:.0f}ms")
        status = getattr(self, "_status", "")
        if status:
            parts.append(f"status={status}")
        req_id = getattr(self, "_req_id", "")
        if req_id:
            parts.append(f"req={req_id}")
        _log(f"[bridge] {' '.join(parts)}")

    def _inspect_body(self, body: bytes) -> None:
        """Record the model and prompt char count from a JSON body."""
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
        self._req_id = next(
            (v for k, v in self.headers.items() if k.lower() == "x-opencode-request"),
            "",
        ).rsplit("/", 1)[-1]

    def _fail_upstream(self, method: str, t0: float, exc: BaseException) -> None:
        """Record a failed upstream exchange and send a 502 to the client."""
        self._elapsed_ms = (time.monotonic() - t0) * 1000
        self._output_chars = 0
        self._status = 502
        self._log_request_console(method)
        _send_502(self, str(exc))

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        self._inspect_body(body)

        cfg_obj = cfg.load_config()
        if cfg_obj.bridge_inject_session:
            body = _ensure_tools(body)
        request = _Request(method, self.path, dict(self.headers.items()), body)
        if os.environ.get("OPENROT_DEBUG_REQ"):
            _debug_request(method, self.path, request.headers, request.body)
        semaphore = _get_semaphore(cfg_obj)
        t0 = time.monotonic()
        semaphore.acquire()
        try:
            _pace_start(cfg_obj)
            resp, _client = forward(cfg_obj, request)
        except httpx.HTTPError as exc:
            events.warning("[bridge] upstream error, retrying: %s", exc)
            try:
                cfg_obj = cfg.load_config()
                _pace_start(cfg_obj)
                resp, _client = forward(cfg_obj, request, rotate_on_429=False)
            except httpx.HTTPError as retry_exc:
                self._fail_upstream(method, t0, retry_exc)
                return
        finally:
            semaphore.release()
        sent = 0
        try:
            sent = _respond(self, resp)
        finally:
            self._elapsed_ms = (time.monotonic() - t0) * 1000
            self._output_chars = sent
            self._status = resp.status_code
            self._log_request_console(method)


class Bridge(ThreadingHTTPServer):
    """A threaded loopback server exposing the openrot bridge."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), BridgeHandler)
