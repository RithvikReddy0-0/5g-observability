#!/usr/bin/env bash
# collect_evidence.sh — a timestamped proof bundle for the Open5GS stack.
#
# Writes docs/evidence/open5gs-<UTC timestamp>/ with everything needed to check a claim
# without re-running the stack: pins, container state, subscribers, per-UE sessions, a
# ground-truth audit of the core's own gauges, user-plane verification, the KPI gate verdict
# at steady state, a concurrent traffic run, and the slice-isolation test.
#
# Order matters: the gate is taken BEFORE any load. An early version ran it straight after a
# saturating traffic run, and its verdict described the load, not the deployment.
#
# Usage: scripts/open5gs/collect_evidence.sh [traffic-seconds=30] [isolation-seconds-per-phase=60]

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2

T="${1:-30}"
ISO="${2:-60}"
TS="$(date -u +%Y%m%d-%H%M%SZ)"
OUT="docs/evidence/open5gs-$TS"
mkdir -p "$OUT"
export NO_COLOR=1

metrics() { docker exec o5gs-ue curl -s --max-time 5 "http://$1/metrics"; }
step() { echo "  [$1] $2"; }

echo "Collecting Open5GS evidence -> $OUT"

step 1 "versions and pins"
{
  echo "collected: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host kernel: $(uname -r)"
  echo "docker engine: $(docker version --format '{{.Server.Version}}' 2>&1)"
  echo
  python3 - <<'PY'
import json
for d in json.load(open("manifest.lock"))["dependencies"]:
    print("pin  %-9s %s  (%s)" % (d["name"], d["commit"], d["ref_hint"]))
PY
  echo
  for i in o5gs/open5gs:v2.8.0 o5gs/ueransim:v3.3.0-udpbuf-mono; do
    echo "image $i  $(docker image inspect "$i" --format '{{.Id}} {{.Size}}' 2>&1)"
  done
  echo "open5gs binary commit: $(docker exec o5gs-amf cat /opt/open5gs/COMMIT 2>&1)"
  echo "open5gs version: $(docker exec o5gs-amf open5gs-amfd -v 2>&1 | head -1)"
} > "$OUT/01-versions.txt" 2>&1

step 2 "containers"
docker ps -a --filter name=o5gs- --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' > "$OUT/02-containers.txt" 2>&1

step 3 "subscribers"
docker exec o5gs-mongodb mongo open5gs --quiet --eval '
print("subscribers: " + db.subscribers.count());
db.subscribers.aggregate([{$unwind: "$slice"}, {$match: {"slice.default_indicator": true}},
  {$group: {_id: {sst: "$slice.sst", sd: "$slice.sd"}, n: {$sum: 1},
            dnn: {$first: {$arrayElemAt: ["$slice.session.name", 0]}},
            fiveqi: {$first: {$arrayElemAt: ["$slice.session.qos.index", 0]}},
            arp: {$first: {$arrayElemAt: ["$slice.session.qos.arp.priority_level", 0]}},
            ambr: {$first: {$arrayElemAt: ["$slice.session.ambr.downlink", 0]}}}},
  {$sort: {"_id.sst": 1}}]).forEach(function (r) { printjson(r); });
print("imsis: " + db.subscribers.find({}, {imsi: 1, _id: 0}).map(function (d) { return d.imsi; }).join(" "));
' > "$OUT/03-subscribers.txt" 2>&1

step 4 "UE registration and PDU sessions (UE side, ground truth)"
{
  echo "--- PDU-session interfaces ---"
  docker exec o5gs-ue ip -4 -o addr show | awk '/uesimtun/ {print $2, $4}' | sort -V
  echo
  echo "interfaces total: $(docker exec o5gs-ue ip -4 -o addr show | grep -c uesimtun)"
  echo "  eMBB  (10.45/16): $(docker exec o5gs-ue ip -4 -o addr show | grep -c 'uesimtun.* 10[.]45[.]')"
  echo "  URLLC (10.46/16): $(docker exec o5gs-ue ip -4 -o addr show | grep -c 'uesimtun.* 10[.]46[.]')"
  echo "  mMTC  (10.47/16): $(docker exec o5gs-ue ip -4 -o addr show | grep -c 'uesimtun.* 10[.]47[.]')"
  echo
  echo "--- per-UE session lines from the UE logs (one log per UE process) ---"
  docker exec o5gs-ue sh -c 'cat /tmp/ue-2089*.log' | grep 'TUN interface' | sed -E 's/^\[[^]]+\] //'
  echo
  echo "--- UEs restarted by the supervisor (user plane lost after radio link failure) ---"
  docker exec o5gs-ue sh -c 'cat /tmp/ues/restarts.log 2>/dev/null || echo none'
  echo
  echo "--- gNB ---"
  for g in o5gs-gnb-embb o5gs-gnb-urllc o5gs-gnb-mmtc; do
    echo "$g: $(docker logs "$g" 2>&1 | grep -E 'NG Setup procedure is successful' | tail -1)"
  done
} > "$OUT/04-ue-sessions.txt" 2>&1

step 5 "ground-truth audit of the core's own gauges"
{
  truth=$(docker exec o5gs-ue ip -4 -o addr show | grep -c uesimtun)
  echo "ground truth: $truth UE interfaces (eMBB 20, URLLC 10, mMTC 70 expected)"
  echo
  echo "--- AMF ---";  metrics 10.53.0.5:9090  | grep -E '^(gnb|ran_ue|amf_session|fivegs_amffunction_rm_(reginit|registeredsubnbr))'
  echo "--- SMF ---";  metrics 10.53.0.4:9090  | grep -E '^(ues_active|pfcp_|fivegs_smffunction_sm_(sessionnbr|qos_flow_nbr|pdusessioncreation))'
  echo "--- UPF eMBB ---";  metrics 10.53.0.7:9090  | grep -E '^(pfcp_peers_active|fivegs_upffunction_upf_(sessionnbr|qosflows))'
  echo "--- UPF URLLC ---"; metrics 10.53.0.8:9090  | grep -E '^(pfcp_peers_active|fivegs_upffunction_upf_(sessionnbr|qosflows))'
  echo "--- UPF mMTC ---";  metrics 10.53.0.9:9090  | grep -E '^(pfcp_peers_active|fivegs_upffunction_upf_(sessionnbr|qosflows))'
  echo
  echo "Interpretation (docs/open5gs.md, 'Which metrics can be trusted'):"
  echo "  exact   : ran_ue, amf_session, ues_active, sm_sessionnbr{snssai}"
  echo "  lower bound only: upf_sessionnbr, upf_qosflows{dnn} (over-count after a UE restart)"
  echo "  NOT     : sm_qos_flow_nbr (never decremented on re-attach), sm_pdusessioncreationsucc (counts"
  echo "            each success twice), sm_pdusessioncreationreq (unlabelled duplicate series)"
  echo "  NONE of the core's gauges notices a UE that has vanished or lost its user plane."
} > "$OUT/05-metrics-audit.txt" 2>&1

step 6 "user-plane verification"
bash scripts/open5gs/verify_user_plane.sh > "$OUT/06-user-plane.txt" 2>&1

step 7 "KPI gate at steady state (before any load)"
python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json \
  --json "$OUT/07-kpi-verdict.json" > "$OUT/07-kpi-gate.txt" 2>&1
GATE=$(python3 -c "import json;print(json.load(open('$OUT/07-kpi-verdict.json'))['verdict'])" 2>/dev/null || echo "NOT RUN")

step 8 "concurrent traffic, ${T}s, 3 UEs per slice"
bash scripts/open5gs/traffic.sh "$T" 3 down both > "$OUT/08-traffic.txt" 2>&1
sleep 10   # let Prometheus take two scrapes after the load ends

step 9 "slice isolation: idle vs eMBB saturated, ${ISO}s per phase"
bash scripts/open5gs/isolation_test.sh "$ISO" > "$OUT/09-isolation.txt" 2>&1

step 10 "Prometheus view"
{
  curl -s http://localhost:9091/api/v1/targets | python3 -c "import json,sys
for t in json.load(sys.stdin)['data']['activeTargets']:
    print('target %-18s %-16s %s' % (t['labels']['job'], t['scrapeUrl'].split('/')[2], t['health']))"
  for q in 'max_over_time((sum by (slice) (rate(upf_slice_downlink_bytes_total[15s])) * 8 / 1e6)[10m:5s])' \
           'sum by (snssai) (fivegs_smffunction_sm_sessionnbr)' \
           'sum by (dnn) (fivegs_upffunction_upf_qosflows)'; do
    echo
    echo "query: $q"
    curl -s --get http://localhost:9091/api/v1/query --data-urlencode "query=$q" | python3 -c "import json,sys
for r in json.load(sys.stdin)['data']['result']:
    m = {k: v for k, v in r['metric'].items() if k in ('slice', 'snssai', 'dnn')}
    print('  %s = %.2f' % (m, float(r['value'][1])))"
  done
} > "$OUT/10-prometheus.txt" 2>&1

up_pass=$(grep -oE 'PASS=[0-9]+  FAIL=[0-9]+' "$OUT/06-user-plane.txt" | tail -1)
restarts=$(docker exec o5gs-ue sh -c 'cat /tmp/ues/restarts.log 2>/dev/null | wc -l' | tr -d ' ')
cat > "$OUT/00-summary.md" <<EOF
# Open5GS evidence — $TS

| | |
|---|---|
| UE interfaces (ground truth) | $(grep -oE 'interfaces total: [0-9]+' "$OUT/04-ue-sessions.txt" | awk '{print $3}') / 100 |
| User-plane verification | ${up_pass:-not run} |
| KPI gate (steady state, before load) | **$GATE** |
| UEs restarted by the supervisor since the UE container started | ${restarts:-?} |

| File | Contents |
|---|---|
| \`01-versions.txt\` | pins, image IDs, the commit baked into the binaries |
| \`02-containers.txt\` | container state |
| \`03-subscribers.txt\` | subscriber records per home slice: DNN, 5QI, ARP, AMBR |
| \`04-ue-sessions.txt\` | every UE's PDU-session interface and address (UE side), supervisor restarts |
| \`05-metrics-audit.txt\` | the core's own gauges next to ground truth |
| \`06-user-plane.txt\` | ping through the UPF per slice, UPF counters, iperf3, internet egress |
| \`07-kpi-gate.txt\`, \`07-kpi-verdict.json\` | KPI gate verdict, taken before any load |
| \`08-traffic.txt\` | concurrent iperf3 per slice against each session's AMBR |
| \`09-isolation.txt\` | URLLC reachability and RTT, idle vs eMBB saturated |
| \`10-prometheus.txt\` | scrape targets and the run as Prometheus recorded it |
EOF

echo "done: $OUT  (KPI gate $GATE)"
