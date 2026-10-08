#!/usr/bin/env bash
# netorch_demo.sh — the network orchestrator (ADR-017) on the live stack, in four experiments.
#
#   1. lifecycle   deactivate mMTC, hold it dormant, activate it again. Per-step timings; core
#                  session counts; the other slices' UEs and URLLC's latency probes throughout;
#                  what the dormant slice gives back (processes, memory).
#   2. rollback    an activation that misses its deadline (ACTIVATE_DEADLINE=10 s, below the
#                  ~15 s the SMF takes to associate with a fresh UPF) is rolled back automatically,
#                  leaving the slice dormant and clean; then a normal activation succeeds.
#   3. closed loop the network orchestrator follows the slice orchestrator's /plan: with no IoT
#                  demand mMTC is deactivated after IDLE_SECONDS; when IoT devices ask for
#                  service it is activated; when they stop, deactivated again.
#   4. compute     the same eMBB TCP load with eMBB's gNB and UPF given unlimited, 1 and 0.5 CPUs:
#                  eMBB throughput, and URLLC latency meanwhile.
#
# Output: docs/evidence/open5gs-network-orchestrator/ (OUT to override)
# Usage:  scripts/open5gs/netorch_demo.sh            ONLY=lifecycle|rollback|loop|compute

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
OUT="${OUT:-docs/evidence/open5gs-network-orchestrator}"
ONLY="${ONLY:-}"
mkdir -p "$OUT"
N="python3 tools/network-orchestrator/netorch.py"
URLLC_KPI=http://10.53.0.52:9130
MMTC_KPI=http://10.53.0.53:9130

netorch() {   # restart the network orchestrator with the given environment
  bash scripts/open5gs/start_netorch.sh --stop >/dev/null
  env "$@" bash scripts/open5gs/start_netorch.sh >/dev/null || { echo "netorch failed to start" >&2; exit 1; }
}
slice_orch() {
  bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
  env "$@" bash scripts/open5gs/start_orchestrator.sh >/dev/null || { echo "slice orchestrator failed" >&2; exit 1; }
}
ensure_active() { $N status | grep -q "^mMTC   ACTIVE" || $N activate mMTC >/dev/null; }
urllc_stats() { curl -s "$URLLC_KPI/run/stats" | python3 -c "import json,sys
k = json.load(sys.stdin); o = k['owd_ms']
print('URLLC probes: %d received, loss %s, one-way mean %s / p95 %s / p99 %s ms' % (k['received'], k['loss_ratio'], o.get('mean'), o.get('p95'), o.get('p99')))"; }
resources() {
  echo "  host load (1 min): $(cut -d' ' -f1 /proc/loadavg)"
  echo "  nr-ue processes: $(docker exec o5gs-ue pgrep -c nr-ue)"
  docker stats --no-stream --format '  {{.Name}} cpu {{.CPUPerc}} mem {{.MemUsage}}' \
    o5gs-ue o5gs-gnb-mmtc o5gs-upf-mmtc o5gs-dn-mmtc 2>/dev/null
}

{
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git: $(git rev-parse --short HEAD) $(git diff --quiet || echo '(working tree modified)')"
  echo "ue_sessions: $(docker exec o5gs-ue ip -4 -o addr show | grep -c uesimtun)"
  echo "free5gc_running: $(docker ps --format '{{.Names}}' | grep -cv '^o5gs-') containers"
} > "$OUT/meta.txt"

slice_orch
# ─────────────────────────── 1. lifecycle ───────────────────────────
if [ -z "$ONLY" ] || [ "$ONLY" = lifecycle ]; then
  netorch
  ensure_active
  curl -s "$URLLC_KPI/run/start" >/dev/null
  {
    echo "== experiment 1: lifecycle of the mMTC slice"
    $N status
    echo; echo "-- resources with mMTC active"; resources
    echo; echo "-- deactivate"; $N deactivate mMTC
    echo; $N status
    sleep 20
    echo; echo "-- resources with mMTC dormant"; resources
    echo; echo "-- an IoT demand while dormant:"
    curl -s -X POST localhost:9111/request -H 'Content-Type: application/json' \
      -d '{"supi":"imsi-208930000000050","traffic_class":"sensor"}'; echo
    sleep 40
    echo; echo "-- activate"; $N activate mMTC
    echo; $N status
    sleep 30
    echo; echo "-- the IoT devices' own reports reach the data network again:"
    curl -s "$MMTC_KPI/metrics" | grep -E '^kpi_devices_active'
    echo; echo "-- the other slices throughout (URLLC collector run covering the whole experiment):"
    urllc_stats
  } 2>&1 | tee "$OUT/1-lifecycle.txt"
fi

# ─────────────────────────── 2. rollback ───────────────────────────
if [ -z "$ONLY" ] || [ "$ONLY" = rollback ]; then
  netorch
  ensure_active
  {
    echo "== experiment 2: an activation that misses its deadline is rolled back"
    $N deactivate mMTC | tail -1
    netorch ACTIVATE_DEADLINE=10
    echo; echo "-- activate with ACTIVATE_DEADLINE=10 s"
    $N activate mMTC
    echo; $N status
    echo "  mMTC containers running: $(docker ps --format '{{.Names}}' | grep -c 'mmtc')"
    netorch
    echo; echo "-- activate again with the normal deadline (150 s)"
    $N activate mMTC
    echo; $N status
  } 2>&1 | tee "$OUT/2-rollback.txt"
fi

# ─────────────────────────── 3. closed loop ───────────────────────────
if [ -z "$ONLY" ] || [ "$ONLY" = loop ]; then
  netorch
  ensure_active
  slice_orch
  netorch NETORCH_AUTO=1 IDLE_SECONDS=60 MIN_UP=30 PLAN_WINDOW=30 AUTO_INTERVAL=5
  gen() {   # <seconds> <subscriber SSTs> <label>
    ( cd tools/slice-orchestrator && CORE=open5gs DB_CONTAINER=o5gs-mongodb ORCH_URL=http://localhost:9111 \
        DURATION="$1" RATE=1 SUBSCRIBER_SSTS="$2" MIX="video:1 control:1" python3 traffic_gen.py ) \
      > "$OUT/3-demand-$3.txt" 2>&1
  }
  {
    echo "== experiment 3: closed loop on /plan (on-demand: mMTC, idle 60 s, plan window 30 s)"
    echo "-- A: phones only for 120 s (no IoT demand)"
    gen 120 "1 2" A-phones
    $N status | grep mMTC
    echo "-- B: IoT devices ask for service for 120 s"
    gen 120 "3" B-iot
    $N status | grep mMTC
    echo "-- C: phones only again for 150 s"
    gen 150 "1 2" C-phones
    $N status | grep mMTC
    echo
    echo "--- closed-loop decisions ---"
    curl -s localhost:9113/auto | python3 -c "import json,sys
for d in json.load(sys.stdin)['decisions']:
    print('  %s %-10s %-5s %s' % (d['t'], d['op'], d['slice'], d['reason']))"
    echo; echo "--- operations ---"
    curl -s localhost:9113/ops | python3 -c "import json,sys
for o in json.load(sys.stdin):
    print('  %s %-10s %-5s %-11s %5.1fs  %s' % (o['started'], o['op'], o['slice'], o['result'], o['seconds'], o['reason']))"
    echo; echo "--- IoT demands in phase B, in order (A = admitted, r = refused) ---"
    grep -E "ADMIT|REFUSE" "$OUT/3-demand-B-iot.txt" | awk '{ printf "%s", ($1 == "ADMIT" ? "A" : "r") } END { print "" }'
    grep -cE "ADMIT" "$OUT/3-demand-B-iot.txt" | sed 's/^/  admitted: /'
    grep -cE "REFUSE" "$OUT/3-demand-B-iot.txt" | sed 's/^/  refused:  /'
  } 2>&1 | tee "$OUT/3-closed-loop.txt"
  curl -s localhost:9113/ops > "$OUT/3-ops.json"
  netorch
  ensure_active
fi

# ─────────────────────────── 4. compute per slice ───────────────────────────
if [ -z "$ONLY" ] || [ "$ONLY" = compute ]; then
  netorch
  {
    echo "== experiment 4: eMBB's compute, set by the network orchestrator"
    echo "   the same load each time: TCP downlink, 60 s, 5 eMBB UEs (scripts/open5gs/traffic.sh)"
    for c in 0 1 0.5; do
      $N scale eMBB "$c" | tail -1
      sleep 5
      curl -s "$URLLC_KPI/run/start" >/dev/null
      tot=$(bash scripts/open5gs/traffic.sh 60 5 down embb 2>&1 | grep -A20 "eMBB" | grep total | head -1)
      echo "  cpus=$c  eMBB:${tot:-  (no result)}"
      echo "           $(urllc_stats)"
      sleep 10
    done
    $N scale eMBB 0 | tail -1
  } 2>&1 | tee "$OUT/4-compute.txt"
fi

curl -s localhost:9113/ops > "$OUT/ops-last-run.json"
echo
echo "evidence: $OUT"
