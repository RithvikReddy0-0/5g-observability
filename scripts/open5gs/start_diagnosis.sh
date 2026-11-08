#!/usr/bin/env bash
# start_diagnosis.sh — the fault-diagnosis service for the Open5GS stack (ADR-024). OPTIONAL.
#
# A host process like the orchestrators: every DIAG_INTERVAL seconds (default 120) it correlates the
# last DIAG_WINDOW seconds (default 600) of Prometheus series, logs (Loki if it is up, else
# `docker logs`) and decision traces, and serves the verdict:
#   GET :9115/diagnosis     ranked causes, each with its evidence and actions
#   GET :9115/correlation   the correlation report behind it (anomalies, timeline)
#   GET :9115/metrics       fault_diagnosis_* for Prometheus (job fault-diagnosis, 10.53.0.1:9115)
# Read-only towards the stack. ~30 Prometheus range queries per run; ~25 MB of memory.
# DIAG_DOCKER=0 stops it reading docker logs (metrics and traces only).
#
# Usage: scripts/open5gs/start_diagnosis.sh [--stop|--status]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
APP="$REPO_ROOT/tools/observability/diagnose.py"
LOG=/tmp/o5gs_diagnosis.log
PORT="${PORT:-9115}"
PATTERN="[d]iagnose.py --serve"

case "${1:-}" in
  --stop)
    pkill -f "$PATTERN" 2>/dev/null && echo "fault diagnosis stopped" || echo "not running"
    exit 0 ;;
  --status)
    if pgrep -f "$PATTERN" >/dev/null; then
      curl -s --max-time 5 "http://localhost:$PORT/diagnosis"
    else
      echo "not running"
    fi
    exit 0 ;;
esac

if pgrep -f "$PATTERN" >/dev/null; then
  echo "fault diagnosis already running (pid $(pgrep -f "$PATTERN" | tr '\n' ' '))"
  exit 0
fi

PORT="$PORT" PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9091}" LOKI_URL="${LOKI_URL:-http://localhost:3100}" \
OBS_TRACE_FILE="${OBS_TRACE_FILE:-/tmp/o5gs_traces.jsonl}" NETORCH_LOG="${NETORCH_LOG:-/tmp/o5gs_netorch_ops.jsonl}" \
DIAG_INTERVAL="${DIAG_INTERVAL:-120}" DIAG_WINDOW="${DIAG_WINDOW:-600}" DIAG_DOCKER="${DIAG_DOCKER:-1}" \
  setsid nohup python3 "$APP" --serve > "$LOG" 2>&1 < /dev/null &

for _ in $(seq 20); do
  curl -s --max-time 2 "http://localhost:$PORT/" >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -s --max-time 5 "http://localhost:$PORT/" >/dev/null 2>&1; then
  echo "fault diagnosis on :$PORT (log: $LOG)"
  head -1 "$LOG" | sed 's/^/  /'
else
  echo "FAILED to start — see $LOG" >&2
  tail -20 "$LOG" >&2
  exit 1
fi
