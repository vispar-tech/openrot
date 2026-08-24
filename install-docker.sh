#!/usr/bin/env bash
#
# openrot — run openrot in Docker, wired to the host-side WARP.
#
#   curl -fsSL <URL>/install-docker.sh | bash
#
# Installs warp-cli on the host, brings WARP up in proxy mode, then runs the
# openrot container with the correct network mode for the platform (host on
# Linux, bridge + host.docker.internal on macOS). Docker itself is NOT
# installed — you're shown how if it's missing.
#
# Env overrides:
#   OPENROT_IMAGE        image name (default: ghcr.io/vispar-tech/openrot:latest)
#   OPENROT_MODE         run mode: cascade | bridge | both (default: both)
#   OPENROT_PORT         host port (default: 7890)
#   OPENROT_BRIDGE_PORT  host bridge port (default: 7891)
#   OPENROT_CONFIG_DIR   config dir (default: ~/.config/openrot)
#   OPENROT_WARP_HOST    WARP upstream host for bridge mode (default: host.docker.internal)
#   NETWORK              host | bridge (default: auto — host on Linux, bridge on macOS)

set -euo pipefail

IMAGE="${OPENROT_IMAGE:-ghcr.io/vispar-tech/openrot:latest}"
MODE="${OPENROT_MODE:-both}"
PORT="${OPENROT_PORT:-7890}"
BRIDGE_PORT="${OPENROT_BRIDGE_PORT:-7891}"
CONFIG_DIR="${OPENROT_CONFIG_DIR:-$HOME/.config/openrot}"
WARP_HOST="${OPENROT_WARP_HOST:-host.docker.internal}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[36m[openrot]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[openrot]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[openrot]\033[0m %s\n' "$*" >&2; exit 1; }

macos() { [ "$(uname -s)" = "Darwin" ]; }
linux() { [ "$(uname -s)" = "Linux" ]; }

# ---------------------------------------------------------------------------
# 1. Docker
# ---------------------------------------------------------------------------
require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        die "Docker not found. Install it first, then re-run:\n" \
            "  macOS:  brew install --cask docker\n" \
            "  Linux:  curl -fsSL https://get.docker.com | sh"
    fi
    if ! docker info >/dev/null 2>&1; then
        die "Docker is installed but the daemon is not running. Start Docker, then re-run."
    fi
}

# ---------------------------------------------------------------------------
# 2. warp-cli on the host
# ---------------------------------------------------------------------------
warp_bin() { command -v warp-cli || true; }

install_warp() {
    if [ -n "$(warp_bin)" ]; then
        say "warp-cli installed: $(warp_bin)"
        return 0
    fi
    say "warp-cli not found — installing Cloudflare WARP."
    if macos; then
        brew install --cask cloudflare-warp
        open -a WARP; sleep 5
    elif linux; then
        if command -v apt-get >/dev/null 2>&1; then
            curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
                | sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
            echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
                | sudo tee /etc/apt/sources.list.d/cloudflare-client.list >/dev/null
            sudo apt-get update -y
            sudo apt-get install -y cloudflare-warp
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf copr enable -y folliehiyuki/fedora-warp lfs
            sudo dnf install -y cloudflare-warp
        else
            die "Unsupported package manager. Install Cloudflare WARP manually."
        fi
        sudo systemctl enable --now warp-svc
    else
        die "Unsupported OS. Supported: macOS, Linux."
    fi
}

start_warp() {
    if ! command -v warp-cli >/dev/null 2>&1; then
        warn "warp-cli unusable — container will run the node chain only."
        return 0
    fi
    say "Bringing WARP up in proxy mode on the host..."
    warp-cli mode proxy 2>/dev/null || true
    warp-cli proxy port 40000 2>/dev/null || true
    warp-cli connect 2>/dev/null || true
    sleep 3
    if warp-cli status 2>/dev/null | grep -qi Connected; then
        say "WARP connected (SOCKS5 on 127.0.0.1:40000)."
    else
        warn "WARP did not report connected — openrot falls back to the node chain."
    fi
}

# ---------------------------------------------------------------------------
# 3. Image
# ---------------------------------------------------------------------------
ensure_image() {
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
        say "Using existing image: $IMAGE"
        return 0
    fi
    if [ -f "$SCRIPT_DIR/Dockerfile" ]; then
        say "Building $IMAGE from local Dockerfile"
        (cd "$SCRIPT_DIR" && docker build -t "$IMAGE" .)
    else
        say "Pulling $IMAGE"
        docker pull "$IMAGE"
    fi
}

# ---------------------------------------------------------------------------
# 4. Run with a platform-correct network mode
# ---------------------------------------------------------------------------
NET="${NETWORK:-}"
[ -n "$NET" ] || { linux && NET=host || NET=bridge; }

run_container() {
    say "Mode: $MODE (cascade | bridge | both — set OPENROT_MODE to override)."
    if [ "$NET" = "host" ]; then
        say "Using host network — WARP visible at 127.0.0.1:40000."
        exec docker run --rm -it --network host \
            -v "$CONFIG_DIR":/root/.config/openrot \
            "$IMAGE" "$MODE"
    fi
    say "Using bridge network — WARP at $WARP_HOST:40000."
    exec docker run --rm -it --network bridge \
        -p "$PORT":7890 \
        -p "$BRIDGE_PORT":7891 \
        --add-host host.docker.internal:host-gateway \
        -e OPENROT_WARP_HOST="$WARP_HOST" \
        -e OPENROT_WARP_PORT=40000 \
        -v "$CONFIG_DIR":/root/.config/openrot \
        "$IMAGE" "$MODE"
}

# ---------------------------------------------------------------------------
main() {
    macos || linux || die "Unsupported OS. Supported: macOS, Linux."
    say "openrot installer (Docker)"
    require_docker
    install_warp
    start_warp
    ensure_image
    run_container
}

main "$@"
