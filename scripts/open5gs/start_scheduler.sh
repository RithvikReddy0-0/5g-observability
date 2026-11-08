#!/usr/bin/env bash
# start_scheduler.sh — the packet scheduler for the Open5GS stack (ADR-019).
#
# Runs as a container on the host's network and PID namespaces, privileged: it installs queues on
# the host side of the UE container's link (downlink) and, through nsenter, inside the UE
# container (uplink). The scheduler code is mounted read-only from this repository. Prometheus
# reaches it through the o5gs_net gateway, 10.53.0.1:9114.
#
# Settings come from deployments/slices.env (SCHED_*, SLICE_<X>_SCHED_*). Change them at run time
# with POST /config, e.g.
#   curl -s -XPOST localhost:9114/config -d '{"mode":"fifo"}'
#   curl -s -XPOST localhost:9114/config -d '{"shares":{"eMBB":50,"mMTC":15}}'
#
# Usage: scripts/open5gs/start_scheduler.sh [--stop|--status]
#   --stop also removes the queues (mode off), restoring the unshaped link.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
NAME=o5gs-scheduler
IMAGE=o5gs/scheduler:1
PORT="${PORT:-9114}"

case "${1:-}" in
  --stop)
    if docker inspect "$NAME" >/dev/null 2>&1; then
      curl -s --max-time 10 -XPOST "localhost:$PORT/config" -d '{"mode":"off"}' >/dev/null
      docker rm -f "$NAME" >/dev/null && echo "packet scheduler stopped; queues removed"
    else
      echo "not running"
    fi
    exit 0 ;;
  --status)
    curl -s --max-time 5 "localhost:$PORT/state" || echo "not running"
    exit 0 ;;
esac

if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; then
  echo "packet scheduler already running"
  exit 0
fi
docker rm -f "$NAME" >/dev/null 2>&1

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE"
  docker build -q -f "$REPO_ROOT/deployments/open5gs/images/scheduler.Dockerfile" -t "$IMAGE" \
    "$REPO_ROOT/deployments/open5gs/images" >/dev/null || { echo "image build failed" >&2; exit 1; }
fi

docker run -d --name "$NAME" --restart unless-stopped \
  --network host --pid host --privileged \
  -e PORT="$PORT" \
  -e OBS_TRACE_FILE="${OBS_TRACE_FILE-/host-tmp/o5gs_traces.jsonl}" \
  -v /tmp:/host-tmp \
  -v "$REPO_ROOT:/repo:ro" \
  "$IMAGE" >/dev/null || { echo "FAILED to start" >&2; exit 1; }

for _ in $(seq 30); do
  curl -s --max-time 2 "http://localhost:$PORT/state" | grep -q '"applies": [1-9]' && break
  sleep 1
done
if curl -s --max-time 5 "http://localhost:$PORT/state" | grep -q '"applies": [1-9]'; then
  echo "packet scheduler on :$PORT"
  docker logs "$NAME" 2>&1 | head -2 | sed 's/^/  /'
else
  echo "FAILED to apply — docker logs $NAME:" >&2
  docker logs --tail 20 "$NAME" >&2
  exit 1
fi
