#!/usr/bin/env bash
# calibrate_capacity.sh — how much UDP traffic can each slice DELIVER on this deployment?
#
# slices.env gives each slice a capacity from policy (eMBB 200 Mbps, URLLC 20 Mbps). Whether
# the hardware can carry that is a separate question, and admitting against a number the
# network cannot deliver hands every admitted flow packet loss. Measured: with admission at
# the 200 Mbps policy, 0 of 78 eMBB UDP flows received their rate.
#
# This steps the aggregate downlink rate up across all of a slice's devices at once (one UDP
# flow per device, the rate split evenly) and reports the highest step the slice delivered:
# >= 95 % of the offered rate with <= 2 % loss. The orchestrator can then admit against
# min(policy, deliverable) — CAPACITY_OVERRIDE in scripts/open5gs/start_orchestrator.sh.
#
# Usage: scripts/open5gs/calibrate_capacity.sh [seconds-per-step=15]
# Stop the orchestrator's own traffic first; the calibration needs the slices to itself.

set -uo pipefail
T="${1:-15}"
UE=o5gs-ue

steps() { case $1 in eMBB) echo "40 80 120 160 200 240 280" ;; URLLC) echo "5 10 15 20" ;; esac; }

run_step() {   # <slice> <pool-prefix> <dn-ip> <dn-container> <total-mbps> -> "delivered loss"
  local slice=$1 prefix=$2 gw=$3 upf=$4 total=$5   # gw/upf: the slice DATA NETWORK, not the UPF (ADR-012)
  mapfile -t devs < <(docker exec "$UE" ip -4 -o addr show | awk -v p="$prefix" '/uesimtun/ && index($4, p) == 1 {split($4, a, "/"); print a[1]}')
  local n=${#devs[@]} per port=5800 tmp
  per=$(python3 -c "print('%.3f' % ($total / $n))")
  tmp=$(mktemp -d)
  docker exec "$upf" sh -c 'pkill -x iperf3 2>/dev/null; true'
  for i in "${!devs[@]}"; do
    docker exec "$upf" iperf3 -s -B "$gw" -p $((port + i)) -1 -D
  done
  for i in "${!devs[@]}"; do
    for _ in $(seq 25); do docker exec "$upf" sh -c "ss -ltn | grep -q '$gw:$((port + i))'" && break; sleep 0.2; done
  done
  for i in "${!devs[@]}"; do
    docker exec "$UE" iperf3 -u -c "$gw" -B "${devs[$i]}" -p $((port + i)) -b "${per}M" -t "$T" -R -J \
      > "$tmp/$i.json" 2>/dev/null &
  done
  wait
  python3 - "$tmp" "$total" <<'PY'
import glob, json, sys
d, total = sys.argv[1], float(sys.argv[2])
bps = lost = pkts = 0
for f in glob.glob(d + "/*.json"):
    try:
        s = json.load(open(f))["end"]["sum"]
        # In -R mode bits_per_second is the SENDER's rate; what arrived is that minus the loss
        # the receiver counted. Reporting the raw figure overstated delivery at 8 % loss.
        bps += s["bits_per_second"] * (1 - s.get("lost_percent", 0.0) / 100.0)
        lost += s.get("lost_packets", 0); pkts += s.get("packets", 0)
    except Exception:
        pass
print("%.1f %.2f" % (bps / 1e6, 100.0 * lost / pkts if pkts else 100.0))
PY
  rm -rf "$tmp"
}

declare -A best
echo "calibration: ${T}s per step, one UDP downlink flow per device, rate split evenly"
for spec in "eMBB 10.45. 10.53.0.51 o5gs-dn-embb" "URLLC 10.46. 10.53.0.52 o5gs-dn-urllc"; do
  set -- $spec
  best[$1]=0
  echo
  printf '  %-6s %10s %12s %8s  %s\n' slice offered delivered loss verdict
  for total in $(steps "$1"); do
    read -r got loss <<<"$(run_step "$1" "$2" "$3" "$4" "$total")"
    ok=$(python3 -c "print(int($got >= 0.95 * $total and $loss <= 2.0))")
    printf '  %-6s %6s Mbps %8s Mbps %7s%%  %s\n' "$1" "$total" "$got" "$loss" "$([ "$ok" = 1 ] && echo delivered || echo NOT delivered)"
    [ "$ok" = 1 ] && best[$1]=$total || break
    sleep 3
  done
done
echo
echo "deliverable capacity: eMBB=${best[eMBB]} URLLC=${best[URLLC]}"
echo "CAPACITY_OVERRIDE=\"eMBB=${best[eMBB]} URLLC=${best[URLLC]}\""
