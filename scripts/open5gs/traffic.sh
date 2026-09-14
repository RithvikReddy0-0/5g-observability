#!/usr/bin/env bash
# traffic.sh — push real traffic through both slices at once and report what each carried.
#
# Every flow is TCP from a UE's own PDU-session interface, through the gNB as GTP-U, into the
# UPF and out of that slice's TUN device. Nothing is modelled: the throughput is what the
# network delivered, and the gNB enforces each session's AMBR (200 Mbps eMBB, 20 Mbps URLLC).
#
# Usage: scripts/open5gs/traffic.sh [seconds=60] [ues-per-slice=3] [down|up] [both|embb|urllc]
#
# Loading ONE slice while the other carries only probes is the isolation test
# (scripts/open5gs/isolation_test.sh).
#
# Watch it live: Grafana http://localhost:3001 → "Open5GS — slices, sessions and traffic".

set -uo pipefail

T="${1:-60}"
N="${2:-3}"
DIR="${3:-down}"
ONLY="${4:-both}"
UE=o5gs-ue
UPF=o5gs-upf
[ "$DIR" = "down" ] && R="-R" || R=""

for c in "$UE" "$UPF"; do
  [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ] \
    || { echo "error: $c is not running (make o5gs-up)"; exit 1; }
done

addrs() {   # <pool-regex> -> "iface ip" lines
  docker exec "$UE" ip -4 -o addr show | awk -v p="$1" '/uesimtun/ && index($4, p) == 1 {split($4, a, "/"); print $2, a[1]}' | head -n "$N"
}

mapfile -t A < <(addrs '10.45.')
mapfile -t B < <(addrs '10.46.')
[ "${#A[@]}" -ge "$N" ] && [ "${#B[@]}" -ge "$N" ] \
  || { echo "error: need $N UEs with sessions on each slice (have ${#A[@]} eMBB, ${#B[@]} URLLC)"; exit 1; }
case "$ONLY" in
  both)  ;;
  embb)  B=() ;;
  urllc) A=() ;;
  *) echo "error: slice selector must be both, embb or urllc"; exit 1 ;;
esac

docker exec "$UPF" sh -c 'pkill -x iperf3 2>/dev/null; true'
port=5300
jobs=()
start() {   # <slice> <gateway> <ue-ip>
  docker exec "$UPF" iperf3 -s -B "$2" -p "$port" -D --one-off
  # Wait until it is actually listening: starting clients on a fixed sleep lost one of six
  # flows in an early run with "connection refused".
  for _ in $(seq 25); do
    docker exec "$UPF" sh -c "ss -ltn | grep -q '$2:$port'" && break; sleep 0.2
  done
  jobs+=("$1 $3 $port $2")
  port=$((port + 1))
}
for l in ${A[@]+"${A[@]}"}; do set -- $l; start eMBB 10.45.0.1 "$2"; done
for l in ${B[@]+"${B[@]}"}; do set -- $l; start URLLC 10.46.0.1 "$2"; done
sleep 1

echo "== ${DIR}link, ${T}s, $N UEs on each loaded slice ($ONLY) — flows run simultaneously"
tmp=$(mktemp -d)
for j in "${jobs[@]}"; do
  set -- $j
  docker exec "$UE" iperf3 -c "$4" -B "$2" -p "$3" -t "$T" $R -J > "$tmp/$1-$2.json" 2>/dev/null &
done
wait

python3 - "$tmp" "$T" <<'PY'
import glob, json, os, sys
d, T = sys.argv[1], int(sys.argv[2])
ambr = {"eMBB": 200, "URLLC": 20}
rows = {}
for f in sorted(glob.glob(os.path.join(d, "*.json"))):
    sl, ip = os.path.basename(f)[:-5].split("-", 1)
    try:
        mbps = json.load(open(f))["end"]["sum_received"]["bits_per_second"] / 1e6
    except Exception:
        mbps = None
    rows.setdefault(sl, []).append((ip, mbps))
for sl in ("eMBB", "URLLC"):
    got = [m for _, m in rows.get(sl, []) if m is not None]
    print(f"\n  {sl}  (session AMBR {ambr[sl]} Mbps)")
    for ip, m in rows.get(sl, []):
        print(f"    UE {ip:<12} {'failed' if m is None else '%7.1f Mbps' % m}")
    if got:
        per = sum(got) / len(got)
        print(f"    total {sum(got):7.1f} Mbps   mean per session {per:6.1f} Mbps   "
              f"({per / ambr[sl] * 100:.0f}% of AMBR over {T}s)")
PY
rm -rf "$tmp"
docker exec "$UPF" sh -c 'pkill -x iperf3 2>/dev/null; true'
