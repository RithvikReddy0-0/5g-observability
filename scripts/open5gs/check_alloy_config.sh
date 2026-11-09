#!/usr/bin/env bash
# Load the generated Alloy pipeline into the pinned Alloy image and fail if Alloy refuses it (ADR-024).
#
# `alloy fmt` checks only Alloy's own syntax. The LogQL inside each stage.match selector is parsed when
# the pipeline is built, so a quoting mistake there passes `fmt` and the generator check, and stops the
# whole pipeline on the stack. That happened on the first deployment, twice (2026-11-09): re.escape's
# "\-" inside a Go string ("invalid char escape"), then backtick strings ("expecting STRING").
#
# Runs `alloy run` on the real config for up to 30 s with the Docker socket mounted (discovery needs
# it; nothing is shipped: Loki at 10.53.0.43 is unreachable here, which is a send error, not a config one).
#
#   bash scripts/open5gs/check_alloy_config.sh [config.alloy]
set -u
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CFG="${1:-$REPO_ROOT/deployments/open5gs/observability/alloy/config.alloy}"
IMAGE=$(grep -oE 'grafana/alloy@sha256:[0-9a-f]{64}' "$REPO_ROOT/deployments/open5gs/docker-compose.logs.yaml" | head -1)
[ -n "$IMAGE" ] || { echo "no pinned Alloy image in docker-compose.logs.yaml" >&2; exit 2; }
[ -f "$CFG" ] || { echo "no such config: $CFG" >&2; exit 2; }

DIR=$(cd "$(dirname "$CFG")" && pwd); NAME=$(basename "$CFG")
# Detached and always removed: a foreground run waits on Alloy's shutdown, which retries the batches it
# could not send for minutes.
C="alloy-config-check-$$"
trap 'docker rm -f "$C" >/dev/null 2>&1' EXIT
docker run -d --name "$C" -v "$DIR:/cfg:ro" -v /var/run/docker.sock:/var/run/docker.sock:ro \
  "$IMAGE" run "/cfg/$NAME" --storage.path=/tmp/alloy >/dev/null || { echo "could not start $IMAGE" >&2; exit 2; }
OUT=""
for _ in $(seq 30); do
  sleep 1
  OUT=$(docker logs "$C" 2>&1)
  printf '%s\n' "$OUT" | grep -qE 'finished complete graph evaluation|failed to evaluate config|Error: ' && break
done
sleep 2; OUT=$(docker logs "$C" 2>&1)

BAD=$(printf '%s\n' "$OUT" | grep -E 'failed to evaluate config|failed to load|invalid stage config|Error: ' || true)
if [ -n "$BAD" ]; then
  echo "Alloy REFUSED $CFG:" >&2
  printf '%s\n' "$BAD" | grep -o 'node=[^ ]*\|err="[^"]*' | paste - - | sort -u | head -20 >&2
  exit 1
fi
if ! printf '%s\n' "$OUT" | grep -q 'finished complete graph evaluation'; then
  echo "Alloy never finished evaluating $CFG (no 'finished complete graph evaluation'):" >&2
  printf '%s\n' "$OUT" | tail -20 >&2
  exit 1
fi
echo "Alloy builds every component of $(basename "$CFG") ($IMAGE)"
