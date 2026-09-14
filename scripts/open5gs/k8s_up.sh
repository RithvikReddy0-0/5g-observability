#!/usr/bin/env bash
# k8s_up.sh — run the Open5GS stack on Kubernetes and judge it with the SAME checks as compose.
#
# SPEC Phase 2a: re-establish the verified behaviour on Kubernetes, using the compose deployment
# as the regression oracle. The manifests are generated from the compose configuration
# (deployments/open5gs/k8s/render.py), and the verdict comes from the same KPI gate definitions
# (deployments/open5gs/kpi-gates.json), queried through a port-forward to the in-cluster
# Prometheus.
#
# Cluster: minikube profile "o5gs" (docker driver) — a separate profile, so any other minikube
# cluster on the machine is untouched. The compose Open5GS stack is paused first; two cores on
# a 7.6 GiB host at once would make the measurements meaningless.
#
# Evidence: docs/evidence/open5gs-k8s/k8s-<ts>.txt
# Usage:    scripts/open5gs/k8s_up.sh            deploy, verify, gate
#           scripts/open5gs/k8s_up.sh --verify   re-check the running deployment only
#           scripts/open5gs/k8s_up.sh --down     delete the namespace and stop the cluster

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
export NO_COLOR=1
P=o5gs; NS=o5gs
K="kubectl --context $P -n $NS"
PF_PORT=19091

if [ "${1:-}" = "--down" ]; then
  kubectl --context "$P" delete namespace "$NS" --wait=true 2>&1 | tail -1
  minikube -p "$P" stop 2>&1 | tail -1
  exit 0
fi

VERIFY=0; [ "${1:-}" = "--verify" ] && VERIFY=1
TS=$(date -u +%Y%m%d-%H%M%SZ)
OUT=docs/evidence/open5gs-k8s; mkdir -p "$OUT"
LOG="$OUT/k8s-$TS.txt"
fail=0
say() { echo; echo "== $(date -u +%T) $*"; }
check() { if eval "$2"; then echo "  [ PASS ] $1"; else echo "  [ FAIL ] $1"; fail=$((fail + 1)); fi; }
uex() { $K exec deploy/ue -c ue -- "$@"; }

{
echo "Open5GS on Kubernetes — $TS"
echo "commit: $(git rev-parse HEAD)"

if [ "$VERIFY" = 0 ]; then
say "1. cluster"
minikube -p "$P" status >/dev/null 2>&1 || minikube -p "$P" start --driver=docker --cpus=6 --memory=4096 \
  --container-runtime=docker --addons=storage-provisioner 2>&1 | tail -2
kubectl --context "$P" version 2>/dev/null | grep -E "Server Version" | sed 's/^/  /'
echo "  pausing the compose Open5GS stack (both cores at once would distort every measurement)"
bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
docker ps -q --filter name=o5gs- | xargs -r docker stop >/dev/null

say "2. images built from the pinned commits, loaded into the cluster"
for i in o5gs/open5gs:v2.8.0 o5gs/ueransim:v3.3.0-udpbuf; do
  minikube -p "$P" image load "$i" && echo "  loaded $i"
done

say "3. apply the manifests generated from the compose deployment"
python3 deployments/open5gs/k8s/render.py | sed 's/^/  /'
kubectl --context "$P" apply -f deployments/open5gs/k8s/generated/o5gs.yaml 2>&1 | grep -vE "unchanged" | sed 's/^/  /' | tail -8
for d in mongodb nrf scp ausf udm udr pcf bsf nssf amf upf-embb upf-urllc smf dn-embb dn-urllc gnb-embb gnb-urllc ue prometheus; do
  $K rollout status "deploy/$d" --timeout=300s >/dev/null 2>&1 && printf '%s ' "$d" || { printf '[%s NOT READY] ' "$d"; fail=$((fail + 1)); }
done
echo

fi   # VERIFY

say "4. core bring-up"
for g in gnb-embb gnb-urllc; do
  for _ in $(seq 60); do $K logs "deploy/$g" 2>/dev/null | grep -q "NG Setup procedure is successful" && break; sleep 2; done
done
check "both gNBs completed NG Setup" '[ "$( ( $K logs deploy/gnb-embb; $K logs deploy/gnb-urllc ) 2>/dev/null | grep -c "NG Setup procedure is successful")" -ge 2 ]'
check "SMF associated with both UPFs over PFCP" '[ "$($K logs deploy/smf 2>/dev/null | grep -c "PFCP associated")" -ge 2 ]'

say "5. subscribers and UEs"
if [ "$VERIFY" = 0 ]; then
  python3 scripts/open5gs/provision_subscribers.py --kubernetes "$NS" | sed 's/^/  /'
  $K rollout restart deploy/ue >/dev/null && $K rollout status deploy/ue --timeout=300s >/dev/null
fi
live=0
for _ in $(seq 60); do
  live=$(uex sh -c 'ip -4 -o addr show | grep -c uesimtun' 2>/dev/null | tr -d '\r')
  [ "${live:-0}" -ge 20 ] && break; sleep 3
done
echo "  UE session addresses: ${live:-0}"
check "20 UEs with a PDU session" '[ "${live:-0}" -ge 20 ]'
na=$(uex sh -c "ip -4 -o addr show | grep -c 'uesimtun.* 10[.]45[.]'" | tr -d '\r')
nb=$(uex sh -c "ip -4 -o addr show | grep -c 'uesimtun.* 10[.]46[.]'" | tr -d '\r')
check "10 per slice, each from its own pool (eMBB $na, URLLC $nb)" '[ "$na" -ge 10 ] && [ "$nb" -ge 10 ]'

say "6. user plane, per slice"
for spec in "eMBB 10.45. 10.45.0.1 upf-embb ogstun dn-embb" "URLLC 10.46. 10.46.0.1 upf-urllc ogstun2 dn-urllc"; do
  # Named, not positional: check() evaluates its condition inside the function, where $1..$6
  # are check()'s own arguments (set -u aborted the first run on "$3: unbound variable").
  read -r sname pool gw upf tun dn <<<"$spec"
  read -r dev ip <<<"$(uex ip -4 -o addr show | awk -v p="$pool" '/uesimtun/ && index($4, p) == 1 {split($4, a, "/"); print $2, a[1]; exit}')"
  before=$($K exec "deploy/$upf" -- cat "/sys/class/net/$tun/statistics/rx_bytes" 2>/dev/null)
  check "$sname: ping $gw through its UPF from $dev ($ip)" 'uex ping -I "$dev" -c 3 -W 2 "$gw" >/dev/null 2>&1'
  after=$($K exec "deploy/$upf" -- cat "/sys/class/net/$tun/statistics/rx_bytes" 2>/dev/null)
  check "$sname: bytes arrived on $upf's $tun ($before -> $after)" '[ "${after:-0}" -gt "${before:-0}" ]'
  dnip=$($K get pod -l app="$dn" -o jsonpath='{.items[0].status.podIP}')
  $K exec "deploy/$dn" -- sh -c 'pkill -x iperf3; iperf3 -s -D --one-off' >/dev/null 2>&1; sleep 1
  mbps=$(uex iperf3 -c "$dnip" -B "$ip" -t 3 -J 2>/dev/null | python3 -c "import json,sys
try: print('%.1f' % (json.load(sys.stdin)['end']['sum_received']['bits_per_second'] / 1e6))
except Exception: print('')")
  check "$sname: iperf3 UE -> $upf -> $dn (${mbps:-none} Mbps)" '[ -n "$mbps" ]'
done

say "7. the same KPI gate as compose, against the in-cluster Prometheus"
$K port-forward svc/prometheus "$PF_PORT:9090" >/dev/null 2>&1 &
PF=$!
sleep 80    # UE probes and the gate's 1-minute windows fill with data from this deployment
python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json \
  --prometheus "http://localhost:$PF_PORT" --json "$OUT/k8s-$TS-gate.json" > /tmp/k8s-gate.txt 2>&1
gate_rc=$?
grep -E "\[  (FAIL|WARN)|passed|GATE" -A1 /tmp/k8s-gate.txt | grep -v '^--$' | sed 's/^/  /'
kill "$PF" 2>/dev/null
check "KPI gate passes on Kubernetes" '[ "$gate_rc" -eq 0 ]'

say "resources"
$K get pods -o wide 2>/dev/null | awk '{print "  " $1, $2, $3, $4}' | column -t
echo
if [ "$fail" -eq 0 ]; then echo "RESULT: PASS — the Open5GS stack runs on Kubernetes and passes the same checks as compose"
else echo "RESULT: FAIL — $fail check(s) failed"; fi
} 2>&1 | tee "$LOG"
grep -q "^RESULT: PASS" "$LOG"
