#!/usr/bin/env bash
# isolation_test.sh — does heavy traffic on one slice degrade the other?
#
# The question slicing exists to answer. Two phases of equal length, measured the same way:
#   1. idle      — no user traffic
#   2. loaded    — eMBB saturated by 3 downlink flows; URLLC carries only the probes
# URLLC reachability and round-trip time come from ue_probe_exporter (every UE, every 5 s),
# read back from Prometheus over each phase's own window, so both phases use identical
# instruments and the comparison is like for like.
#
# Usage: scripts/open5gs/isolation_test.sh [seconds-per-phase=60]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
T="${1:-60}"
PROM="${PROMETHEUS_URL:-http://localhost:9091}"

q() {   # <promql> <eval-unix-time>
  curl -s --get "$PROM/api/v1/query" --data-urlencode "query=$1" --data-urlencode "time=$2" \
    | python3 -c "import json,sys
r = json.load(sys.stdin)['data']['result']
print('%.2f' % float(r[0]['value'][1]) if r else 'n/a')"
}

phase() {   # <label> <start> <end>
  local w=$(( $3 - $2 ))
  printf '  %-8s' "$1"
  for sl in URLLC eMBB; do
    printf '  %-5s rtt avg %6s ms  worst %6s ms  min reachable %4s/10 |' "$sl" \
      "$(q "avg_over_time(ue_probe_rtt_ms{slice=\"$sl\",stat=\"avg\"}[${w}s])" "$3")" \
      "$(q "max_over_time(ue_probe_rtt_ms{slice=\"$sl\",stat=\"max\"}[${w}s])" "$3")" \
      "$(q "min_over_time(ue_probe_reachable{slice=\"$sl\"}[${w}s])" "$3")"
  done
  printf '  eMBB DL peak %s Mbps\n' \
    "$(q "max_over_time((sum(rate(upf_slice_downlink_bytes_total{slice=\"eMBB\"}[15s])) * 8 / 1e6)[${w}s:5s])" "$3")"
}

echo "== phase 1: idle, ${T}s"
s1=$(date +%s); sleep "$T"; e1=$(date +%s)

echo "== phase 2: eMBB saturated (3 downlink flows), URLLC idle, ${T}s"
s2=$(date +%s)
bash "$SCRIPT_DIR/traffic.sh" "$T" 3 down embb | sed 's/^/     /'
e2=$(date +%s)

sleep 8   # let the last scrapes of phase 2 land
echo
echo "== result (Prometheus, per phase window)"
phase idle   "$s1" "$e1"
phase loaded "$s2" "$e2"
