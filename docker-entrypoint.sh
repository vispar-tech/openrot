#!/bin/sh
#
# openrot container entrypoint — pick the run mode via the first argument
# (i.e. the `docker run image <mode>` / `docker start` command, or CMD):
#
#   bridge    WARP-rotation bridge in the foreground; logs stream to stdout
#
set -e

MODE="${1:-bridge}"

case "$MODE" in
  --*|-*)
    # Any CLI flag (e.g. --version, --help) is forwarded straight to openrot
    # instead of being treated as a run mode.
    exec openrot "$@"
    ;;
  bridge)
    exec openrot start bridge
    ;;
  *)
    echo "openrot: unknown mode '$MODE' (expected: bridge)" >&2
    exit 2
    ;;
esac
