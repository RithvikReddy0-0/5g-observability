#!/usr/bin/env bash
# multi_session_demo.sh — multi-session UEs measured on the live stack (ADR-020).
#
#   1. sessions     every device's live PDU sessions: home (PSI 1) and second, per slice, and the core's
#                   own count per S-NSSAI
#   2. placement    demands placed on the OTHER slice run on the requester's own second session:
#                   eMBB phones asking for control traffic (URLLC), URLLC devices asking for video
#                   (eMBB), and an IoT device (mMTC only) as the control case
#   3. lifecycle    URLLC deactivated: the eMBB phones release only their URLLC session and stay
#                   registered with their eMBB one; URLLC activated: their URLLC session comes back
#
# Needs the slice orchestrator (EXECUTE=1) and the network orchestrator; starts both if needed.
# Evidence: docs/evidence/open5gs-multi-session/<UTC timestamp>/. ~6 min.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
TS=$(date -u +%Y%m%d-%H%M%SZ)
OUT=docs/evidence/open5gs-multi-session/$TS
mkdir -p "$OUT"
SESS="docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh sessions"
N="python3 tools/network-orchestrator/netorch.py"
smf() { curl -s --max-time 5 localhost:9091/api/v1/query --data-urlencode 'query=fivegs_smffunction_sm_sessionnbr' |
        python3 -c 'import json,sys; print("  core (SMF) sessions: " + ", ".join("%s %s" % (r["metric"]["snssai"], r["value"][1]) for r in sorted(json.load(sys.stdin)["data"]["result"], key=lambda r: r["metric"]["snssai"])))'; }
counts() { $SESS | awk '{split($4,a,"."); k=a[1]"."a[2]" "$5; n[k]++} END {for (k in n) printf "  %-16s %d\n", k, n[k]}' | sort; }

bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null; EXECUTE=1 bash scripts/open5gs/start_orchestrator.sh >/dev/null
# restart both, so they run the code in this tree
bash scripts/open5gs/start_netorch.sh --stop >/dev/null; bash scripts/open5gs/start_netorch.sh >/dev/null
sleep 3

{
  echo "== 1. sessions — $(date -u +%H:%M:%S)"
  echo "  two devices, every live session:"
  $SESS | grep -E '^2089300000000(05|25) ' | sed 's/^/    /'
  echo "  per pool and kind:"; counts
  smf
} | tee "$OUT/1-sessions.txt"

{
  echo "== 2. placement — demands on the other slice run on the requester's own session"
  req() {   # <imsi> <class> <label>
    r=$(curl -s -XPOST localhost:9111/request -d "{\"supi\":\"imsi-$1\",\"traffic_class\":\"$2\"}")
    echo "$r" | python3 -c "
import json,sys; r=json.load(sys.stdin); e=r.get('flow') or {}
own=[l.split() for l in open('/tmp/ms-sessions').read().splitlines()]
mine={f[3] for f in own if f[0]=='$1'}
print('  %-26s %-8s -> %-6s on %-13s %s' % ('$3 (imsi-...${1: -3})', '$2', (r.get('slice') or {}).get('name','REFUSED'),
      e.get('device','-'), (e.get('device_kind','') + (' = its own session' if e.get('device') in mine else ' (another device)')) if e.get('device') else r.get('reason','')[:70]))"
  }
  $SESS > /tmp/ms-sessions
  for i in 01 02 03; do req 2089300000000$i control "eMBB phone"; done
  for i in 24 25 26; do req 2089300000000$i video "URLLC (standard) device"; done
  req 208930000000040 control "IoT device"
  sleep 5
  echo; echo "  slice_flow_device_total:"
  curl -s localhost:9111/metrics | grep '^slice_flow_device_total' | sed 's/^/    /'
} | tee "$OUT/2-placement.txt"
sleep 40     # let the executed flows finish

{
  echo "== 3. lifecycle with second sessions — URLLC off, then on"
  echo "  before:"; counts
  echo; echo "-- deactivate URLLC"; $N deactivate URLLC
  sleep 12
  echo; echo "  after deactivation:"; counts; smf
  echo "  eMBB phones still registered with their home session: $($SESS | awk '$5=="home" && $4 ~ /^10\.45\./' | wc -l)/20"
  echo; echo "-- activate URLLC"; $N activate URLLC
  sleep 12      # Prometheus scrapes the SMF every 5 s: read the core's count after it has
  echo; echo "  after activation:"; counts; smf
} 2>&1 | tee "$OUT/3-lifecycle.txt"

echo "evidence: $OUT"
