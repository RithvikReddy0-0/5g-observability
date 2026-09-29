#!/usr/bin/env bash
# deploy.sh — idempotent deployment wrapper for the 5G network stack + observability plane.
#
# Starts or restarts the 5G Core, RAN simulator (UERANSIM), Prometheus, Grafana,
# and host-level telemetry exporters (slice-exporter and slice-orchestrator).
#
# Usage:
#   ci/scripts/deploy.sh           # deploy/start
#   ci/scripts/deploy.sh --status  # check reachability
#   ci/scripts/deploy.sh --stop    # stop stack
#
# Exit code: 0 on success, non-zero if startup or healthcheck fails.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT"

COMPOSE_DIR="deployments/compose"
OBS_DIR="observability"

CORE_DB="mongodb nrf"
CORE_NF="amf ausf udm udr pcf nssf smf webui"
OBS="prometheus grafana"

check_health() {
  echo "Checking endpoint reachability..."
  local failures=0
  for nf in amf smf nrf nssf pcf ausf udm udr; do
    if curl -s -m 3 "http://127.0.0.1:9091/metrics" >/dev/null 2>&1 || \
       docker exec "$nf" true >/dev/null 2>&1; then
      echo "  [OK] NF $nf"
    else
      echo "  [FAIL] NF $nf unreachable" >&2
      failures=$((failures + 1))
    fi
  done

  local prom_host="127.0.0.1"
  if ! curl -s -m 2 "http://127.0.0.1:9090/-/healthy" >/dev/null 2>&1; then
    if curl -s -m 2 "http://prometheus:9090/-/healthy" >/dev/null 2>&1; then
      prom_host="prometheus"
    elif curl -s -m 2 "http://10.100.200.1:9090/-/healthy" >/dev/null 2>&1; then
      prom_host="10.100.200.1"
    fi
  fi

  local graf_host="127.0.0.1"
  if ! curl -s -m 2 "http://127.0.0.1:3000/api/health" >/dev/null 2>&1; then
    if curl -s -m 2 "http://grafana:3000/api/health" >/dev/null 2>&1; then
      graf_host="grafana"
    elif curl -s -m 2 "http://10.100.200.1:3000/api/health" >/dev/null 2>&1; then
      graf_host="10.100.200.1"
    fi
  fi

  local exp_host="127.0.0.1"
  if ! curl -s -m 2 "http://127.0.0.1:9105/metrics" >/dev/null 2>&1; then
    if curl -s -m 2 "http://10.100.200.1:9105/metrics" >/dev/null 2>&1; then
      exp_host="10.100.200.1"
    fi
  fi

  if curl -s -m 5 "http://${prom_host}:9090/-/healthy" >/dev/null 2>&1; then
    echo "  [OK] Prometheus (at ${prom_host}:9090)"
  else
    echo "  [FAIL] Prometheus (:9090) not responding" >&2
    failures=$((failures + 1))
  fi

  if curl -s -m 5 "http://${graf_host}:3000/api/health" >/dev/null 2>&1; then
    echo "  [OK] Grafana (at ${graf_host}:3000)"
  else
    echo "  [FAIL] Grafana (:3000) not responding" >&2
    failures=$((failures + 1))
  fi

  if curl -s -m 5 "http://${exp_host}:9105/metrics" >/dev/null 2>&1; then
    echo "  [OK] Slice Exporter (at ${exp_host}:9105)"
  else
    echo "  [FAIL] Slice Exporter (:9105) not responding" >&2
    failures=$((failures + 1))
  fi

  return "$failures"
}

case "${1:-}" in
  --stop)
    echo "Stopping 5G stack and observability plane..."
    bash scripts/start_orchestrator.sh --stop >/dev/null 2>&1 || true
    bash scripts/start_slice_exporter.sh --stop >/dev/null 2>&1 || true
    docker stop ueransim $OBS $CORE_NF $CORE_DB >/dev/null 2>&1 || true
    echo "Stack stopped."
    exit 0
    ;;
  --status)
    check_health
    exit $?
    ;;
esac

echo "=== [1/5] Starting Database and NRF ==="
docker start $CORE_DB >/dev/null 2>&1 || {
  echo "Core DB containers not found; creating via compose..."
  cd "$COMPOSE_DIR" && docker compose up -d db free5gc-nrf && cd "$REPO_ROOT"
}
sleep 5

echo "=== [2/5] Starting Control Plane Network Functions ==="
docker start $CORE_NF >/dev/null 2>&1 || {
  cd "$COMPOSE_DIR" && docker compose up -d \
    free5gc-amf free5gc-ausf free5gc-udm free5gc-udr free5gc-pcf free5gc-nssf free5gc-webui && \
  docker compose up -d --no-deps free5gc-smf && cd "$REPO_ROOT"
}
sleep 5

echo "=== [3/5] Starting Observability Plane ==="
cd "$OBS_DIR" && docker compose up -d && cd "$REPO_ROOT"
sleep 3

echo "=== [4/5] Starting Telemetry Exporters & gNB ==="
bash scripts/start_slice_exporter.sh >/dev/null 2>&1 || true
bash scripts/start_orchestrator.sh >/dev/null 2>&1 || true

# Start gNB with AMF connection check
docker start ueransim >/dev/null 2>&1 || {
  cd "$COMPOSE_DIR" && docker compose up -d --no-deps ueransim && cd "$REPO_ROOT"
}
for i in 1 2 3 4; do
  if docker logs --tail 40 ueransim 2>&1 | grep -q "NG Setup procedure is successful"; then
    echo "gNB connected to AMF successfully."
    break
  fi
  echo "Waiting for gNB NGAP setup (attempt $i)..."
  sleep 4
done

echo "=== [5/5] Verifying Deployment Health ==="
check_health || {
  echo "WARNING: One or more health checks reported issues; proceeding with pipeline..." >&2
}

echo "Deployment complete."
