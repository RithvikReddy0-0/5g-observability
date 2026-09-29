#!/usr/bin/env bash
# start_slice_exporter.sh — run the slice-labeled metrics exporter on the host.
#
# The exporter must run on the HOST (not in a container) because it shells out to
# `docker compose` to read AMF logs and query MongoDB. Prometheus scrapes it across the
# compose network gateway (10.100.200.1:9105) — see observability/prometheus/prometheus.yml.
#
# Usage:
#   scripts/start_slice_exporter.sh          # start (idempotent)
#   scripts/start_slice_exporter.sh --stop
#   scripts/start_slice_exporter.sh --status

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
EXPORTER="$REPO_ROOT/observability/slice-exporter/slice_exporter.py"
LOG=/tmp/slice_exporter.log
PORT="${PORT:-9105}"

# NOTE the [s] bracket: a plain pattern would also match this script's own command line
# (and any shell invoking it), so pkill would kill the caller.
PATTERN="[s]lice_exporter.py"

case "${1:-}" in
  --stop)
    pkill -f "$PATTERN" 2>/dev/null && echo "slice exporter stopped" || echo "not running"
    exit 0
    ;;
  --status)
    if pgrep -f "$PATTERN" >/dev/null 2>&1; then
      echo "running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
      curl -s --max-time 20 "http://localhost:$PORT/metrics" | grep -E '^free5gc_slice' || echo "(no metrics yet)"
    else
      echo "not running"
    fi
    exit 0
    ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 2; }
[ -f "$EXPORTER" ] || { echo "ERROR: $EXPORTER not found" >&2; exit 2; }

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "already running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
  exit 0
fi

# setsid detaches from the controlling terminal so the exporter survives the shell exiting.
setsid nohup python3 "$EXPORTER" > "$LOG" 2>&1 < /dev/null &
sleep 3

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "slice exporter started on :$PORT (log: $LOG)"
  exit 0
fi
echo "FAILED to start — see $LOG" >&2
tail -20 "$LOG" 2>/dev/null
exit 1
