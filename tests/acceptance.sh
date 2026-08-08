#!/usr/bin/env bash
# acceptance.sh — Phase 1 acceptance criteria (SPEC section 5) as executable checks.
#
# Each check prints PASS / FAIL / SKIP-ODE / GAP and the script exits non-zero if any
# runnable check FAILS. Criteria that genuinely require the Official Development Environment
# are reported SKIP-ODE rather than silently passing — a green run on a non-baseline host
# does NOT mean Phase 1 is complete.
#
# Uses plain `docker` against container names, not `docker compose`: on Docker Desktop + WSL
# the compose plugin is a symlink into /mnt/wsl/docker-desktop/ that disappears whenever
# Desktop restarts, while the daemon keeps working.
#
# Usage: tests/acceptance.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2

DB=${DB_CONTAINER:-mongodb}
UERANSIM=${UERANSIM_CONTAINER:-ueransim}
EXPECT_UES=${EXPECT_UES:-20}

pass=0; fail=0; skip=0; gap=0
ok()   { printf '  [ PASS ] %s\n' "$1"; pass=$((pass+1)); }
no()   { printf '  [ FAIL ] %s\n' "$1"; fail=$((fail+1)); }
ode()  { printf '  [SKIP-ODE] %s\n' "$1"; skip=$((skip+1)); }
gapd() { printf '  [ GAP  ] %s\n' "$1"; gap=$((gap+1)); }

echo "==================================================================="
echo "Phase 1 acceptance checks — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==================================================================="

echo
echo "-- Environment & reproducibility --"

if [ -f VERSIONS.lock ]; then
  if grep -q "TO_BE_FILLED_ON_LAB_ODE" VERSIONS.lock; then
    ode "VERSIONS.lock frozen (environment values still to capture on the ODE)"
  else
    ok "VERSIONS.lock fully populated"
  fi
else
  no "VERSIONS.lock exists"
fi

if bash scripts/bootstrap.sh --verify-only >/dev/null 2>&1; then
  ok "bootstrap --verify-only: upstreams match manifest.lock"
else
  no "bootstrap --verify-only (external/ missing or drifted — run scripts/bootstrap.sh)"
fi

if [ -z "$(git ls-files external/ 2>/dev/null)" ]; then
  ok "external/ is not tracked by git"
else
  no "external/ has tracked files"
fi

if lsmod 2>/dev/null | grep -q '^gtp5g'; then
  ok "gtp5g module loaded"
else
  ode "gtp5g builds and loads (requires conforming ODE)"
fi

echo
echo "-- Core bring-up --"

running() { docker inspect "$1" --format '{{.State.Running}}' 2>/dev/null | grep -q true; }

missing=""
for c in $DB nrf amf ausf udm udr pcf nssf smf; do
  running "$c" || missing="$missing $c"
done
if [ -z "$missing" ]; then ok "all control-plane containers running"; else no "containers not running:$missing"; fi

restarts=0
for c in nrf amf ausf udm udr pcf nssf smf; do
  n=$(docker inspect "$c" --format '{{.RestartCount}}' 2>/dev/null || echo 0)
  restarts=$((restarts + ${n:-0}))
done
if [ "$restarts" -eq 0 ]; then ok "zero crash-loops (restart count 0)"; else no "containers have restarted $restarts time(s)"; fi

NFS=$(docker exec -i "$DB" mongo free5gc --quiet \
      --eval 'print(db.NfProfile.distinct("nfType").sort().join(" "))' 2>/dev/null | tr -d '\r')
allnf=1
for t in AMF AUSF NSSF PCF SMF UDM UDR; do
  case " $NFS " in *" $t "*) ;; *) allnf=0 ;; esac
done
if [ "$allnf" -eq 1 ]; then ok "every control-plane NF registered with NRF ($NFS)"; else no "NF registration incomplete: [$NFS]"; fi

SUBS=$(docker exec -i "$DB" mongo free5gc --quiet \
       --eval 'print(db["subscriptionData.provisionedData.amData"].count())' 2>/dev/null | tr -d ' \r')
if [ "${SUBS:-0}" -ge 1 ] 2>/dev/null; then ok "MongoDB provisioned with subscribers ($SUBS)"; else no "no subscribers provisioned"; fi

if curl -s -o /dev/null --max-time 8 -w '%{http_code}' http://localhost:5000/api/subscriber 2>/dev/null | grep -qE '^(200|401)$'; then
  ok "WebUI reachable on :5000"
else
  no "WebUI not reachable on :5000"
fi

gapd "structured JSON logs — NOT configurable in free5GC v4.2.3; documented per ADR-005 (docs/logging.md)"

echo
echo "-- End-to-end function --"

REG=$(docker exec -i "$UERANSIM" sh -c \
      'cat /tmp/ue.log /tmp/ue-b.log 2>/dev/null | grep "Initial Registration is successful" | grep -oE "20893[0-9]+" | sort -u | wc -l' \
      2>/dev/null | tr -d ' \r')
if [ "${REG:-0}" -ge "$EXPECT_UES" ] 2>/dev/null; then
  ok "UE registration: ${REG}/${EXPECT_UES} reached REGISTERED"
else
  no "UE registration: only ${REG:-0}/${EXPECT_UES}"
fi

PDU=$(docker exec -i "$DB" mongo free5gc --quiet --eval 'print(1)' >/dev/null 2>&1; echo skip)
ode "PDU session established / UE gets an IP (needs UPF + gtp5g)"
ode "end-to-end user-plane connectivity — ping through the UPF (needs UPF + gtp5g)"

# Slice consistency: every provisioned slice must be advertised by AMF, SMF, NSSF, gNB.
SLICES=$(docker exec -i "$DB" mongo free5gc --quiet --eval '
var s={};
db["subscriptionData.provisionedData.amData"].find({},{_id:0,"nssai.defaultSingleNssais":1}).forEach(function(d){
  var l=(d.nssai&&d.nssai.defaultSingleNssais)||[]; if(l.length){s[l[0].sd]=1;}
});
print(Object.keys(s).sort().join(" "));' 2>/dev/null | tr -d '\r')
sliceok=1
for sd in $SLICES; do
  for f in deployments/compose/config/amfcfg.yaml deployments/compose/config/smfcfg.yaml \
           deployments/compose/config/nssfcfg.yaml deployments/compose/config/gnbcfg.yaml; do
    grep -q "$sd" "$f" 2>/dev/null || { sliceok=0; echo "         (missing $sd in $f)"; }
  done
done
if [ -n "$SLICES" ] && [ "$sliceok" -eq 1 ]; then
  ok "modeled slice(s) [$SLICES] consistent across AMF/SMF/NSSF/gNB and subscribers"
else
  no "slice inconsistency across configs (provisioned: [$SLICES])"
fi

echo
echo "-- Understanding & documentation --"

[ -s docs/interfaces/interface-catalog.md ] && ok "Interface Catalog authored" || no "Interface Catalog missing"
[ -s diagrams/ue-registration.mmd ] && ok "Registration sequence diagram authored" || no "Registration diagram missing"
if [ -s diagrams/pdu-session-establishment.mmd ]; then
  if grep -q "DESIGN ONLY" diagrams/pdu-session-establishment.mmd; then
    ode "PDU Session diagram — authored but its data path is DESIGN-ONLY until the ODE"
  else
    ok "PDU Session sequence diagram authored from observed behaviour"
  fi
else
  no "PDU Session diagram missing"
fi
[ -s docs/runbooks/clean-install.md ] && ok "clean-install runbook present" || no "runbook missing"
ode "teardown/rebuild returns to an identical working state (prove with 'down -v' on the ODE)"

echo
echo "-- Freeze --"
if git describe --tags --exact-match >/dev/null 2>&1; then
  ok "baseline tagged ($(git describe --tags --exact-match))"
else
  ode "baseline tagged in git — Phase 1 cannot be frozen without the user plane"
fi

echo
echo "==================================================================="
printf 'PASS=%d  FAIL=%d  SKIP-ODE=%d  GAP=%d\n' "$pass" "$fail" "$skip" "$gap"
echo "==================================================================="
if [ "$fail" -gt 0 ]; then
  echo "RESULT: FAIL — runnable criteria did not all pass."
  exit 1
fi
if [ "$skip" -gt 0 ] || [ "$gap" -gt 0 ]; then
  echo "RESULT: PASS (non-baseline) — every runnable criterion passed."
  echo "        Phase 1 is NOT complete: $skip criteria require the ODE, $gap documented gap(s)."
  exit 0
fi
echo "RESULT: PASS — Phase 1 acceptance complete."
exit 0
