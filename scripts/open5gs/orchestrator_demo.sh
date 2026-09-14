#!/usr/bin/env bash
# orchestrator_demo.sh — demand-based slice allocation on real traffic, in two experiments.
#
#   1. saturation     demands arrive faster than both slices can hold. Expect refusals on
#                     both slices, and admitted flows that still receive their rate.
#   2. measured load  eMBB is flooded by traffic the orchestrator never admitted. A video
#                     demand must be refused on measured load alone, then admitted again
#                     once the flood stops. Without measurement it would have been accepted.
#
# Output: docs/evidence/open5gs-orchestrator-<UTC timestamp>/
# Usage:  scripts/open5gs/orchestrator_demo.sh [saturation-seconds=120] [demands-per-second=3]
#         ONLY_SATURATION=1 skips experiment 2.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
D="${1:-120}"
RATE="${2:-3}"   # 2/s peaked at 182/200 Mbps eMBB and refused nothing; 3/s saturates both slices
OUT="docs/evidence/open5gs-orchestrator-$(date -u +%Y%m%d-%H%M%SZ)"
mkdir -p "$OUT"
ORCH=http://localhost:9111

bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
bash scripts/open5gs/start_orchestrator.sh || exit 1   # fresh counters for the evidence

req() {   # <supi> <class> -> one line: ADMIT/REFUSE ...
  curl -s -X POST "$ORCH/request" -H 'Content-Type: application/json' \
    -d "{\"supi\":\"$1\",\"traffic_class\":\"$2\"}" | python3 -c "import json,sys
r = json.load(sys.stdin)
if r.get('admitted'):
    print('ADMIT  %s -> %s  admitted %.1f / measured %.1f / capacity %.0f Mbps' % (
        r['traffic_class'], r['slice']['name'], r['slice_used_mbps'], r['slice_measured_mbps'], r['slice_capacity_mbps']))
else:
    print('REFUSE %s' % r.get('reason'))"
}

metric_snapshot() {
  curl -s "$ORCH/metrics" | grep -E '^slice_(admitted_total|rejected_total|flows_completed_total|flow_delivery_ratio|flow_start_failed)' | sort
}

echo "== experiment 1: saturation, ${D}s at ${RATE} demands/s, flows of 20 s" | tee "$OUT/1-saturation.txt"
( cd tools/slice-orchestrator && CORE=open5gs DB_CONTAINER=o5gs-mongodb ORCH_URL="$ORCH" \
    DURATION="$D" RATE="$RATE" MIX="video:3 file:3 blog:3 control:3" python3 traffic_gen.py ) \
  >> "$OUT/1-saturation.txt" 2>&1
echo "   waiting for the last flows to finish" | tee -a "$OUT/1-saturation.txt"
sleep 35
{
  echo; echo "--- orchestrator counters ---"; metric_snapshot
  echo; echo "--- every executed flow: requested vs delivered ---"
  curl -s "$ORCH/flows" | python3 -c "import json,sys,collections
f = json.load(sys.stdin)
by = collections.defaultdict(list)
for r in f: by[(r['slice'], r['cls'])].append(r)
print('%-6s %-8s %5s %5s %5s %12s %12s %9s' % ('slice','class','flows','met','short','requested','delivered','max loss'))
for (sl, c), rs in sorted(by.items()):
    print('%-6s %-8s %5d %5d %5d %9.1f Mb %9.1f Mb %8.2f%%' % (sl, c, len(rs),
        sum(r['outcome']=='met' for r in rs), sum(r['outcome']!='met' for r in rs),
        sum(r['requested_mbps'] for r in rs), sum(r['delivered_mbps'] for r in rs),
        max(r['loss_percent'] for r in rs)))
print('TOTAL  flows %d, met %d (%.1f%%)' % (len(f), sum(r['outcome']=='met' for r in f),
      100.0 * sum(r['outcome']=='met' for r in f) / max(1, len(f))))"
  echo; echo "--- peak concurrent load per slice during the run (Prometheus) ---"
  for m in slice_allocated_mbps slice_measured_mbps; do
    curl -s --get http://localhost:9091/api/v1/query \
      --data-urlencode "query=max_over_time(${m}[$((D + 60))s])" | python3 -c "import json,sys
for r in json.load(sys.stdin)['data']['result']:
    print('  peak %-22s %-6s %6.1f Mbps' % ('$m', r['metric']['slice'], float(r['value'][1])))"
  done
} >> "$OUT/1-saturation.txt" 2>&1
tail -25 "$OUT/1-saturation.txt"

[ "${ONLY_SATURATION:-0}" = 1 ] && { echo "evidence: $OUT"; exit 0; }
echo
echo "== experiment 2: unmanaged load on eMBB, then a video demand" | tee "$OUT/2-measured-load.txt"
{
  echo "-- before the flood:"
  req imsi-208930000000004 video
  sleep 22                                  # let that flow end so admitted load is ~0
  echo "-- flooding eMBB with traffic the orchestrator never admitted (TCP, 3 devices, 70 s)"
  bash scripts/open5gs/traffic.sh 70 3 down embb > "$OUT/2-flood-traffic.txt" 2>&1 &
  FLOOD=$!
  sleep 30
  echo "-- during the flood (admitted load on eMBB is ~0):"
  curl -s "$ORCH/metrics" | grep -E '^slice_(allocated|measured)_mbps\{.*eMBB'
  for i in 1 2 3; do req imsi-20893000000000$i video; req imsi-20893000000000$i file; done
  wait "$FLOOD"
  sleep 25                                  # the 15 s rate window drains
  echo "-- after the flood:"
  curl -s "$ORCH/metrics" | grep -E '^slice_(allocated|measured)_mbps\{.*eMBB'
  req imsi-208930000000005 video
  echo; sed -n '/eMBB/,/total/p' "$OUT/2-flood-traffic.txt"
} >> "$OUT/2-measured-load.txt" 2>&1
cat "$OUT/2-measured-load.txt"
echo
echo "evidence: $OUT"
