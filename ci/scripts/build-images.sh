#!/usr/bin/env bash
# build-images.sh — pre-flight verification of required container images and tools.
#
# Validates that all required core and observability images are present locally,
# and checks that Python helper tools and test scripts compile without syntax errors.
#
# Usage:
#   ci/scripts/build-images.sh
#
# Exit code: 0 on success, non-zero if any required image or syntax check fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT"

echo "=== [1/3] Checking Docker daemon access ==="
docker info >/dev/null 2>&1 || { echo "ERROR: Cannot connect to the Docker daemon" >&2; exit 1; }
echo "Docker daemon reachable."

echo "=== [2/3] Verifying required 5G and Observability images ==="
REQUIRED_IMAGES=(
  "mongo:4.4"
  "free5gc/nrf:v4.2.3"
  "free5gc/amf:v4.2.3"
  "free5gc/smf:v4.2.3"
  "free5gc/udr:v4.2.3"
  "free5gc/udm:v4.2.3"
  "free5gc/pcf:v4.2.3"
  "free5gc/nssf:v4.2.3"
  "free5gc/ausf:v4.2.3"
  "free5gc/webui:v4.2.3"
  "free5gc/ueransim:latest"
  "prom/prometheus:v3.1.0"
  "grafana/grafana:11.5.2"
)

missing=0
for img in "${REQUIRED_IMAGES[@]}"; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    echo "  [FOUND] $img"
  else
    echo "  [MISSING] $img — attempting pull..."
    if docker pull "$img"; then
      echo "  [PULLED] $img"
    else
      echo "  [FAILED] Failed to pull $img" >&2
      missing=$((missing + 1))
    fi
  fi
done

if [ "$missing" -gt 0 ]; then
  echo "ERROR: $missing required images are missing" >&2
  exit 1
fi

echo "=== [3/3] Checking Python tools syntax and script permissions ==="
python3 -m py_compile \
  tools/kpi-gate/*.py \
  tools/slice-orchestrator/*.py \
  observability/slice-exporter/*.py

chmod +x scripts/*.sh ci/scripts/*.sh tests/*.sh 2>/dev/null || true

echo "All images and tools verified successfully."
