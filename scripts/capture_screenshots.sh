#!/usr/bin/env bash
# capture_screenshots.sh — render PNG screenshots of the live dashboards.
#
# Uses headless Chromium in a throwaway container attached to the core's compose network, so
# it works without a desktop session, an X server, or a browser on the host. Grafana runs
# with anonymous viewer access, so no credentials are involved.
#
# Prerequisites: the core, Prometheus/Grafana and the slice exporter must be running —
# see docs/runbooks/clean-install.md. For figures that match the documented numbers,
# re-provision subscribers and relaunch the UEs first: the AMF gauges only agree with ground
# truth right after a clean cycle (observability/README.md).
#
# Usage: scripts/capture_screenshots.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
OUT="${OUT:-$REPO_ROOT/docs/screenshots}"
NETWORK="${NETWORK:-compose_privnet}"
IMAGE="${CHROME_IMAGE:-zenika/alpine-chrome:latest}"
BUDGET="${BUDGET:-25000}"   # ms of virtual time — Grafana needs to render its panels

mkdir -p "$OUT"

shot() {  # shot <file> <width> <height> <url>
  local file="$1" w="$2" h="$3" url="$4"
  docker run --rm --network "$NETWORK" -v "$OUT:/out" \
    --entrypoint chromium-browser "$IMAGE" \
    --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size="$w,$h" --virtual-time-budget="$BUDGET" \
    --screenshot="/out/$file" "$url" >/dev/null 2>&1
  if [ -s "$OUT/$file" ]; then
    printf '  [ OK ] %-34s %s\n' "$file" "$(du -h "$OUT/$file" | cut -f1)"
  else
    printf '  [FAIL] %-34s (is the service running?)\n' "$file"
  fi
}

GRAFANA="http://grafana:3000/d/free5gc-overview/free5gc-e28094-control-plane-overview"
PROM="http://prometheus:9090"

echo "==================================================================="
echo "capturing screenshots into $OUT"
echo "==================================================================="

shot 01-grafana-dashboard.png       1600 1750 "$GRAFANA?kiosk&from=now-1h&to=now"
shot 02-prometheus-targets.png      1600 1200 "$PROM/targets"
shot 03-prometheus-slice-metrics.png 1600 900 "$PROM/graph?g0.expr=free5gc_slice_provisioned_subscribers&g0.tab=1"
shot 04-grafana-slice-row.png       1600  700 "$GRAFANA?kiosk&viewPanel=10&from=now-1h&to=now"
shot 05-prometheus-nf-up.png        1600  900 "$PROM/graph?g0.expr=up%7Bjob%3D%22free5gc%22%7D&g0.tab=1"
shot 06-webui-login.png             1400  900 "http://webui:5000"

echo
echo "done — $(ls -1 "$OUT"/*.png 2>/dev/null | wc -l | tr -d ' ') PNG(s) in $OUT"
