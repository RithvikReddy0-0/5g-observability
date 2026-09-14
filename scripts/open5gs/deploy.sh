#!/usr/bin/env bash
# deploy.sh — deploy a change to the Open5GS stack behind the KPI gate, and roll back if it fails.
#
# Closes objective 8: KPI validation is not only reported, it decides. The candidate is whatever
# is in deployments/open5gs/ and deployments/slices.env right now.
#
#   1. validate   static checks on the candidate: YAML, slice/DNN consistency, KPI definitions.
#                 A candidate that fails here is rejected WITHOUT touching the running stack.
#   2. snapshot   the running configuration is the last known-good, if it currently passes the
#                 gate (the first run establishes it).
#   3. apply      restart the stack in dependency order. The SMF is always followed by its UPFs:
#                 restarting only the SMF left a UPF with stale PFCP state that rejected every
#                 session (ADR-011).
#   4. verify     wait for UEs, let the gate's 1-minute windows fill with post-deploy data only,
#                 run the KPI gate.
#   5. decide     PASS: the candidate becomes the new known-good.
#                 FAIL: the rejected candidate is saved to .deploy/rejected/<ts>/, the known-good
#                 files are restored, the stack is redeployed and re-gated.
#
# Exit: 0 deployed · 1 rejected and rolled back (stack healthy again) · 2 rollback also failed
# Evidence: docs/evidence/open5gs-deploy/deploy-<ts>.txt      State: .deploy/ (git-ignored)
# Usage: scripts/open5gs/deploy.sh --init   once, on a working stack: record it as known-good
#        scripts/open5gs/deploy.sh          deploy whatever the files now say

set -uo pipefail
INIT=0; [ "${1:-}" = "--init" ] && INIT=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
export NO_COLOR=1

TS=$(date -u +%Y%m%d-%H%M%SZ)
STATE=.deploy
KG="$STATE/known-good"
LOG_DIR=docs/evidence/open5gs-deploy
mkdir -p "$STATE" "$LOG_DIR"
LOG="$LOG_DIR/deploy-$TS.txt"
TRACKED="deployments/open5gs deployments/slices.env"
DEFS=deployments/open5gs/kpi-gates.json
ORDER="o5gs-mongodb o5gs-nrf o5gs-scp o5gs-ausf o5gs-udm o5gs-udr o5gs-pcf o5gs-bsf o5gs-nssf o5gs-amf o5gs-smf o5gs-upf-embb o5gs-upf-urllc o5gs-dn-embb o5gs-dn-urllc o5gs-gnb-embb o5gs-gnb-urllc o5gs-ue"

say() { echo "== $(date -u +%T) $*"; }
gate() { python3 tools/kpi-gate/kpi_gate.py --defs "$DEFS" --wait 20; }

validate() {
  python3 - <<'PY' || return 1
import glob, sys, yaml
bad = 0
for f in glob.glob("deployments/open5gs/**/*.y*ml", recursive=True):
    try:
        list(yaml.safe_load_all(open(f, encoding="utf-8")))   # multi-document: Kubernetes manifests
    except Exception as e:
        print("  invalid YAML %s: %s" % (f, str(e).splitlines()[0])); bad += 1
print("  YAML: %s" % ("ok" if not bad else "%d invalid" % bad))
sys.exit(1 if bad else 0)
PY
  python3 tools/check_open5gs_slices.py | sed 's/^/  /' | tail -3
  [ "${PIPESTATUS[0]}" -eq 0 ] || return 1
  python3 tools/kpi-gate/kpi_gate.py --self-test --defs "$DEFS" >/dev/null || { echo "  KPI definitions invalid"; return 1; }
  echo "  KPI definitions: ok"
}

apply() {   # restart everything in dependency order so no NF keeps state from the old config
  (cd deployments/open5gs && docker compose up -d --remove-orphans 2>&1 | grep -E "Error" )
  for c in $ORDER; do docker restart "$c" >/dev/null 2>&1; done
  bash scripts/open5gs/start_ues.sh 2>&1 | tail -5 | sed 's/^/  /'
  echo "  waiting 75 s so every gate window holds only post-deploy samples"
  sleep 75
}

snapshot() {   # <dir>
  rm -rf "$1"; mkdir -p "$1"
  tar -cf "$1/files.tar" $TRACKED
  (cd "$1" && tar -tf files.tar | grep -v '/$' | sort > manifest.txt)
  for i in o5gs/open5gs:v2.8.0 o5gs/ueransim:v3.3.0-udpbuf; do
    echo "$i $(docker image inspect "$i" --format '{{.Id}}')" >> "$1/images.txt"
  done
  git rev-parse HEAD > "$1/commit.txt"
}

{
echo "Open5GS deployment — $TS"
echo "commit: $(git rev-parse HEAD)$(git diff --quiet -- $TRACKED || echo ' + uncommitted changes in the candidate')"

say "1. validate the candidate (static, the running stack is not touched)"
if ! validate; then
  echo "DECISION: REJECTED before deployment — static validation failed; the running stack is unchanged"
  exit 1
fi

say "2. last known-good"
if [ "$INIT" = 1 ]; then
  # Only the operator knows the files on disk are what is running; the gate confirms it works.
  if gate > /tmp/deploy-gate.txt 2>&1; then
    snapshot "$KG"
    echo "  known-good established from the running stack ($(wc -l < "$KG/manifest.txt") files, gate passed)"
    echo "DECISION: INITIALISED — change files, then run scripts/open5gs/deploy.sh"
    exit 0
  fi
  grep -E "\[  FAIL" /tmp/deploy-gate.txt | head -5
  echo "DECISION: ABORTED — the running stack does not pass the gate, so it cannot be known-good"
  exit 2
elif [ ! -f "$KG/files.tar" ]; then
  echo "  no known-good snapshot: run 'scripts/open5gs/deploy.sh --init' on a working stack first"
  echo "DECISION: ABORTED — nothing safe to roll back to"
  exit 2
else
  echo "  known-good snapshot from $(stat -c %y "$KG/files.tar" | cut -d. -f1), commit $(cat "$KG/commit.txt")"
fi
changed=$(mkdir -p "$STATE/cmp" && rm -rf "$STATE/cmp"/* && tar -xf "$KG/files.tar" -C "$STATE/cmp" && diff -rq "$STATE/cmp/deployments" deployments 2>/dev/null | grep -vE 'Only in deployments: (compose|kpi-gates.json)' | sed 's/^/    /')
if [ -z "$changed" ]; then echo "  candidate is identical to known-good"; else echo "  candidate differs from known-good:"; echo "$changed"; fi

say "3. apply the candidate"
apply

say "4. KPI gate on the candidate"
if gate > /tmp/deploy-gate.txt 2>&1; then
  grep -E "passed|GATE" /tmp/deploy-gate.txt | sed 's/^/  /'
  snapshot "$KG"
  echo "DECISION: DEPLOYED — the candidate passed the gate and is the new known-good"
  exit 0
fi
grep -E "\[  FAIL" -A1 /tmp/deploy-gate.txt | grep -v '^--$' | sed 's/^/  /'
grep -E "passed|GATE" /tmp/deploy-gate.txt | sed 's/^/  /'

say "5. ROLLBACK — the candidate failed the gate"
rej="$STATE/rejected/$TS"
snapshot "$rej"
echo "  rejected candidate saved to $rej (nothing is discarded)"
rm -rf deployments/open5gs/config deployments/open5gs/ran deployments/open5gs/observability
tar -xf "$KG/files.tar"
echo "  known-good files restored"
apply
if gate > /tmp/deploy-gate.txt 2>&1; then
  grep -E "passed|GATE" /tmp/deploy-gate.txt | sed 's/^/  /'
  echo "DECISION: REJECTED and ROLLED BACK — the stack is on the known-good configuration and passes the gate"
  exit 1
fi
grep -E "\[  FAIL|passed|GATE" /tmp/deploy-gate.txt | sed 's/^/  /'
echo "DECISION: ROLLBACK FAILED — the known-good configuration does not pass either; manual attention needed"
exit 2
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
