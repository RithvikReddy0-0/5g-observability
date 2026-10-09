#!/usr/bin/env bash
# controller_benchmark.sh — the control laws for eMBB's admission limit, compared on the live stack
# under the same seeded demand (ADR-018).
#
#   static   no controller: eMBB admits up to its 500 Mbps policy capacity
#   aimd     additive increase, multiplicative decrease (ADR-016; TCP's rule)
#   pi       PIE-style proportional-integral control on the latency error (RFC 8033)
#   mpc      one-step model-predictive control on an online least-squares model of latency vs load
#   ucb      UCB1 bandit over discrete limit levels (online learning, model-free)
#
# Every run: the same demand (SEED), the same duration and rate, the URLLC collector's run
# statistics, every executed flow, every controller action, and Prometheus series of URLLC p95
# and eMBB's limit. Host load before/after and leaked flow processes are recorded too.
#
# Output: docs/evidence/open5gs-algorithm-comparison/controller/  (OUT to override)
# Usage:  scripts/open5gs/controller_benchmark.sh [seconds=300] [demands-per-second=3]
#         POLICIES="static aimd pi mpc ucb" ROUNDS=1

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
D="${1:-300}"
RATE="${2:-3}"
POLICIES="${POLICIES:-static aimd pi mpc ucb}"
ROUNDS="${ROUNDS:-1}"
SEED="${SEED:-42}"
MIX="${MIX:-video:3 file:3 blog:1 control:1}"
OUT="${OUT:-docs/evidence/open5gs-algorithm-comparison/controller}"
mkdir -p "$OUT"
ORCH=http://localhost:9111
URLLC_KPI=http://10.53.0.52:9130
PROM=http://localhost:9091

load1() { cut -d' ' -f1 /proc/loadavg; }
leaks() { ps -eo etimes=,args= | awk '(/[i]perf3/ || /[p]ython3 - (send|recv)/) && $1 > 90' | wc -l; }

{
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git: $(git rev-parse --short HEAD) $(git diff --quiet || echo '(working tree modified)')"
  echo "seconds_per_run: $D  demands_per_second: $RATE  seed: $SEED  rounds: $ROUNDS"
  echo "mix: $MIX"
  echo "policies: $POLICIES"
  echo "ue_sessions: $(docker exec o5gs-ue ip -4 -o addr show | grep -c uesimtun)"
} >> "$OUT/meta.txt"
# FIRST_ROUND > 1 adds rounds to an earlier benchmark (e.g. the same policies in reverse order, so
# that what one run leaves behind on the host does not always favour the same policy).
FIRST_ROUND="${FIRST_ROUND:-1}"
[ "$FIRST_ROUND" = 1 ] && : > "$OUT/runs.txt"

for round in $(seq "$FIRST_ROUND" $((FIRST_ROUND + ROUNDS - 1))); do
  for p in $POLICIES; do
    label="$p-r$round"
    bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
    if [ "$p" = static ]; then ctl=0; else ctl=1; fi
    CONTROL=$ctl CONTROL_POLICY=$p FLOW_SECONDS=20 bash scripts/open5gs/start_orchestrator.sh >/dev/null || exit 1
    sleep 5
    curl -s "$URLLC_KPI/run/start" >/dev/null
    l0=$(load1); t0=$(date +%s)
    echo "== $label: ${D}s at ${RATE}/s, seed $SEED"
    ( cd tools/slice-orchestrator && CORE=open5gs DB_CONTAINER=o5gs-mongodb ORCH_URL="$ORCH" SEED="$SEED" \
        DURATION="$D" RATE="$RATE" MIX="$MIX" python3 traffic_gen.py ) > "$OUT/demand-$label.txt" 2>&1
    curl -s "$URLLC_KPI/run/stats" > "$OUT/urllc-kpi-$label.json"
    t1=$(date +%s)
    sleep 30                                  # the last flows finish
    curl -s "$ORCH/flows" > "$OUT/flows-$label.json"
    curl -s "$ORCH/control" > "$OUT/control-$label.json"
    for q in 'kpi_owd_recent_ms{slice="URLLC",stat="p95"}' 'slice_admission_limit_mbps{slice="eMBB"}' \
             'slice_measured_mbps{slice="eMBB"}'; do
      name=$(echo "$q" | sed 's/[{].*//')
      curl -s --get "$PROM/api/v1/query_range" --data-urlencode "query=$q" \
        --data-urlencode "start=$t0" --data-urlencode "end=$t1" --data-urlencode "step=5" > "$OUT/series-$label-$name.json"
    done
    echo "$label $p $t0 $t1 load_before=$l0 load_after=$(load1) leaked=$(leaks)" >> "$OUT/runs.txt"
    echo "   $(grep -c ADMIT "$OUT/demand-$label.txt") admitted, $(grep -c REFUSE "$OUT/demand-$label.txt") refused"
    sleep 20
  done
done

python3 tools/slice-orchestrator/compare_runs.py "$OUT" "$D"
bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
bash scripts/open5gs/start_orchestrator.sh >/dev/null
echo "evidence: $OUT"
