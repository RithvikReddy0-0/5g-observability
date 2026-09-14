#!/usr/bin/env bash
# open5gs-rebuild.sh — prove teardown and rebuild return the Open5GS stack to the same working state.
#
# SPEC §5 asks for `down -v` teardown and rebuild to reach an identical working state. On free5GC
# that could only be claimed for the control plane; here the whole stack is judged, including
# the user plane:
#
#   1. docker compose down -v      containers, network AND volumes (subscribers, metrics, Grafana)
#   2. confirm nothing is left
#   3. docker compose up -d        from the committed configuration and the pinned images
#   4. provision into the empty database, attach 20 UEs
#   5. verify the user plane, then the KPI gate
#
# DESTRUCTIVE: deletes the Open5GS subscriber database and Prometheus/Grafana history. The
# free5GC stack is not touched. Evidence: docs/evidence/open5gs-rebuild/rebuild-<ts>.txt
#
# Usage: tests/open5gs-rebuild.sh --yes

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
[ "${1:-}" = "--yes" ] || { echo "destructive: re-run with --yes to delete and rebuild the Open5GS stack"; exit 2; }

TS=$(date -u +%Y%m%d-%H%M%SZ)
OUT=docs/evidence/open5gs-rebuild
mkdir -p "$OUT"
LOG="$OUT/rebuild-$TS.txt"
export NO_COLOR=1
fail=0
step() { echo; echo "== $(date -u +%T) $*"; }
check() { if eval "$2"; then echo "  [ PASS ] $1"; else echo "  [ FAIL ] $1"; fail=$((fail + 1)); fi; }

{
echo "Open5GS teardown/rebuild — $TS"
echo "commit: $(git rev-parse HEAD) $(git diff --quiet && echo clean || echo '(uncommitted changes present)')"
for i in o5gs/open5gs:v2.8.0 o5gs/ueransim:v3.3.0-udpbuf; do echo "image $i $(docker image inspect "$i" --format '{{.Id}}')"; done

step "1. teardown (down -v)"
bash scripts/open5gs/start_orchestrator.sh --stop
(cd deployments/open5gs && docker compose down -v 2>&1 | grep -cE "Removed" | sed 's/^/  removed objects: /')

step "2. nothing left"
check "no o5gs-* containers" '[ -z "$(docker ps -aq --filter name=o5gs-)" ]'
check "no open5gs_* volumes" '[ -z "$(docker volume ls -q --filter name=open5gs_)" ]'
check "network o5gs_net gone" '! docker network inspect o5gs_net >/dev/null 2>&1'

step "3. rebuild (up -d)"
(cd deployments/open5gs && docker compose up -d 2>&1 | grep -cE "Started|Created" | sed 's/^/  started\/created: /')
for _ in $(seq 60); do
  docker exec o5gs-mongodb mongo --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -q 1 && break
  sleep 2
done
check "subscriber database starts EMPTY" '[ "$(docker exec o5gs-mongodb mongo open5gs --quiet --eval "db.subscribers.count()")" = "0" ]'

step "4. provision + attach"
bash scripts/open5gs/start_ues.sh 2>&1 | tail -6
check "20 subscribers provisioned" '[ "$(docker exec o5gs-mongodb mongo open5gs --quiet --eval "db.subscribers.count()")" = "20" ]'
check "20 UE session addresses live" '[ "$(docker exec o5gs-ue ip -4 -o addr show | grep -c uesimtun)" -ge 20 ]'

step "5. user plane"
bash scripts/open5gs/verify_user_plane.sh 2>&1 | grep -E "PASS|FAIL|WARN"
check "user-plane verification passes" 'bash scripts/open5gs/verify_user_plane.sh >/dev/null 2>&1'

step "6. KPI gate (after 75 s, so the 1-minute windows hold only the rebuilt stack)"
bash scripts/open5gs/start_orchestrator.sh >/dev/null
sleep 75
python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json | grep -E "\[  (FAIL|WARN)|passed|GATE"
check "KPI gate passes" 'python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json >/dev/null 2>&1'

echo
if [ "$fail" -eq 0 ]; then echo "RESULT: PASS — teardown with volumes and rebuild returned to a working stack"
else echo "RESULT: FAIL — $fail check(s) failed"; fi
} 2>&1 | tee "$LOG"

grep -q "^RESULT: PASS" "$LOG"
