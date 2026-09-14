#!/usr/bin/env bash
# capture_screenshots.sh — PNG screenshots of the Open5GS dashboards, taken under real load.
#
# Same approach as scripts/capture_screenshots.sh (headless Chromium in a throwaway container
# on the stack's own network, Grafana with anonymous viewer access), pointed at the Open5GS
# stack. Traffic is started first so the per-slice panels show real throughput, not flat lines.
#
# Usage: scripts/open5gs/capture_screenshots.sh [traffic-seconds=100]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
OUT="${OUT:-$REPO_ROOT/docs/screenshots/open5gs}"
IMAGE="${CHROME_IMAGE:-zenika/alpine-chrome:latest}"
BUDGET="${BUDGET:-25000}"
T="${1:-100}"
mkdir -p "$OUT"

shot() {  # shot <file> <width> <height> <url>
  docker run --rm --network o5gs_net -v "$OUT:/out" --entrypoint chromium-browser "$IMAGE" \
    --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size="$2,$3" --virtual-time-budget="$BUDGET" \
    --screenshot="/out/$1" "$4" >/dev/null 2>&1
  if [ -s "$OUT/$1" ]; then printf '  [ OK ] %-36s %s\n' "$1" "$(du -h "$OUT/$1" | cut -f1)"
  else printf '  [FAIL] %-36s\n' "$1"; fi
}

GRAFANA="http://10.53.0.42:3000/d/open5gs-slices/open5gs-e28094-slices-sessions-and-traffic"
PROM="http://10.53.0.41:9090"

echo "== starting ${T}s of traffic on both slices in the background"
bash "$SCRIPT_DIR/traffic.sh" "$T" 3 down both > /tmp/o5gs-shot-traffic.txt 2>&1 &
TRAFFIC=$!
sleep 45   # let throughput build up across several scrapes

echo "== capturing into $OUT"
shot 01-dashboard-under-load.png       1600 2300 "$GRAFANA?kiosk&from=now-10m&to=now"
shot 02-prometheus-targets.png         1600 1000 "$PROM/targets"
shot 03-prometheus-sessions-by-slice.png 1600 800 "$PROM/graph?g0.expr=fivegs_smffunction_sm_sessionnbr&g0.tab=1"
shot 04-prometheus-probe-by-slice.png  1600 900  "$PROM/graph?g0.expr=ue_probe_rtt_ms&g0.tab=1"

wait "$TRAFFIC"
echo "== traffic result"
sed 's/^/  /' /tmp/o5gs-shot-traffic.txt
