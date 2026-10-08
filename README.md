# openrot

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen)
![Coverage](https://img.shields.io/badge/Coverage-%3E75%25-brightgreen)
![Built for opencode](https://img.shields.io/badge/Built%20for-opencode-7c3aed)

**English** · [Русский](README.ru.md)

Local WARP bridge. openrot keeps a Cloudflare WARP connection alive and serves
a loopback HTTP bridge that forwards opencode traffic through it. One config
file, one moving part: WARP.

> Huge thanks to opencode for existing. This project is built around it:
> openrot serves a loopback bridge so that opencode (or any OpenAI-compatible
> client) keeps working on the free `opencode/*` providers, with automatic
> 429 self-rotation.

```
opencode ──HTTP──▶ openrot bridge (127.0.0.1:7891)
                        │
                        ▼
               WARP SOCKS5 (127.0.0.1:40000)
                        │
                        ▼
               bridge_upstream (https://opencode.ai/zen)
```

## Why

The free opencode providers rate-limit by session and IP. openrot keeps you on
a WARP connection and puts a local bridge in front of opencode that rotates the
WARP IP the moment the upstream answers `429` — long sessions keep going
without manual intervention. Nothing is hosted server-side; everything runs on
your machine.

## Quick install

**Standalone binary** (macOS / Linux) — downloads the prebuilt release, installs
the `warp-cli` prerequisite and brings WARP up:

```bash
curl -fsSL https://raw.githubusercontent.com/vispar-tech/openrot/main/install.sh | bash
```

**Docker** (WARP still runs on the host) — installs prerequisites and runs the
image from GHCR:

```bash
curl -fsSL https://raw.githubusercontent.com/vispar-tech/openrot/main/install-docker.sh | bash
```

> The first method installs the standalone binary to `~/.local/lib/openrot`
> (symlink in `~/.local/bin/openrot`).
> If your distro already has warp-cli, you can skip starting it:
> `OPENROT_SKIP_WARP=1 ... | bash`.

## Requirements

- **warp-cli** (Cloudflare WARP client) — required at runtime; it runs on the
  host, not inside the container:
  - macOS: `brew install --cask cloudflare-warp`
  - Linux: [Cloudflare's package repo](https://pkg.cloudflareclient.com/)
  - `openrot warp install` prints the same instructions
- Python 3.14+ and [Poetry](https://python-poetry.org) — only when running
  from source

## Install (from source)

```bash
poetry install
poetry run openrot --help
```

## Quick start

```bash
# 1. connect WARP (once; warp-cli must be installed)
poetry run openrot warp on

# 2. start the local bridge (127.0.0.1:7891); Ctrl-C stops it
poetry run openrot start bridge
# ...or run it as a background daemon
poetry run openrot start bridge --daemon

# 3. status / rotate / stop
poetry run openrot status
poetry run openrot rotate
poetry run openrot stop
```

## Commands

| Command | Description |
| --- | --- |
| `openrot start bridge [--daemon]` | Start the opencode bridge (foreground, or background with `--daemon`) |
| `openrot stop [-y\|--yes]` | Disconnect WARP and stop the bridge daemon |
| `openrot status [--json] [-v\|--verbose]` | Show WARP and bridge status (--verbose = full diagnostics table) |
| `openrot rotate` | Rotate the WARP IP (reconnects and verifies the address actually changed) |
| `openrot warp on\|off\|install` | Connect / disconnect WARP; `install` prints warp-cli install instructions |
| `openrot warp status [--json]` | Show WARP status and egress IP |
| `openrot logs [-n N] [--follow/--no-follow]` | Tail the openrot log file (default: follow) |
| `openrot run -- <cmd>` | Run a command with proxy env vars pointing at WARP (starts WARP if needed) |
| `openrot config` | Open the config file in $EDITOR (default vim) |
| `openrot provider [--action on\|off\|auto] [--config PATH]` | Toggle the opencode provider in opencode.jsonc |
| `openrot self-update [-y\|--yes]` | Update the openrot binary to the latest release |
| `openrot --version` | Print the version and exit |

### `warp off` is transient

`openrot warp off` disconnects WARP now, and `openrot status` reports
disconnected — but while the bridge is running, its health loop reconnects on
the next check: within `health_interval` (default 30s). With no bridge running,
nothing reconnects it. `openrot stop` disconnects WARP *and* stops the bridge
daemon, so the stack stays down.

## opencode (bridge)

opencode talks to openrot through the loopback **bridge** — no launcher, no
config magic. Bring the bridge up, then point opencode at it by merging one key
into its config:

```bash
openrot start bridge           # WARP + bridge, foreground; Ctrl-C to stop
openrot start bridge --daemon  # or run it as a persistent background daemon
```

Merge this into `~/.config/opencode/opencode.json`. The override is scoped to
opencode's built-in `opencode` provider — the one serving the default free
`opencode/*` models — so those models route through the bridge with 429
self-rotation, and nothing else changes:

```json
{ "provider": { "opencode": { "options": { "baseURL": "http://127.0.0.1:7891/v1" } } } }
```

`bridge_port` defaults to `7891`. opencode only ever sees plain loopback HTTP —
no TLS, no proxy env vars, nothing to restore; the bridge itself opens the
upstream leg over TLS through WARP. A background bridge daemon is stopped with
`openrot stop`; its logs go to `~/.config/openrot/openrot.log` (see
`openrot logs`).

```bash
openrot status           # bridge: running (http://127.0.0.1:7891/v1)
curl -s http://127.0.0.1:7891/v1/models
```

## Bridge (429 self-rotation)

A proxy tunnel routes opencode's traffic opaquely, so it cannot see the
provider returning an HTTP `429` (the request is already inside a TLS CONNECT
tunnel). The local **bridge** is a loopback OpenAI-compatible reverse proxy
(`baseURL`) that forwards each request *through the WARP SOCKS5 connection* to
`bridge_upstream` and watches the upstream status. On a `429` whose body is a
`FreeUsageLimitError` it rotates the WARP IP (`openrot rotate`) and retries the
request; other configured retry statuses wait out `Retry-After` first. Retries
are bounded by `bridge_retry_attempts`.

The upstream base and port are configurable — see `bridge_port` (default
`7891`), `bridge_upstream` (default `https://opencode.ai/zen`; the incoming
request path, e.g. `/v1/chat/completions`, is appended to it), or the env
overrides `OPENROT_BRIDGE_PORT` and `OPENROT_UPSTREAM`.

### Free-tier session headers

opencode's free tier keys on the `X-Opencode-Session` header: a bare request
that lacks it lands in the anonymous quota and can be rejected with
`FreeUsageLimitError`. When `bridge_inject_session` is on (the default), the
bridge adds, on every upstream request that doesn't already carry them:

- `X-Opencode-Session` — `ses_` + 26 digits
- `X-Opencode-Request` — `msg_` + 24 alnum
- an opencode `User-Agent` (a non-opencode one is replaced)
- stub entries in the `tools` array of chat bodies (the free gateway requires
  at least two)

Values sent by the client are respected (never overwritten). Toggle this off
with `bridge_inject_session: false` in config or
`OPENROT_BRIDGE_INJECT_SESSION=0`:

```bash
# via the bridge: headers are added automatically
curl -s http://127.0.0.1:7891/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"model":"opencode/sonnet","messages":[{"role":"user","content":"hi"}]}'

# hitting the upstream directly: add the header yourself
curl -s https://opencode.ai/zen/v1/chat/completions \
  -H 'X-Opencode-Session: ses_<26 digits>' \
  -H 'Content-Type: application/json' -d '{"model":"opencode/sonnet","messages":[{"role":"user","content":"hi"}]}'
```

## Config

Stored in `~/.config/openrot/config.yaml`.
Overridden by `OPENROT_DIR` (base dir) and `OPENROT_CONFIG` (full config path).

```yaml
health_interval: 30      # seconds between WARP health checks (health loop)
active_level: none       # none | warp (written by start/stop)
bridge_port: 7891        # loopback port for the opencode bridge
bridge_upstream: "https://opencode.ai/zen"  # request path is appended to this base
bridge_retry_statuses: [429]   # upstream statuses that trigger a retry
bridge_retry_attempts: 1       # max retries per request
bridge_max_concurrent: 3       # max parallel upstream requests
bridge_min_interval: 0.5       # min seconds between upstream request starts
bridge_inject_session: true    # inject free-tier session headers / UA / tools
```

### Environment overrides

| Variable | Effect |
| --- | --- |
| `OPENROT_DIR` | base directory (default `~/.config/openrot`) |
| `OPENROT_CONFIG` | full path of the config file |
| `OPENROT_LISTEN` | bridge bind address (default `127.0.0.1`; use e.g. `0.0.0.0` in Docker) |
| `OPENROT_BRIDGE_PORT` | → `bridge_port` |
| `OPENROT_UPSTREAM` | → `bridge_upstream` |
| `OPENROT_BRIDGE_RETRY_STATUSES` | → `bridge_retry_statuses` (comma-separated ints) |
| `OPENROT_BRIDGE_RETRY_ATTEMPTS` | → `bridge_retry_attempts` |
| `OPENROT_BRIDGE_MAX_CONCURRENT` | → `bridge_max_concurrent` |
| `OPENROT_BRIDGE_MIN_INTERVAL` | → `bridge_min_interval` |
| `OPENROT_BRIDGE_INJECT_SESSION` | → `bridge_inject_session` (bool) |
| `OPENROT_WARP` | warp-cli binary name/path (default `warp-cli`) |
| `OPENROT_WARP_HOST` / `OPENROT_WARP_PORT` | WARP SOCKS5 address (default `127.0.0.1:40000`) |
| `OPENROT_WARP_ROTATE_DELAY` / `OPENROT_WARP_ROTATE_ATTEMPTS` | rotate pacing: seconds between attempts / max attempts (defaults `10` / `5`) |
| `OPENROT_DEBUG_REQ` | verbose per-request bridge logging (secrets in auth headers are masked) |

Runtime paths, all under the base directory: `config.yaml`,
`openrot.log`, `openrot-bridge.daemon.pid`.

## Install (standalone / Docker)

Both installers auto-install `warp-cli` and bring WARP up on the host in proxy
mode.

**Standalone binary** (PyInstaller build, no Python needed):

```bash
./install.sh        # from a checkout
make installer      # equivalent
```

Environment overrides: `OPENROT_BIN_URL`, `OPENROT_VERSION`, `OPENROT_PREFIX`,
`OPENROT_SKIP_WARP`. On macOS you'll be prompted to grant WARP a system
extension / VPN permission the first time.

**Docker** (not auto-installed; the script prints instructions):

```bash
./install-docker.sh
make install-docker # equivalent
```

Environment overrides: `OPENROT_IMAGE`, `OPENROT_BRIDGE_PORT`,
`OPENROT_CONFIG_DIR`, `OPENROT_WARP_HOST`, `NETWORK`.

## Docker

**WARP runs on the host, not in the container.** `warp-cli` needs a TUN device
and the OS network stack, so it can't tunnel inside Docker. The container
reaches the host-side WARP through its SOCKS5 proxy; the image runs the bridge
(bridge-only — that is all openrot runs).

### Prerequisite: run WARP on the host first

Before `make docker-run`, start WARP once on the host in proxy mode:

```bash
poetry run openrot warp on
```

### Makefile (recommended for dev)

```bash
make docker-run              # builds image and runs openrot, wired to the host WARP
make docker-sh               # interactive shell in the image (same config mount)
```

By default the container uses bridge networking and points at the host's WARP via
`OPENROT_WARP_HOST=host.docker.internal` (host port 40000):

- macOS (Docker Desktop): works out of the box.
- Linux: the Makefile adds `--add-host host.docker.internal:host-gateway`.
- On Linux you can instead run `NETWORK=host make docker-run` to share the host
  loopback, so WARP answers at `127.0.0.1:40000` directly.

### Manual run

```bash
docker build -t openrot .
docker run --rm -it \
  -p 7891:7891 \
  --add-host host.docker.internal:host-gateway \
  -e OPENROT_WARP_HOST=host.docker.internal \
  -v $HOME/.config/openrot:/root/.config/openrot \
  openrot
```

### What gets overridden / passed through

- **Config and data** are shared read-write via the `-v $HOME/.config/openrot:...`
  mount, so your config is the same on the host and in the container.
  `OPENROT_DIR` is already set to `/root/.config/openrot` inside the image
  (see `Dockerfile`).
- **Port**: the bridge listens on `7891`; map a different host port with
  `-p $PORT_BRIDGE:7891`.
- **Listen address**: the bridge binds `127.0.0.1` by default; the image sets
  `OPENROT_LISTEN=0.0.0.0` so the published port is reachable from the host
  (override with `-e OPENROT_LISTEN=127.0.0.1` to lock down).
- **WARP address**: `OPENROT_WARP_HOST` (default `127.0.0.1`) and
  `OPENROT_WARP_PORT` (default `40000`) point openrot at the host-side WARP
  SOCKS5 proxy. In the container set `OPENROT_WARP_HOST` to the host.

If the host WARP is not reachable, the bridge still starts — the health loop
keeps retrying the connect every `health_interval` — and `openrot status`
reports `connectivity: FAIL` until WARP is up.

## Development

- Tests: `poetry run pytest` (or `make check` — ruff + mypy + coverage).
- Coverage standard: keep the local report **above 85 %**; the CI gate is set
  lower (75 %) as a safety floor.

## Troubleshooting

**`status` shows `connectivity: FAIL (WARP not forwarding traffic)`**
Check warp-cli: `openrot warp status`. If warp-cli is missing, run
`openrot warp install` and then `openrot warp on`.

**I ran `openrot warp off`, but it reconnects by itself**
Expected while the bridge runs: the health loop reconnects within
`health_interval` (default 30s). Use `openrot stop` to shut the whole stack
down, or stop the bridge first.

**`bridge port 127.0.0.1:7891 is already in use; not starting`**
Another openrot (or a stale daemon) holds the port. Run `openrot stop`, or
change `bridge_port` / `OPENROT_BRIDGE_PORT`.

**The bridge is up but upstream requests return 502**
The upstream leg could not be fetched — usually WARP is down or unreachable.
`openrot status --verbose` prints the diagnostics table; `openrot warp on`
brings WARP back. Set `OPENROT_DEBUG_REQ=1` for per-request logs and follow
them with `openrot logs`.

**In Docker the container cannot reach WARP**
The container must point at the host: `OPENROT_WARP_HOST=host.docker.internal`
with bridge networking (the Makefile does this), or run with
`NETWORK=host make docker-run` on Linux so host-loopback WARP at
`127.0.0.1:40000` is visible directly.
