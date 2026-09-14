#!/usr/bin/env bash
# verify_user_plane.sh — prove user traffic flows through the Open5GS UPF, per slice.
#
# On the free5GC stack this could not run at all: its UPF needs the gtp5g kernel module,
# which WSL2 cannot load. Each check below sends real packets from a UE's own TUN device,
# through the gNB as GTP-U, into the UPF and out of that slice's TUN device.
#
# Evidence is taken from both ends: the UE side (ping replies, iperf3 throughput) and the
# UPF side (byte counters on ogstun / ogstun2). A reply alone could in principle come from
# somewhere else; counters moving on the slice's own UPF device cannot.
#
# Usage: scripts/open5gs/verify_user_plane.sh

set -uo pipefail

UE=o5gs-ue
UPF=o5gs-upf
pass=0; fail=0; warn=0
ok()   { printf '  [ PASS ] %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  [ FAIL ] %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  [ WARN ] %s\n' "$1"; warn=$((warn+1)); }

ue()  { docker exec "$UE"  sh -c "$1"; }
upf() { docker exec "$UPF" sh -c "$1"; }

# First UE interface holding an address in the given pool.
tun_for() { ue "ip -4 -o addr show | awk '/uesimtun/ && \$4 ~ /^$1/ {print \$2, \$4; exit}'" | tr -d '\r'; }
rx_bytes() { upf "cat /sys/class/net/$1/statistics/rx_bytes" | tr -d '\r'; }

echo "==================================================================="
echo "Open5GS user-plane verification — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==================================================================="

for c in "$UE" "$UPF"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "error: $c is not running (make o5gs-up)"; exit 2; }
done

sessions=$(ue "ip -4 -o addr show | grep -c uesimtun" | tr -d '\r')
echo
echo "-- PDU sessions --"
if [ "${sessions:-0}" -ge 20 ]; then ok "$sessions UEs hold a PDU session address"
elif [ "${sessions:-0}" -gt 0 ]; then no "only $sessions of 20 UEs hold a PDU session address"
else no "no UE has a PDU session address — nothing to test"; exit 1; fi

for slice in "A eMBB 10.45. 10.45.0.1 ogstun" "B URLLC 10.46. 10.46.0.1 ogstun2"; do
  set -- $slice
  key=$1; name=$2; pool=$3; gw=$4; dev=$5

  echo
  echo "-- slice $key ($name): pool ${pool}0.0/16, UPF device $dev --"
  read -r tun cidr <<<"$(tun_for "$pool")"
  if [ -z "${tun:-}" ]; then no "no UE on slice $key has an address"; continue; fi
  ip=${cidr%/*}
  ok "UE interface $tun has $ip (address came from the slice's own pool)"

  before=$(rx_bytes "$dev")
  out=$(ue "ping -I $tun -c 5 -i 0.2 -W 2 $gw" 2>&1)
  after=$(rx_bytes "$dev")
  loss=$(echo "$out" | grep -oE '[0-9.]+% packet loss' | cut -d% -f1)
  rtt=$(echo "$out" | grep -oE 'rtt [^=]+= [0-9./]+' | awk -F'[=/ ]+' '{print $(NF-2)}')

  if [ "${loss:-100}" = "0" ]; then ok "ping $gw via $tun: 5/5 replies, avg rtt ${rtt:-?} ms"
  else no "ping $gw via $tun: ${loss:-100}% loss"; echo "$out" | tail -3 | sed 's/^/           /'; fi

  if [ "$after" -gt "$before" ]; then ok "UPF $dev received $((after - before)) bytes during the ping — traffic went through the UPF"
  else no "UPF $dev counters did not move — replies did not come through the user plane"; fi

  # Throughput through the slice, measured from the UE side.
  # pkill -x (exact process name), never pkill -f: the pattern would match this very
  # `sh -c` command line, and pkill killed its own shell before the server ever started.
  upf "pkill -x iperf3 2>/dev/null; sleep 0.3; iperf3 -s -B $gw -D --one-off" >/dev/null 2>&1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    upf "ss -ltn | grep -q '$gw:5201'" && break; sleep 0.2
  done
  mbps=$(ue "iperf3 -c $gw -B $ip -t 3 -J 2>/dev/null" \
         | python3 -c "import json,sys
try: print('%.1f' % (json.load(sys.stdin)['end']['sum_received']['bits_per_second']/1e6))
except Exception: print('')" 2>/dev/null)
  if [ -n "$mbps" ]; then ok "iperf3 through slice $key: $mbps Mbps"
  else note "iperf3 through slice $key did not complete (ping already proved the path)"; fi
done

echo
echo "-- beyond the core --"
read -r tun cidr <<<"$(tun_for 10.45.)"
if [ -n "${tun:-}" ] && ue "ping -I $tun -c 3 -W 3 8.8.8.8" >/dev/null 2>&1; then
  ok "UE on slice A reached 8.8.8.8 through the UPF's NAT (real internet egress)"
else
  note "internet egress from a UE failed — the core-internal path above is unaffected"
fi

echo
echo "==================================================================="
echo "  PASS=$pass  FAIL=$fail  WARN=$warn"
echo "==================================================================="
[ "$fail" -eq 0 ]
