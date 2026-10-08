#!/usr/bin/env bash
# capture_orchestrator_screens.sh — screenshots of the Phase 2b orchestrator panels with live data.
#
# One scripted scenario, then every new panel captured over exactly its time window, so the
# screenshots show the same events:
#   t+0     saturating demand for 200 s (3/s, video/file/blog/control), feedback controller on
#   t+30    the network orchestrator deactivates mMTC
#   t+100   ... and activates it again
#   t+210   priority: URLLC filled by standard devices, then critical devices pre-empt them
# Same capture method as capture_screenshots.sh: headless Chromium in a throwaway container on
# the stack's network, Grafana's single-panel view (d-solo), anonymous viewer access.
#
# Output: docs/screenshots/open5gs/1x-*.png        Usage: scripts/open5gs/capture_orchestrator_screens.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
OUT="${OUT:-$REPO_ROOT/docs/screenshots/open5gs}"
IMAGE="${CHROME_IMAGE:-zenika/alpine-chrome:latest}"
BUDGET="${BUDGET:-25000}"
mkdir -p "$OUT"
N="python3 tools/network-orchestrator/netorch.py"
ORCH=http://localhost:9111

shot() {  # shot <file> <width> <height> <url>
  docker run --rm --network o5gs_net -v "$OUT:/out" --entrypoint chromium-browser "$IMAGE" \
    --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size="$2,$3" --virtual-time-budget="$BUDGET" \
    --screenshot="/out/$1" "$4" >/dev/null 2>&1
  if [ -s "$OUT/$1" ]; then printf '  [ OK ] %-44s %s\n' "$1" "$(du -h "$OUT/$1" | cut -f1)"
  else printf '  [FAIL] %-44s\n' "$1"; fi
}
req() {
  curl -s -X POST "$ORCH/request" -H 'Content-Type: application/json' \
    -d "{\"supi\":\"$1\",\"traffic_class\":\"$2\"}" >/dev/null
}

echo "== fresh orchestrators (slice orchestrator with its controller, network orchestrator)"
bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
CONTROL=1 FLOW_SECONDS=20 bash scripts/open5gs/start_orchestrator.sh >/dev/null || exit 1
bash scripts/open5gs/start_netorch.sh >/dev/null || exit 1
$N status | grep -q "^mMTC   ACTIVE" || $N activate mMTC >/dev/null

T0=$(date +%s)
echo "== scenario starts $(date -u +%H:%M:%SZ)"
( cd tools/slice-orchestrator && CORE=open5gs DB_CONTAINER=o5gs-mongodb ORCH_URL="$ORCH" \
    DURATION=200 RATE=3 MIX="video:3 file:3 blog:1 control:1" python3 traffic_gen.py ) > /tmp/shots-demand.txt 2>&1 &
GEN=$!
sleep 30
echo "-- deactivate mMTC";  $N deactivate mMTC | tail -1
sleep 40
echo "-- activate mMTC";    $N activate mMTC | tail -1
wait "$GEN"
echo "-- demand: $(grep -c ADMIT /tmp/shots-demand.txt) admitted, $(grep -c REFUSE /tmp/shots-demand.txt) refused"
sleep 25
echo "-- priority: 20 standard URLLC demands, then 5 critical ones"
bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
CONTROL=1 FLOW_SECONDS=40 bash scripts/open5gs/start_orchestrator.sh >/dev/null
for i in $(seq 0 19); do req "imsi-2089300000000$((24 + i % 7))" control; done
for i in 1 2 3 1 2; do req "imsi-20893000000002$i" control; done
req imsi-208930000000025 control
sleep 70                                     # flows end, Prometheus scrapes the counters
T1=$(date +%s)
FROM=$(( (T0 - 30) * 1000 )); TO=$(( (T1 + 10) * 1000 ))
echo "== scenario window $(( T1 - T0 ))s; capturing into $OUT"

G="http://10.53.0.42:3000/d-solo/open5gs-slices/open5gs?orgId=1&from=$FROM&to=$TO&panelId"
shot 10-embb-capacity-limit-admitted-measured.png 1200 520 "$G=31"
shot 11-urllc-capacity-limit-admitted-measured.png 1200 520 "$G=32"
shot 12-controller-watches-urllc-p95.png          1200 520 "$G=38"
shot 13-demands-by-arp-priority.png               1200 520 "$G=39"
shot 14-slice-active-or-dormant.png               1200 520 "$G=44"
shot 15-ues-and-core-sessions-per-slice.png       1200 520 "$G=45"
shot 16-stat-preemptions.png                       520 260 "$G=40"
shot 17-stat-controller-cuts.png                   520 260 "$G=41"
shot 18-stat-lifecycle-operations.png              520 260 "$G=46"
shot 19-stat-last-activation-seconds.png           520 260 "$G=48"
shot 20-flows-that-got-their-rate.png              520 260 "$G=33"
PROM="http://10.53.0.41:9090"
shot 21-prometheus-netorch-target-up.png          1600 1000 "$PROM/targets?search=orchestrator"
echo "$T0 $T1" > "$OUT/.orchestrator-window"
echo "done"
