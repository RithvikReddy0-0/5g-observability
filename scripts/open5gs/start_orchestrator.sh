#!/usr/bin/env bash
# start_orchestrator.sh — the slice orchestrator for the Open5GS stack: measured and executed.
#
# Same decision logic as the free5GC instance (tools/slice-orchestrator/orchestrator.py),
# started with:
#   CORE=open5gs        subscribers and permitted slices from the Open5GS database
#   PROMETHEUS_URL      admission counts what each slice's UPF is measured to carry
#   EXECUTE=1           every admitted demand runs as a real UDP flow on the chosen slice
#
# Runs on the host (it drives containers with `docker exec`). Prometheus reaches it through
# the o5gs_net gateway, 10.53.0.1:9111. It is a separate process from the free5GC instance
# (:9110), told apart by its "--instance open5gs" argument.
#
# Usage: scripts/open5gs/start_orchestrator.sh [--stop|--status]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
APP="$REPO_ROOT/tools/slice-orchestrator/orchestrator.py"
LOG=/tmp/o5gs_orchestrator.log
PORT="${PORT:-9111}"
PATTERN="[o]rchestrator.py --instance open5gs"   # [o]: never match this script itself

case "${1:-}" in
  --stop)
    pkill -f "$PATTERN" 2>/dev/null && echo "open5gs orchestrator stopped" || echo "not running"
    exit 0 ;;
  --status)
    if pgrep -f "$PATTERN" >/dev/null; then
      echo "running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
      curl -s --max-time 10 "http://localhost:$PORT/state"
    else
      echo "not running"
    fi
    exit 0 ;;
esac

if pgrep -f "$PATTERN" >/dev/null; then
  echo "open5gs orchestrator already running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
  exit 0
fi

CORE=open5gs PORT="$PORT" DB_CONTAINER=o5gs-mongodb \
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}" EXECUTE="${EXECUTE:-1}" \
FLOW_SECONDS="${FLOW_SECONDS:-20}" \
  setsid nohup python3 "$APP" --instance open5gs > "$LOG" 2>&1 < /dev/null &

for _ in $(seq 20); do
  curl -s --max-time 2 "http://localhost:$PORT/" >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -s --max-time 5 "http://localhost:$PORT/" >/dev/null 2>&1; then
  echo "open5gs slice orchestrator on :$PORT (log: $LOG)"
  sed -n '1,4p' "$LOG" | sed 's/^/  /'
else
  echo "FAILED to start — see $LOG" >&2
  tail -20 "$LOG" >&2
  exit 1
fi
