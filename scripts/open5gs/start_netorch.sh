#!/usr/bin/env bash
# start_netorch.sh — the network orchestrator (slice lifecycle, ADR-017) for the Open5GS stack.
#
# A host process like the slice orchestrator: it drives containers with `docker start/stop/update`
# and `docker exec`. Prometheus reaches it through the o5gs_net gateway, 10.53.0.1:9113.
#
#   NETORCH_AUTO=1     closed loop on the slice orchestrator's /plan (default off: lifecycle on request)
#   ON_DEMAND="mMTC"   slices the closed loop may activate and deactivate
#   IDLE_SECONDS=120   no demand for this long -> deactivate an on-demand slice
#
# Usage: scripts/open5gs/start_netorch.sh [--stop|--status]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
APP="$REPO_ROOT/tools/network-orchestrator/netorch.py"
LOG=/tmp/o5gs_netorch.log
PORT="${PORT:-9113}"
PATTERN="[n]etorch.py --serve"

case "${1:-}" in
  --stop)
    pkill -f "$PATTERN" 2>/dev/null && echo "network orchestrator stopped" || echo "not running"
    exit 0 ;;
  --status)
    if pgrep -f "$PATTERN" >/dev/null; then python3 "$APP" status; else echo "not running"; fi
    exit 0 ;;
esac

if pgrep -f "$PATTERN" >/dev/null; then
  echo "network orchestrator already running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
  exit 0
fi

PORT="$PORT" setsid nohup python3 "$APP" --serve > "$LOG" 2>&1 < /dev/null &

for _ in $(seq 20); do
  curl -s --max-time 2 "http://localhost:$PORT/" >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -s --max-time 5 "http://localhost:$PORT/" >/dev/null 2>&1; then
  echo "network orchestrator on :$PORT (log: $LOG)"
  head -1 "$LOG" | sed 's/^/  /'
else
  echo "FAILED to start — see $LOG" >&2
  tail -20 "$LOG" >&2
  exit 1
fi
