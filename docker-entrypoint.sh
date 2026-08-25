#!/bin/sh
#
# openrot container entrypoint — pick the run mode via the first argument
# (i.e. the `docker run image <mode>` / `docker start` command, or CMD):
#
#   cascade   proxy/cascade in the foreground; logs stream to stdout
#   bridge    429-rotation bridge in the foreground
#   both      cascade + bridge as background daemons, supervised by `openrot logs`
#
set -e

MODE="${1:-both}"
PORT="${OPENROT_PORT:-7890}"

case "$MODE" in
  --*|-*)
    # Any CLI flag (e.g. --version, --help) is forwarded straight to openrot
    # instead of being treated as a run mode.
    exec openrot "$@"
    ;;
  cascade)
    exec openrot start cascade
    ;;
  bridge)
    exec openrot start bridge
    ;;
  both)
    echo "[openrot] starting cascade daemon..."
    openrot start cascade --daemon
    echo "[openrot] waiting for the proxy on 127.0.0.1:$PORT..."
    i=0
    while [ "$i" -lt 20 ]; do
        if python3 -c "import socket as s; s.socket().connect(('127.0.0.1', $PORT))" 2>/dev/null; then
            break
        fi
        sleep 1
        i=$((i + 1))
    done
    if [ "$i" -ge 20 ]; then
        echo "[openrot] warning: proxy did not come up in time" >&2
    fi
    echo "[openrot] starting bridge daemon..."
    openrot start bridge --daemon
    echo "[openrot] following logs (docker stop to quit)..."
    exec openrot logs
    ;;
  *)
    echo "openrot: unknown mode '$MODE' (expected: cascade, bridge, both)" >&2
    exit 2
    ;;
esac