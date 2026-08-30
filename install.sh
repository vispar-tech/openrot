#!/usr/bin/env bash
#
# openrot — install the standalone `openrot` binary (PyInstaller onedir build).
#
#   curl -fsSL https://raw.githubusercontent.com/vispar-tech/openrot/main/install.sh | bash
#
# Installs warp-cli and sing-box prerequisites, downloads the openrot onedir
# build (launcher + _internal/ payload) and brings WARP up on the host in
# proxy mode.
#
# Env overrides:
#   OPENROT_BIN_URL   full URL to the prebuilt binary archive
#   OPENROT_VERSION   default tag/version for the download (default: latest)
#   OPENROT_PREFIX    install directory (default: ~/.local)
#   OPENROT_SKIP_WARP set to 1 to skip starting WARP

set -euo pipefail

PREFIX="${OPENROT_PREFIX:-$HOME/.local}"
VERSION="${OPENROT_VERSION:-latest}"
BIN_URL="${OPENROT_BIN_URL:-}"

say()  { printf '\033[36m[openrot]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[openrot]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[openrot]\033[0m %s\n' "$*" >&2; exit 1; }

macos() { [ "$(uname -s)" = "Darwin" ]; }
linux() { [ "$(uname -s)" = "Linux" ]; }

bin_dir="$PREFIX/bin"
URL_BASE="${BIN_URL:-https://github.com/vispar-tech/openrot/releases/download/$VERSION}"

# ---------------------------------------------------------------------------
# Prerequisites: warp-cli + sing-box
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

check_singbox() {
    if command -v sing-box >/dev/null 2>&1; then
        return 0
    fi
    warn "sing-box not found — openrot needs it for relay nodes. Install it:"
    if macos; then
        warn "  brew install sing-box"
    else
        warn "  https://github.com/SagerNet/sing-box/releases"
    fi
}

start_warp() {
    [ -n "${OPENROT_SKIP_WARP:-}" ] && return 0
    if ! command -v warp-cli >/dev/null 2>&1; then
        warn "warp-cli unusable — running node chain only."
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
# Download + install the binary
# ---------------------------------------------------------------------------
arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "aarch64" ;;
        *) die "Unsupported arch: $(uname -m)" ;;
    esac
}

download_binary() {
    local os
    if macos; then os="macos"; else os="linux"; fi
    local file="openrot-$VERSION-$os-$(arch).tar.gz"
    local url="$URL_BASE/$file"
    local tmp
    tmp="$(mktemp -d)"
    say "Downloading $url"
    curl -fsSL -o "$tmp/$file" "$url"
    tar -xzf "$tmp/$file" -C "$tmp"
    # Accept both a flat payload (launcher `openrot` + `_internal/` sibling at
    # the archive root) and an archive that wraps them in an `openrot/` dir.
    local src="$tmp"
    if [ -d "$tmp/openrot" ] && [ -x "$tmp/openrot/openrot" ]; then
        src="$tmp/openrot"
    fi
    mkdir -p "$bin_dir"
    install -m 0755 "$src/openrot" "$bin_dir/openrot"
    if [ -d "$src/_internal" ]; then
        rm -rf "$bin_dir/_internal"
        cp -R "$src/_internal" "$bin_dir/_internal"
        chmod -R u+rwX,go+rX "$bin_dir/_internal"
    fi
    rm -rf "$tmp"
    say "Installed openrot to $bin_dir/openrot"
    if ! echo "$PATH" | grep -q "$bin_dir"; then
        warn "Add $bin_dir to your PATH:"
        if macos; then
            warn '  echo '"'"'export PATH="'"$bin_dir"':$PATH"'"'"' >> ~/.zshrc'
        else
            warn '  echo '"'"'export PATH="'"$bin_dir"':$PATH"'"'"' >> ~/.bashrc'
        fi
    fi
}

# ---------------------------------------------------------------------------
# Resolve the latest release tag when OPENROT_VERSION is unset
# ---------------------------------------------------------------------------
resolve_latest() {
    [ "$VERSION" != "latest" ] && return 0
    VERSION="$(curl -fsSL -m 20 \
        https://api.github.com/repos/vispar-tech/openrot/releases/latest \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)"
    [ -n "$VERSION" ] || { VERSION="latest"; warn "Could not resolve latest release tag; using '$VERSION'"; }
    URL_BASE="https://github.com/vispar-tech/openrot/releases/download/$VERSION"
    say "Latest release: $VERSION"
}

# ---------------------------------------------------------------------------
main() {
    macos || linux || die "Unsupported OS. Supported: macOS, Linux."
    say "openrot installer (standalone binary)"
    resolve_latest
    install_warp
    start_warp
    check_singbox
    download_binary
    "$bin_dir/openrot" --version
    say "Done. Run 'openrot --help' to get started."
}

main "$@"
