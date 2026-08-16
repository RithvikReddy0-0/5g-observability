#!/usr/bin/env bash
# start_orchestrator.sh — run the slice orchestrator on the host.
#
# It runs on the host (not in a container) because it reads the live subscriber database via
# `docker exec`. Prometheus scrapes it across the compose network gateway at
# 10.100.200.1:9110 — host.docker.internal does not resolve on that user-defined network.
#
# Usage: scripts/start_orchestrator.sh [--stop|--status]

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
APP="$REPO_ROOT/tools/slice-orchestrator/orchestrator.py"
LOG=/tmp/slice_orchestrator.log
PORT="${PORT:-9110}"

# The [o] bracket stops the pattern matching this script's own command line.
PATTERN="[o]rchestrator.py"

case "${1:-}" in
  --stop)
    pkill -f "$PATTERN" 2>/dev/null && echo "orchestrator stopped" || echo "not running"
    exit 0 ;;
  --status)
    if pgrep -f "$PATTERN" >/dev/null 2>&1; then
      echo "running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
      curl -s --max-time 10 "http://localhost:$PORT/state" | head -40
    else
      echo "not running"
    fi
    exit 0 ;;
esac

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 2; }
[ -f "$APP" ] || { echo "ERROR: $APP not found" >&2; exit 2; }

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "already running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
  exit 0
fi

setsid nohup python3 "$APP" > "$LOG" 2>&1 < /dev/null &
sleep 3

if pgrep -f "$PATTERN" >/dev/null 2>&1; then
  echo "slice orchestrator started on :$PORT (log: $LOG)"
  sed -n '1,6p' "$LOG" 2>/dev/null | sed 's/^/  /'
  exit 0
fi
echo "FAILED to start — see $LOG" >&2
tail -20 "$LOG" 2>/dev/null
exit 1
