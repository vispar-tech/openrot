"""Loopback HTTP bridge that routes opencode traffic through WARP.

``openrot start bridge`` brings WARP up and serves the loopback bridge in the
foreground (Ctrl-C stops it); ``openrot start bridge --daemon`` forks it into a
persistent background process. opencode is pointed at the bridge via the manual
``provider.opencode.options.baseURL`` override in the README; ``status``
reports whether the bridge is currently listening.

Requests arrive as plain HTTP on 127.0.0.1 and are forwarded *through the
active level* (the WARP SOCKS5 proxy on ``socks5://{warp_host}:{warp_port}``) to
``bridge_upstream``. An upstream HTTP ``429`` rotates the WARP IP once
(``cascade.rotate()``) and retries the request, so the rate-limited WARP IP is
rotated out transparently. Only the upstream leg goes over
TLS (handled by ``httpx``); the client leg is plain HTTP — unlike a CONNECT
tunnel — so the 429 is visible here without any MITM.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

import httpx
from rich.console import Console

from openrot import config as cfg
from openrot import signals
from openrot.config import ActiveLevel, Config
from openrot.core import cascade, daemon
from openrot.core.http import make_client, port_in_use
from openrot.core.opencode_tools import (
    CORE_TOOL_NAMES,
    GENUINE_TOOLS,
    TITLE_SYSTEM_PROMPT,
)
from openrot.log import get_logger as _get_logger
from openrot.models.config import DEFAULT_BRIDGE_UPSTREAM
from openrot.providers import warp


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
# ``x-opencode-request`` is rejected by the gateway with a 403 (the legacy
# header), so it is dropped rather than forwarded.
_REQUEST_STRIP = _HOP_BY_HOP | {
    "transfer-encoding",
    "content-length",
    "x-opencode-request",
}

# Response leg: framing is re-created below (content-length or chunked).
_RESPONSE_STRIP = _HOP_BY_HOP | {"transfer-encoding"}

# User-Agent shape the free tier gateway expects: opencode/<channel>/<version>/<name>.
_OPENCODE_UA = "opencode/latest/2.0.29/cli"

# Identity defaults applied when the anonymous free tier client sends none.
_ANONYMOUS_AUTH = "Bearer public"
_CLIENT_NAME = "cli"
_ACCEPT = "application/json, text/event-stream"

# Salts/mask for the deterministic session and project id derivation.
_SESSION_SALT = b"ses\x00"
_PROJECT_ID_SALT = b"opencode-project\x00openrot"
_SESSION_TIME_MASK = 0xFFFFFFFFFFFF

# Session shape: "ses_" + 12 lowercase hex + 14 base62. The time part must be
# the descending-encoded timestamp (~(ts_ms<<12|counter), low 12 bits fc0-fff).
_SESSION_ID_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
_CANONICAL_TIME_PART_RE = re.compile(r"^[0-9a-f]{9}f[c-f][0-9a-f]$")
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# All four session headers must carry the same canonical id upstream.
_SESSION_HEADERS = (
    "X-Opencode-Session",
    "X-Opencode-Session-Id",
    "X-Session-Affinity",
    "X-Session-Id",
)

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


def _log_error_body(status: int, body: bytes) -> None:
    """Log the full response body for any non-200 status.

    Error bodies carry the upstream's reason (quota, validation, ...), so they
    are logged verbatim instead of being summarized like the request line.
    """
    if status == 200:
        return
    events.warning(
        "[bridge] status=%s body: %s", status, body.decode("utf-8", "replace")
    )


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
        tool_name = _chat_tool_name(tool)
        if tool_name is not None:
            tool_names.append(tool_name)
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


def _debug_dump(kind: str, body: bytes) -> None:
    """Persist a raw request body to ``<config dir>/debug`` for offline diffing.

    Opt-in via ``OPENROT_DEBUG_REQ=1``; the summary log above shows names only,
    which is not enough to reproduce an upstream 403.
    """
    if not os.environ.get("OPENROT_DEBUG_REQ"):
        return
    try:
        dump_dir = cfg.LOG_PATH.parent / "debug"
        dump_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{secrets.token_hex(3)}.json"
        (dump_dir / name).write_bytes(body)
    except OSError as exc:
        events.warning("[bridge][debug] dump failed: %s", exc)


def _error_payload(message: str) -> bytes:
    """Build the JSON body for a bridge-generated upstream error."""
    return json.dumps({"error": {"message": message, "type": "upstream"}}).encode()


def _send_502(handler: BaseHTTPRequestHandler, message: str) -> None:
    """Write a JSON 502 response to the connected client."""
    payload = _error_payload(message)
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
    w_host, w_port = warp.proxy_address()
    proxy_url = f"socks5://{w_host}:{w_port}"
    return make_client(proxy=proxy_url, timeout=300)


def _client_project_id() -> str:
    """Return the bare 40-hex project header value the gateway expects."""
    return hashlib.sha256(_PROJECT_ID_SALT).hexdigest()[:40]


def _canonical_session_id(raw_session: str) -> str:
    """Return a session id in the descending layout the free tier accepts.

    A raw session that already is an official ``ses_`` session with a
    descending time part is passed through unchanged (prompt-cache affinity).
    Anything else is deterministically re-encoded: sha256 -> 48-bit inverted
    timestamp with counter 1 (low 12 hex digits ``ffe``) + 14 base62 chars.
    """
    if _SESSION_ID_RE.match(raw_session) and _CANONICAL_TIME_PART_RE.match(
        raw_session[4:16]
    ):
        return raw_session
    digest = hashlib.sha256(_SESSION_SALT + raw_session.encode()).digest()
    # 36-bit timestamp fragment from 5 bytes, shifted into a 48-bit word:
    # digest[0..3] supply 32 bits and only the top nibble of digest[4] (>> 4)
    # supplies the last 4 — the low nibble is dropped so the fragment stays
    # exactly 36 bits, letting the << 12 reserve the low 12 bits for the
    # counter. digest[5] is skipped so the time part and the base62 tail come
    # from disjoint regions: the tail takes bytes 6..16 (80 bits), which always
    # fit into the 14 base62 chars (62^14 > 2^80) with no truncation.
    # `|= 1` fixes the low 12 counter bits to 1, so the inverted time part
    # always ends in the fc0-fff range the layout requires.
    current = (
        digest[0] << 28
        | digest[1] << 20
        | digest[2] << 12
        | digest[3] << 4
        | digest[4] >> 4
    ) << 12
    current |= 1
    inverted = (~current) & _SESSION_TIME_MASK
    time_part = f"{inverted:012x}"
    n = int.from_bytes(digest[6:16], "big")
    out: list[str] = []
    for _ in range(14):
        n, rem = divmod(n, 62)
        out.append(_BASE62[rem])
    return "ses_" + time_part + "".join(reversed(out))


def _client_session_seed(headers: dict[str, str]) -> str:
    """Return the first session-identifying header value the client sent.

    When the client sends no session header the fallback is random per
    request, so each request gets a fresh canonical session (no prompt-cache
    affinity for bare requests).
    """
    lower = {k.lower(): v for k, v in headers.items()}
    seed = next(
        (
            lower[name]
            for name in (
                "x-opencode-session",
                "x-opencode-session-id",
                "x-session-affinity",
                "x-session-id",
                "conversation-id",
            )
            if lower.get(name)
        ),
        None,
    )
    return seed if seed is not None else secrets.token_hex(8)


def _force_streaming(data: dict[str, object]) -> bool:
    """Force ``stream`` and ``stream_options.include_usage`` on. True if changed."""
    changed = False
    if data.get("stream") is not True:
        data["stream"] = True
        changed = True
    options = data.get("stream_options")
    if not isinstance(options, dict):
        data["stream_options"] = {"include_usage": True}
        changed = True
    elif options.get("include_usage") is not True:
        options["include_usage"] = True
        changed = True
    return changed


def _chat_tool_name(item: object) -> str | None:
    """Return the function name of a Chat tools-array item, if any."""
    if not isinstance(item, dict):
        return None
    fn = item.get("function")
    name = fn.get("name") if isinstance(fn, dict) else None
    return name if isinstance(name, str) and name else None


def _normalize_tools(tools: list[object]) -> tuple[list[object], bool]:
    """Replace recognised stubs with genuine defs, append missing core tools.

    Unknown tool names are left exactly as the client declared them.
    """
    present: set[str] = set()
    out = list(tools)
    changed = False
    for index, item in enumerate(out):
        name = _chat_tool_name(item)
        if name is None:
            continue
        present.add(name)
        genuine = GENUINE_TOOLS.get(name)
        if genuine is not None and item != genuine:
            out[index] = genuine
            changed = True
    missing = [name for name in CORE_TOOL_NAMES if name not in present]
    if missing:
        out = out + [GENUINE_TOOLS[name] for name in missing]
        changed = True
    return out, changed


def _normalize_title_prompt(data: dict[str, object]) -> bool:
    """Swap opencode's title-generation system prompt for a reworded one.

    opencode fires a title request at the start of every new chat, carrying the
    exact prompt shipped in its client. The gateway fingerprints that prompt —
    no single line trips the check, only its ``<task>``/``<rules>``/
    ``<examples>`` markers together — and answers 403 FreeTierError, so the
    first request of every new chat fails while the real one right after it
    succeeds. A reworded equivalent that still produces a good short title is
    accepted, so it is swapped in; anything else is left untouched.
    """
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if "title generator" not in content[:80].lower():
            continue
        if content != TITLE_SYSTEM_PROMPT:
            message["content"] = TITLE_SYSTEM_PROMPT
            return True
    return False


def _normalize_body(body: bytes) -> bytes:
    """Normalize a chat body for the free tier: streaming + genuine tools.

    - ``stream`` is forced on and ``stream_options.include_usage`` is set
      (the SSE path reports usage only with it);
    - recognised tool names are replaced with their genuine definitions
      (the gateway rejects stubs that merely reuse a recognised name);
    - missing core tools (shell/edit/glob/grep/read) are appended;
    - opencode's title-generation system prompt is swapped for a reworded
      equivalent (the gateway fingerprints the original and answers 403);
    - unknown tool names are left exactly as the client declared them.

    Bodies that already satisfy everything are returned byte-identical.
    When any change is made the body is re-serialized with ``json.dumps``
    defaults (whitespace normalized, non-ASCII escaped), so byte-identical
    pass-through is only promised for already-compliant bodies.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError, AttributeError, ValueError:
        return body
    if not isinstance(data, dict):
        return body
    changed = _force_streaming(data)
    changed = _normalize_title_prompt(data) or changed
    tools = data.get("tools")
    if not isinstance(tools, list):
        tools = []
    data["tools"], tools_changed = _normalize_tools(tools)
    changed = changed or tools_changed
    if not changed:
        return body
    return json.dumps(data).encode()


def _strip_unforwardable_headers(headers: dict[str, str]) -> dict[str, str]:
    """Copy headers, dropping unforwardable ones and case-duplicate names.

    ``x-opencode-request`` (legacy) and hop-by-hop/framing headers are
    stripped; when two incoming names differ only by case, the first
    occurrence wins and later duplicates are dropped.
    """
    out: dict[str, str] = {}
    seen: set[str] = set()
    for key, value in headers.items():
        lower = key.lower()
        if lower in _REQUEST_STRIP or lower in seen:
            continue
        seen.add(lower)
        out[key] = value
    return out


def _apply_session_headers(
    out: dict[str, str], known: dict[str, str], session: str
) -> None:
    """Set the four session headers to one canonical value in ``out``."""
    for name in _SESSION_HEADERS:
        existing = known.get(name.lower())
        if existing is not None:
            del out[existing]
        out[name] = session


def _ensure_user_agent(out: dict[str, str], known: dict[str, str]) -> None:
    """Ensure an opencode User-Agent, replacing a non-opencode one."""
    ua = known.get("user-agent")
    if ua is None or not out[ua].startswith("opencode/"):
        if ua is not None:
            del out[ua]
        out["User-Agent"] = _OPENCODE_UA


def _forward_headers(headers: dict[str, str], host: str) -> dict[str, str]:
    """Rebuild the upstream header set to mirror the official client.

    Missing identity headers are added, already-canonical values are kept,
    and legacy ones (``x-opencode-request``) are dropped.
    """
    out = _strip_unforwardable_headers(headers)
    known = {k.lower(): k for k in out}
    _apply_session_headers(
        out, known, _canonical_session_id(_client_session_seed(headers))
    )
    # Recompute the map: the session rewrite mutated ``out`` above.
    known = {k.lower(): k for k in out}
    if "x-opencode-project" not in known:
        out["X-Opencode-Project"] = _client_project_id()
    _ensure_user_agent(out, known)
    if "x-opencode-client" not in known:
        out["X-Opencode-Client"] = _CLIENT_NAME
    if "authorization" not in known:
        out["Authorization"] = _ANONYMOUS_AUTH
    if "accept" not in known:
        out["Accept"] = _ACCEPT
    if "content-type" not in known:
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
    # Ask for identity so httpx neither injects its own Accept-Encoding nor
    # transparently decompresses: the body is then passed through verbatim.
    out_headers["Accept-Encoding"] = "identity"
    if os.environ.get("OPENROT_DEBUG_REQ"):
        _debug_request("OUT", url, out_headers, request.body)
        _debug_dump("out", request.body)
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
        # The upstream leg is streamed (``_fetch`` uses ``stream=True``), so the
        # body is unreadable until ``read()`` is called — otherwise httpx raises
        # ResponseNotRead from ``resp.text``. Only 429 payloads get buffered
        # here, and they are small error JSON; the 200 path still streams.
        resp.read()
        data = json.loads(resp.text)
        error = data.get("error", {})
        return isinstance(error, dict) and error.get("type") == "FreeUsageLimitError"
    except json.JSONDecodeError, AttributeError, httpx.HTTPError, RuntimeError:
        # Best effort: any read/parse failure means "not a usage limit", which
        # only costs the slower plain-retry path instead of a 502 to the client.
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


def _write_body(
    handler: BaseHTTPRequestHandler,
    resp: httpx.Response,
    *,
    chunked: bool,
    error_body: bytearray | None,
) -> int:
    """Stream the response body to the client, returning bytes written.

    When ``error_body`` is given, each chunk is also accumulated into it so a
    non-200 body can be logged in full after the stream ends. A client that
    disconnects mid-stream stops the copy but keeps the bytes written so far.
    """
    sent = 0
    try:
        for chunk in resp.iter_bytes():
            if chunked:
                handler.wfile.write(f"{len(chunk):x}\r\n".encode())
                handler.wfile.write(chunk)
                handler.wfile.write(b"\r\n")
            else:
                handler.wfile.write(chunk)
            sent += len(chunk)
            if error_body is not None:
                error_body.extend(chunk)
            handler.wfile.flush()
        if chunked:
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
    except BrokenPipeError, ConnectionResetError, OSError:
        pass
    return sent


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
    chunked = decoded or not resp.headers.get("content-length")
    sent = 0
    # Non-200 bodies are small error payloads; keep them to log in full.
    error_body = bytearray() if resp.status_code != 200 else None
    try:
        handler.send_response(resp.status_code)
        for key, value in resp.headers.items():
            if key.lower() in _RESPONSE_STRIP:
                continue
            if decoded and key.lower() in {"content-encoding", "content-length"}:
                continue
            with contextlib.suppress(ValueError, OSError):
                handler.send_header(key, value)
        if chunked:
            handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        sent = _write_body(handler, resp, chunked=chunked, error_body=error_body)
    except BrokenPipeError, ConnectionResetError, OSError:
        pass
    finally:
        resp.close()
    if error_body is not None:
        _log_error_body(resp.status_code, bytes(error_body))
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
    """Start WARP and run the bridge server in the foreground.

    Standalone bridge for testing (``openrot start bridge``): start it, then
    point any OpenAI-compatible client at the loopback URL and watch it route
    through WARP with 429 self-rotation. Ctrl-C stops it.
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
        console.print("no active level, connecting warp...")
        if not cascade.start():
            console.print(
                "[yellow]warp unavailable; the health loop will keep retrying[/yellow]"
            )
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
        ses_id = getattr(self, "_ses_id", "")
        if ses_id:
            parts.append(f"ses={ses_id}")
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
        self._ses_id = next(
            (v for k, v in self.headers.items() if k.lower() == "x-opencode-session"),
            "",
        ).rsplit("/", 1)[-1]

    def _fail_upstream(self, method: str, t0: float, exc: BaseException) -> None:
        """Record a failed upstream exchange and send a 502 to the client."""
        self._elapsed_ms = (time.monotonic() - t0) * 1000
        self._output_chars = 0
        self._status = 502
        self._log_request_console(method)
        _log_error_body(502, _error_payload(str(exc)))
        _send_502(self, str(exc))

    def _handle(self, method: str) -> None:
        """Entry point: log unexpected errors instead of losing them to stderr.

        ``socketserver`` routes handler exceptions to ``handle_error``; without
        this wrapper the client never gets a response and simply hangs.
        """
        try:
            self._dispatch(method)
        except Exception as exc:
            self._status = 502
            self._output_chars = 0
            self._elapsed_ms = getattr(self, "_elapsed_ms", 0)
            _log(
                f"[bridge] unhandled {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}"
            )
            with contextlib.suppress(Exception):
                self._log_request_console(method)
            with contextlib.suppress(OSError):
                _send_502(self, str(exc))

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        self._inspect_body(body)

        cfg_obj = cfg.load_config()
        # Body normalization is chat-specific: only /chat/completions is rewritten,
        # any other path passes the body through verbatim.
        if self.path.endswith("/chat/completions"):
            body = _normalize_body(body)
        request = _Request(method, self.path, dict(self.headers.items()), body)
        if os.environ.get("OPENROT_DEBUG_REQ"):
            _debug_request(method, self.path, request.headers, request.body)
            _debug_dump("in", request.body)
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

    def handle_error(self, request: object, client_address: object) -> None:
        """Log handler tracebacks via the events logger (daemon stderr is /dev/null)."""
        _log(f"[bridge] handler error:\n{traceback.format_exc()}")

    def __init__(self, host: str, port: int) -> None:
        super().__init__((host, port), BridgeHandler)
