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
# Also reports UDP receive-buffer drops in every gNB container per phase. Those drops were
# the measured coupling on the shared-gNB topology: the eMBB flood overflowed the one gNB's
# socket and URLLC packets were dropped with it (docs/open5gs.md, "Slice isolation").
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

gnbs() { docker ps --format '{{.Names}}' | grep -E '^o5gs-gnb' | sort; }

rcvbuf_errors() {   # <container> -> RcvbufErrors from the container's own network namespace
  docker exec "$1" awk '/^Udp:/ && ++n == 2 {print $6}' /proc/net/snmp 2>/dev/null
}

snapshot() {   # -> "name=value name=value"
  for g in $(gnbs); do printf '%s=%s ' "$g" "$(rcvbuf_errors "$g")"; done
}

delta() {   # <before> <after>
  for g in $(gnbs); do
    b=$(echo "$1" | tr ' ' '\n' | sed -n "s/^$g=//p")
    a=$(echo "$2" | tr ' ' '\n' | sed -n "s/^$g=//p")
    printf '%s +%s  ' "${g#o5gs-}" "$(( ${a:-0} - ${b:-0} ))"
  done
}

phase() {   # <label> <start> <end> <gnb-drop-delta>
  local w=$(( $3 - $2 ))
  printf '  %-7s' "$1"
  for sl in URLLC eMBB; do
    printf ' %-5s avg %5s ms  worst %6s ms  min reach %5s/10 |' "$sl" \
      "$(q "avg_over_time(ue_probe_rtt_ms{slice=\"$sl\",stat=\"avg\"}[${w}s])" "$3")" \
      "$(q "max_over_time(ue_probe_rtt_ms{slice=\"$sl\",stat=\"max\"}[${w}s])" "$3")" \
      "$(q "min_over_time(ue_probe_reachable{slice=\"$sl\"}[${w}s])" "$3")"
  done
  printf ' eMBB DL peak %s Mbps | gNB UDP drops: %s\n' \
    "$(q "max_over_time((sum(rate(upf_slice_downlink_bytes_total{slice=\"eMBB\"}[15s])) * 8 / 1e6)[${w}s:5s])" "$3")" \
    "$4"
}

echo "== topology: $(gnbs | wc -l) gNB(s): $(gnbs | tr '\n' ' ')"
echo "== phase 1: idle, ${T}s"
d1=$(snapshot); s1=$(date +%s); sleep "$T"; e1=$(date +%s); d1b=$(snapshot)

echo "== phase 2: eMBB saturated (3 downlink flows), URLLC idle, ${T}s"
d2=$(snapshot); s2=$(date +%s)
bash "$SCRIPT_DIR/traffic.sh" "$T" 3 down embb | sed 's/^/     /'
e2=$(date +%s); d2b=$(snapshot)

sleep 8   # let the last scrapes of phase 2 land
echo
echo "== result (Prometheus, per phase window)"
phase idle   "$s1" "$e1" "$(delta "$d1" "$d1b")"
phase loaded "$s2" "$e2" "$(delta "$d2" "$d2b")"
