#!/usr/bin/env bash
# scale_test.sh — what attaching many UEs costs this system, and where this laptop stops.
#
# Phase 2b, Track 1 (docs/briefs/phase2b-work-division.md): the stack holds 100 UEs across three
# slices; this measures what that scale costs.
#
#   Part 1  attach burst   the same 100 UEs attached START_BATCH at a time (1, 10, 25, 100 by
#                          default): time until all 100 have a working session, per-UE
#                          registration and PDU-session latency, registration failures, and the
#                          peak CPU of every control-plane NF while it happens.
#   Part 2  scale steps    more mMTC devices on top of eMBB 20 + URLLC 10 (100, 150, 200, 300 UEs
#                          by default): does every UE attach, and what does holding them cost at
#                          rest — CPU and memory per container, host memory left, how long one
#                          liveness probe round takes. Stops early if a step fails to attach or
#                          the host has less than MIN_AVAIL_MB left.
#
# Measured from the kernel's cgroups (scripts/open5gs/cgroup_stats.py), not `docker stats`.
# The stack is restored to the standard 100 UEs at the end, even if a step fails.
#
# Evidence: docs/evidence/open5gs-scale/scale-<ts>.txt and .csv (the CSV opens in Excel).
# Usage:    scripts/open5gs/scale_test.sh
#           BATCHES="1 10 100" STEPS="100 200" scripts/open5gs/scale_test.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
export NO_COLOR=1

BATCHES="${BATCHES:-1 10 25 100}"
STEPS="${STEPS:-100 150 200 300}"
SETTLE="${SETTLE:-60}"           # seconds at rest before a step is measured
MEASURE="${MEASURE:-20}"         # seconds of CPU sampling at rest
MIN_AVAIL_MB="${MIN_AVAIL_MB:-700}"
ATTACH_TIMEOUT="${ATTACH_TIMEOUT:-600}"

TS=$(date -u +%Y%m%d-%H%M%SZ)
OUT=docs/evidence/open5gs-scale; mkdir -p "$OUT"
LOG="$OUT/scale-$TS.txt"; CSV="$OUT/scale-$TS.csv"
OVR=/tmp/o5gs-scale-override.yaml
CP="o5gs-amf o5gs-smf o5gs-ausf o5gs-udm o5gs-udr o5gs-nrf o5gs-scp o5gs-pcf o5gs-mongodb"
RAN="o5gs-gnb-embb o5gs-gnb-urllc o5gs-gnb-mmtc o5gs-ue o5gs-upf-mmtc o5gs-prometheus"
EMBB=20; URLLC=10

say() { echo; echo "== $(date -u +%T) $*"; }
prom() { curl -s --get --data-urlencode "query=$1" http://localhost:9091/api/v1/query \
           | python3 -c "import json,sys; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '')"; }
avail_mb() { awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo; }

# Recreate the UE container with a given plan and batch size, through a compose override so the
# committed compose file is never edited. <mmtc count> <batch>
ue_with() {
  cat > "$OVR" <<EOF
services:
  ue:
    environment:
      UE_PLAN: "ue-slice-a.yaml:$EMBB:10.45.0.1:eMBB ue-slice-b.yaml:$URLLC:10.46.0.1:URLLC ue-slice-c.yaml:$1:10.47.0.1:mMTC"
      START_BATCH: "$2"
EOF
  (cd deployments/open5gs && docker compose -f docker-compose.yaml -f "$OVR" up -d --no-deps --force-recreate ue >/dev/null 2>&1)
}

# Wait for the UE supervisor's startup record: "<up> <total> <seconds> <batch>".
wait_startup() {
  for _ in $(seq "$ATTACH_TIMEOUT"); do
    r=$(docker exec o5gs-ue cat /tmp/ues/startup 2>/dev/null) && [ -n "$r" ] && { echo "$r"; return 0; }
    sleep 1
  done
  echo "0 0 $ATTACH_TIMEOUT 0"; return 1
}

# Per-UE latency from the UEs' own logs: registration = MM-REGISTER-INITIATED -> "Initial
# Registration is successful"; PDU = that -> "PDU Session establishment is successful".
# Prints "reg_p50 reg_p95 reg_max pdu_p50 pdu_p95 pdu_max" in ms, and the UE-side reject count.
ue_latency() {
  docker exec o5gs-ue sh -c 'for f in /tmp/ue-2089*.log; do
      a=$(grep -m1 "MM-REGISTER-INITIATED" "$f" | cut -c13-24)
      b=$(grep -m1 "Initial Registration is successful" "$f" | cut -c13-24)
      c=$(grep -m1 "PDU Session establishment is successful" "$f" | cut -c13-24)
      echo "$a $b $c"; done' 2>/dev/null | python3 -c '
import sys
def ms(t):
    h, m, s = t.split(":"); return (int(h) * 3600 + int(m) * 60 + float(s)) * 1000
reg, pdu = [], []
for line in sys.stdin:
    p = line.split()
    if len(p) == 3:
        reg.append(ms(p[1]) - ms(p[0])); pdu.append(ms(p[2]) - ms(p[1]))
def pct(v, q):
    v = sorted(v); return round(v[min(len(v) - 1, int(q * len(v)))]) if v else ""
print(pct(reg, .5), pct(reg, .95), round(max(reg)) if reg else "", pct(pdu, .5), pct(pdu, .95), round(max(pdu)) if pdu else "")'
}

cpu_of() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); v=d.get(sys.argv[2],{}).get(sys.argv[3]); print('' if v is None else v)" "$1" "$2" "$3"; }

restore() {
  say "restore: the standard 100 UEs"
  docker exec o5gs-mongodb mongo open5gs --quiet --eval \
    'print("removed extra subscribers: " + db.subscribers.deleteMany({imsi: {$gt: "208930000000100"}}).deletedCount)'
  rm -f "$OVR"
  (cd deployments/open5gs && docker compose up -d --no-deps --force-recreate ue >/dev/null 2>&1)
  bash scripts/open5gs/start_ues.sh 2>&1 | tail -9 | sed 's/^/  /'
}
trap restore EXIT

{
echo "Open5GS scale test — $TS"
echo "commit: $(git rev-parse HEAD)$(git diff --quiet || echo ' + uncommitted changes')"
echo "host: $(nproc) CPUs, $(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo) MiB, $(avail_mb) MiB available at start"
echo "UERANSIM: $(docker image inspect o5gs/ueransim:v3.3.0-udpbuf-mono --format '{{index .Config.Labels "io.5g-observability.patches"}}')"

echo "part,batch,ues,attached,attach_s,reg_p50_ms,reg_p95_ms,reg_max_ms,pdu_p50_ms,pdu_p95_ms,pdu_max_ms,reg_fail,ue_rejects,amf_cpu_peak,smf_cpu_peak,ausf_cpu_peak,udm_cpu_peak,udr_cpu_peak,nrf_cpu_peak,scp_cpu_peak,pcf_cpu_peak,mongodb_cpu_peak,gnb_mmtc_cpu_peak,ue_cpu_peak,ue_cpu_avg,amf_cpu_avg,smf_cpu_avg,upf_mmtc_cpu_avg,prometheus_cpu_avg,ue_mem_mib,amf_mem_mib,smf_mem_mib,host_avail_mib,probe_round_s,reachable" > "$CSV"

say "part 1 — attach burst: 100 UEs, START_BATCH = $BATCHES"
for b in $BATCHES; do
  # Same starting state for every batch: fresh subscriber records (SQN reset), then the burst.
  python3 scripts/open5gs/provision_subscribers.py >/dev/null || { echo "  provisioning failed"; break; }
  fail0=$(prom 'sum(fivegs_amffunction_rm_reginitfail)')
  rm -f /tmp/o5gs-scale-stop
  python3 scripts/open5gs/cgroup_stats.py --json --until-file /tmp/o5gs-scale-stop $CP $RAN > /tmp/o5gs-scale-cpu.json &
  sampler=$!
  ue_with 70 "$b"
  read -r up total secs _ <<<"$(wait_startup)"
  touch /tmp/o5gs-scale-stop; wait "$sampler"
  cpu=$(cat /tmp/o5gs-scale-cpu.json)
  sleep 6   # let Prometheus scrape the AMF counters
  fail1=$(prom 'sum(fivegs_amffunction_rm_reginitfail)')
  rej=$(docker exec o5gs-ue sh -c 'cat /tmp/ue-2089*.log | grep -ci reject' | tr -d '\r')
  read -r r50 r95 rmax p50 p95 pmax <<<"$(ue_latency)"
  regfail=$(python3 -c "print(int(float('${fail1:-0}' or 0) - float('${fail0:-0}' or 0)))")
  printf '  batch %3s: %s/%s attached in %ss · registration p50 %s ms p95 %s ms max %s ms · PDU p50 %s ms p95 %s ms · reg failures %s · AMF peak %s%% · SMF peak %s%% · AUSF %s%% · UDM %s%%\n' \
    "$b" "$up" "$total" "$secs" "$r50" "$r95" "$rmax" "$p50" "$p95" "$regfail" \
    "$(cpu_of "$cpu" o5gs-amf cpu_peak)" "$(cpu_of "$cpu" o5gs-smf cpu_peak)" "$(cpu_of "$cpu" o5gs-ausf cpu_peak)" "$(cpu_of "$cpu" o5gs-udm cpu_peak)"
  row="burst,$b,$total,$up,$secs,$r50,$r95,$rmax,$p50,$p95,$pmax,$regfail,$rej"
  for c in amf smf ausf udm udr nrf scp pcf mongodb gnb-mmtc ue; do row="$row,$(cpu_of "$cpu" "o5gs-$c" cpu_peak)"; done
  echo "$row,,,,,,,,,,," >> "$CSV"
done

say "part 2 — scale steps: total UEs = $STEPS (eMBB $EMBB + URLLC $URLLC + the rest mMTC)"
for n in $STEPS; do
  m=$((n - EMBB - URLLC))
  O5GS_UES="A=$EMBB B=$URLLC C=$m" python3 scripts/open5gs/provision_subscribers.py | tail -1 | sed 's/^/  /'
  ue_with "$m" 25
  read -r up total secs _ <<<"$(wait_startup)"
  echo "  $n UEs: $up/$total attached in ${secs}s; settling ${SETTLE}s"
  sleep "$SETTLE"
  cpu=$(python3 scripts/open5gs/cgroup_stats.py --json --duration "$MEASURE" $CP $RAN)
  av=$(avail_mb)
  round=$(prom 'max(ue_probe_round_duration_seconds)')
  reach=$(prom 'sum(ue_probe_reachable)')
  read -r r50 r95 rmax p50 p95 pmax <<<"$(ue_latency)"
  printf '  %3s UEs at rest: UE container CPU avg %s%% mem %s MiB · AMF %s%% · SMF %s%% · mMTC UPF %s%% · Prometheus %s%% · host available %s MiB · probe round %ss · reachable %s\n' \
    "$n" "$(cpu_of "$cpu" o5gs-ue cpu_avg)" "$(cpu_of "$cpu" o5gs-ue mem_peak_mib)" "$(cpu_of "$cpu" o5gs-amf cpu_avg)" \
    "$(cpu_of "$cpu" o5gs-smf cpu_avg)" "$(cpu_of "$cpu" o5gs-upf-mmtc cpu_avg)" "$(cpu_of "$cpu" o5gs-prometheus cpu_avg)" "$av" "${round:-?}" "${reach:-?}"
  row="steps,25,$total,$up,$secs,$r50,$r95,$rmax,$p50,$p95,$pmax,,"
  for c in amf smf ausf udm udr nrf scp pcf mongodb gnb-mmtc ue; do row="$row,"; done
  row="$row,$(cpu_of "$cpu" o5gs-ue cpu_avg),$(cpu_of "$cpu" o5gs-amf cpu_avg),$(cpu_of "$cpu" o5gs-smf cpu_avg),$(cpu_of "$cpu" o5gs-upf-mmtc cpu_avg),$(cpu_of "$cpu" o5gs-prometheus cpu_avg)"
  row="$row,$(cpu_of "$cpu" o5gs-ue mem_peak_mib),$(cpu_of "$cpu" o5gs-amf mem_peak_mib),$(cpu_of "$cpu" o5gs-smf mem_peak_mib),$av,$round,$reach"
  echo "$row" >> "$CSV"
  if [ "${up:-0}" -lt "$n" ]; then echo "  STOP: only $up of $n UEs attached — this is where this laptop stops"; break; fi
  if [ "$av" -lt "$MIN_AVAIL_MB" ]; then echo "  STOP: $av MiB available, below $MIN_AVAIL_MB — this is where this laptop stops"; break; fi
done

echo
echo "CSV: $CSV"
} 2>&1 | tee "$LOG"
