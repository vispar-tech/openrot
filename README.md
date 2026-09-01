# openrot

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-brightgreen)
![Coverage](https://img.shields.io/badge/Coverage-%3E75%25-brightgreen)
![Built for opencode](https://img.shields.io/badge/Built%20for-opencode-7c3aed)

**English** · [Русский](README.ru.md)

Local proxy rotator. One config file defines **profiles** (sources of free
nodes) and their **nodes**. Traffic starts from **Cloudflare WARP** (top
priority, not a node), then follows the chain down: profiles by profile
priority, nodes by node priority, to the first alive node. When one dies,
openrot rotates to the next.

> Huge thanks to opencode for existing. This project is built around it:
> openrot keeps a cascade of free nodes alive and serves a loopback bridge so
> that opencode (or any OpenAI-compatible client) keeps working on free
> providers, with auto-rotation.

```
WARP (enabled by default)
   │  warp_enabled: true, host-only (skipped in Docker)
   ▼
Profile A (priority 0) ── node 0, node 1, node 2   ◀─ choose first alive
Profile B (priority 1) ── node 0, node 1
...
```

## Why

Public V2Ray configs and proxy lists die constantly. You add several sources as
profiles, and openrot keeps them alive: it fetches them on a schedule,
health-checks every node, picks the best one, and rotates when a node or WARP
goes down.

## Quick install

**Standalone binary** (macOS / Linux) — downloads the prebuilt release, installs
the `warp-cli` + sing-box prerequisites and brings WARP up:

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
> If your distro already has the prerequisites, you can skip them:
> `OPENROT_SKIP_WARP=1 ... | bash`.

## Requirements

- Python 3.14+, [Poetry](https://python-poetry.org)
- **sing-box** — external binary, NOT a Python dependency:
  - macOS: `brew install sing-box`
  - [GitHub Releases](https://github.com/SagerNet/sing-box/releases)
- **warp-cli** (Cloudflare WARP client) — optional, host-only, unavailable in Docker

## Install

```bash
poetry install
poetry run openrot --help
```

## Quick start

```bash
# 1. add a source profile (a repo of vless:// links or free proxies)
poetry run openrot profile add gitlab https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/WHITE-CIDR-RU-all.txt --kind relay --priority 0

# 2. fetch its nodes
poetry run openrot update --relay

# 3. health-check everything
poetry run openrot test

# 4. start the local proxy (listens on 127.0.0.1:7890)
poetry run openrot start cascade
# in another terminal:
curl -x http://127.0.0.1:7890 https://ifconfig.me

# 5. status / rotate / stop
poetry run openrot status
poetry run openrot rotate
poetry run openrot stop
```

## Commands

| Command | Description |
| --- | --- |
| `openrot profile add NAME URL --kind relay\|proxy [--priority N] [--interval S] [--disabled]` | Register a source profile |
| `openrot profile list [--json]` | List profiles |
| `openrot profile set NAME [--priority N] [--interval S] [--enabled\|--disabled]` | Update a profile's priority, refresh interval, state |
| `openrot profile remove NAME` | Remove a profile and its nodes |
| `openrot list [--alive] [--json]` | List nodes across profiles |
| `openrot test [--json]` | Health-check all nodes |
| `openrot update [--relay] [--proxy] [--name X]` | Fetch nodes for all enabled profiles (default; filter with flags) |
| `openrot start cascade\|bridge [--daemon]` | Start the cascade (foreground = logs stream to the terminal; `--daemon` = background) or the `bridge` 429-rotation server (foreground, or `--daemon` as a background process) |
| `openrot stop [-y]` | Stop the running stack: WARP or local proxy, plus any bridge daemon |
| `openrot status [--json] [-v\|--verbose]` | Show active level and connectivity (--verbose = full stack diagnostics) |
| `openrot rotate` | Rotate WARP IP or select the next node |
| `openrot logs [-n N] [--follow/--no-follow]` | Tail daemon, events and bridge logs in one stream |
| `openrot config` | Open the config file in $EDITOR (default vim) |
| `openrot warp on\|off\|install` | Enable/connect or disable/disconnect WARP |
| `openrot warp status [--json]` | Show WARP status |
| `openrot run -- <cmd>` | Run a command with the proxy env vars set |
| `openrot probe <url>` | Request `url` through the active stack, showing each stage |

## opencode (bridge)

opencode talks to openrot through the loopback **bridge** — no launcher, no
config magic. Bring the bridge up, then point opencode at it by merging one key
into its config:

```bash
openrot start bridge           # cascade + bridge, foreground; Ctrl-C to stop
openrot start bridge --daemon  # or run it as a persistent background daemon
```

Merge this into `~/.config/opencode/opencode.json`. The override is scoped to
opencode's built-in `opencode` provider — the one serving the default free
`opencode/*` models — so those models route through the bridge with 429
self-rotation, and nothing else changes:

```json
{ "provider": { "opencode": { "options": { "baseURL": "http://127.0.0.1:7891/v1" } } } }
```

`<bridge_port>` defaults to `7891` (config `bridge_port`). opencode only ever
sees plain loopback HTTP — no TLS, no proxy env vars, nothing to restore; the
bridge itself terminates the upstream leg over TLS through the cascade. A
background bridge daemon is stopped with `openrot stop` (log:
`~/.config/openrot/openrot-bridge.log`).

```bash
openrot status           # bridge: running (http://127.0.0.1:7891/v1)
curl -s http://127.0.0.1:7891/v1/models
```

## Bridge (429 self-rotation)

The proxy tunnel routes opencode's traffic opaquely, so it cannot see the
provider returning an HTTP `429` (the request is already inside a TLS CONNECT
tunnel). To rotate a rate-limited node automatically, point opencode at the
local **bridge**: a loopback OpenAI-compatible reverse proxy (`baseURL`) that
forwards each request *through the active cascade* and watches the upstream
status. On a `429` it rotates the cascade (`openrot rotate` — next node / WARP)
and retries the request once, transparently.

The upstream endpoint and port are configurable — see the config fields
`bridge_port` (default `7891`) and `bridge_upstream` (default
`https://opencode.ai/zen/v1`, the endpoint opencode's built-in provider uses
natively), or the env overrides `OPENROT_BRIDGE_PORT` and `OPENROT_UPSTREAM`.

## Free sources (profiles)

Everything you add is a **profile**: a URL that returns a list of nodes.
`--kind` tells openrot how to parse it.

### relay profiles (`--kind relay`)

Sources of `vless://` subscription links. Public repos and mirrors that republish
daily dumps of working configurations:

- [igareck/vpn-configs-for-russia](https://gitlab.com/igareck/vpn-configs-for-russia) — direct raw file:
  ```
  https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/WHITE-CIDR-RU-all.txt
  ```
- Search GitHub/GitLab for `vless config list` / `v2ray subscription` — most of
  the repos that come up work as a relay profile.

```bash
poetry run openrot profile add gitlab <URL> --kind relay --priority 0
poetry run openrot update --relay
```

> Only `vless://` is parsed today. Files mixing other protocols (hysteria2,
> vmess, ss) aren't handled yet. See `NodeProtocol` in `models/enums.py`.

### proxy profiles (`--kind proxy`)

Sources of free `http` / `socks5` forward proxies (`proto://host:port` lines).
Updated lists from aggregators:

- [proxifly/free-proxy-list](https://github.com/proxifly/free-proxy-list) — validated every 5 minutes, served from CDN, raw text:
  ```
  https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt
  ```
  Protocol-specific: `.../proxies/protocols/http/data.txt`, `.../proxies/protocols/socks5/data.txt`.
- `free-proxy-list` mirrors and Telegram channels that post daily
  `proto://host:port` dumps work too.

```bash
poetry run openrot profile add proxifly <URL> --kind proxy --priority 10
poetry run openrot update --proxy
```

Free proxies are the least stable layer, so they usually get a **lower priority**
(a higher `--priority` number) than relay profiles: they only kick in when the
relay chain is exhausted. `update --proxy` keeps the 20 fastest alive.

### Priority and refresh

- `--priority N`: lower number, higher priority. Profiles are tried top-down,
  then nodes within each profile by their own priority.
- `--interval S`: per-profile refresh seconds. Defaults to the global
  `update_interval`. `openrot start cascade` refreshes each profile when
  its interval elapses. Manual `openrot update` respects `--name` and the
  flag-based kind filter.
- `--disabled`: add the profile as off; enable it later by editing `config.yaml`.

## Config

Stored in `~/.config/openrot/config.yaml`.
Overridden by `OPENROT_DIR` (base dir), `OPENROT_CONFIG` (full config path),
`OPENROT_PORT` (int), `OPENROT_SINGBOX_BIN` (sing-box binary name/path),
`OPENROT_BRIDGE_PORT` (int), and `OPENROT_UPSTREAM` (bridge upstream base URL).
All standardized fields are `StrEnum` values.

```yaml
port: 7890
strategy: fallback        # fallback | urltest
urltest_url: "https://www.gstatic.com/generate_204"   # latency reference target
health_interval: 30
health_timeout: 5
fail_threshold: 3
update_interval: 3600     # seconds; per-profile refresh default
warp_enabled: true        # WARP = top priority; disable via 'openrot warp off'
profiles:
  - name: gitlab
    kind: relay           # relay | proxy
    url: https://gitlab.com/igareck/vpn-configs-for-russia/-/raw/main/WHITE-CIDR-RU-all.txt
    priority: 0           # lower = higher
    enabled: true
    interval: null        # null → uses update_interval
    nodes:
      - id: "node-..."
        raw: "vless://..."
        protocol: vless   # vless | http | socks5
        priority: 0
        status: unknown   # unknown | alive | dead
        latency_ms: null
        fails: 0
        last_check: null
active_level: none        # none | warp | node
current_node_id: null
singbox_bin: "sing-box"
bridge_port: 7891         # loopback port for the opencode bridge
bridge_upstream: "https://opencode.ai/zen/v1"
```

Node latency is the time of an HTTP request to `urltest_url`
(default `https://www.gstatic.com/generate_204`); relay nodes go through
sing-box, http/socks5 proxies are probed directly.

## How nodes are verified

`openrot update` and the scheduler run every fetched node through the pipeline:

```
parse (dedupe vless:// / proto://host:port)
  -> TCP reachability (parallel, 50 workers)
  -> TLS handshake (only for tls/reality/wss nodes)
  -> sing-box config check (only relay)
  -> HTTP probe of urltest_url via the node
```

The probe requests `urltest_url` (default `generate_204`); a node needs a 2xx
response to survive. Every node that passes the earlier stages is probed (all
in parallel), survivors are ranked by latency (the measured request time) and
the **best 20** (`TOP_LIMIT`) are published per profile.

Progress is printed live: `update` shows a per-stage counter/bar
(`verify parse: 3/3`, `verify probe: 12/100`), and `openrot probe <url>` prints
the same stage counts as plain lines. `openrot test` re-verifies the currently
published nodes through the same pipeline.

## Strategies

- `fallback` — first alive node top-down by profile/node priority (default)
- `urltest` — lowest latency among alive nodes

In foreground mode, the current node is re-checked every `health_interval`
seconds and rotated when it fails (`fail_threshold` consecutive failures = dead).

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

Environment overrides: `OPENROT_IMAGE`, `OPENROT_PORT`, `OPENROT_CONFIG_DIR`,
`OPENROT_WARP_HOST`, `NETWORK`.

Add `sing-box` too; openrot needs it for relay nodes
(`brew install sing-box` on macOS).

## Docker

**WARP runs on the host, not in the container.** `warp-cli` needs a TUN
device and the OS network stack, so it can't tunnel inside Docker. The container
reaches the host-side WARP through its SOCKS5 upstream, so WARP and the node
chain work together.

### Makefile (recommended for dev)

```bash
make docker-run              # builds image and runs openrot, wired to the host WARP
make docker-run MODE=both    # same, but both daemons in the background
make docker-sh               # interactive shell in the image (same config mount)
```

By default the container uses bridge networking and points at the host's WARP via
`OPENROT_WARP_HOST=host.docker.internal` (host port 40000):

- macOS (Docker Desktop): works out of the box.
- Linux: the Makefile adds `--add-host host.docker.internal:host-gateway`.
- On Linux you can instead run `NETWORK=host make docker-run` to share the host
  loopback, so WARP answers at `127.0.0.1:40000` directly.

### Run modes

The container takes one argument — the run mode (the image's default `CMD` is
`both`):

| Mode     | What runs                                                                         |
|----------|-----------------------------------------------------------------------------------|
| `cascade`| Proxy/cascade in the foreground; the events log streams to `docker logs`.         |
| `bridge` | 429-rotation bridge in the foreground (starts the cascade along with it).         |
| `both`   | Cascade + bridge as background daemons, started simultaneously; `openrot logs` streams all of them to stdout and keeps the container alive. |

```bash
docker run --rm -it ... openrot cascade   # proxy only, logs in foreground
docker run --rm -it ... openrot bridge    # bridge only, foreground
docker run --rm -it ... openrot both      # both daemons, logs via `openrot logs`
docker stop <container>                   # stops supervision; daemons die with it
```

### Manual run

```bash
docker build -t openrot .
docker run --rm -it \
  -p 7890:7890 \
  -p 7891:7891 \
  --add-host host.docker.internal:host-gateway \
  -e OPENROT_WARP_HOST=host.docker.internal \
  -v $HOME/.config/openrot:/root/.config/openrot \
  openrot both
```

### What gets overridden / passed through

- **Config and data** are shared read-write via the `-v $HOME/.config/openrot:...`
  mount, so a profile you add on the host (or in the container) is visible on
  both sides. `OPENROT_DIR` is already set to `/root/.config/openrot` inside the
  image (see `Dockerfile`).
- **Ports**: the mixed proxy listens on `7890`, the bridge on `7891`; map a
  different host port with `-p $PORT:7890` / `-p $PORT_BRIDGE:7891`.
- **Listen address**: the proxy and bridge bind `127.0.0.1` by default; the
  image sets `OPENROT_LISTEN=0.0.0.0` so the published ports are reachable from
  the host (override with `-e OPENROT_LISTEN=127.0.0.1` to lock down).
- **WARP address**: `OPENROT_WARP_HOST` (default `127.0.0.1`) and
  `OPENROT_WARP_PORT` (default `40000`) point openrot at the host-side WARP
  SOCKS5 proxy. In the container set `OPENROT_WARP_HOST` to the host.
- **`OPENROT_PORT` / `OPENROT_SINGBOX_BIN`**: set with `-e` if you need to
  override `openrot` itself; `sing-box` is already preinstalled in the image.
- **Mode at container start**: `docker run ... openrot cascade|bridge|both`, or
  re-run an existing container with `docker start -ai <name> <mode>` (the mode
  becomes the container's command).

### Prerequisite: run WARP on the host first

Before `make docker-run`, start WARP once on the host in proxy mode:

```bash
poetry run openrot warp on
```

Then the container (or any openrot on the host) can use it. If the host WARP is
not reachable, openrot falls back to the profiles + node chain and
`status` reports `WARP: available on host only`.

## Development

- Tests: `poetry run pytest` (or `make check` — ruff + mypy + coverage).
- Coverage standard: keep the local report **above 85 %**; the CI gate is set
  lower (75 %) as a safety floor.

## FAQ

**Do I need to host anything?**
No. openrot only pulls remote lists, health-checks them, and runs a local
sing-box. Nothing is stored server-side.

**Why not just one node?**
Free sources die. openrot treats them as a pool with priorities: when the top
node dies, it rotates down the chain, and profiles get re-fetched on their
interval.