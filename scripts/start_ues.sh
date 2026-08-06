#!/usr/bin/env bash
# start_ues.sh — launch N UERANSIM UEs against the running core and report how many
# reach REGISTERED.
#
# UEs run INSIDE the existing `ueransim` container (which runs nr-gnb), so the radio
# link simulation stays on 127.0.0.1 and no extra containers are needed — 20 separate
# UE containers would be wasteful on a 8 GB laptop.
#
# Prerequisites: control plane up and subscribers provisioned --
#   deployments/compose/README.md  +  scripts/provision_subscribers.sh
#
# Usage:
#   scripts/start_ues.sh            # launch COUNT UEs (default 20)
#   COUNT=5 scripts/start_ues.sh
#   scripts/start_ues.sh --status   # registration count + per-UE state
#   scripts/start_ues.sh --stop     # stop all UEs
#
# NOTE: PDU session establishment WILL fail on a non-baseline host (T3580 retransmit) --
# the UPF needs the gtp5g kernel module. Registration is the success criterion here.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/../deployments/compose" >/dev/null 2>&1 && pwd)"

COUNT="${COUNT:-20}"
TEMPO="${TEMPO:-300}"          # ms stagger between UE starts
UE_CFG="${UE_CFG:-./config/uecfg.yaml}"
SETTLE="${SETTLE:-45}"         # seconds to wait before counting
LOG=/tmp/ue.log

# Optional SECOND slice group (Phase 1.5). Set COUNT_B to launch a second nr-ue process
# with its own config, so the two groups differ only by requested S-NSSAI. Each group logs
# separately; registration is counted across both.
COUNT_B="${COUNT_B:-0}"
UE_CFG_B="${UE_CFG_B:-./config/uecfg-slice-b.yaml}"
LOG_B=/tmp/ue-b.log

dc() { (cd "$COMPOSE_DIR" && docker compose "$@" 2>/dev/null); }

count_in_log() {
  dc exec -T ueransim sh -c \
    "grep 'Initial Registration is successful' $1 2>/dev/null | grep -oE '20893[0-9]+' | sort -u | wc -l" \
    | tr -d ' \r'
}

registered_count() {
  a="$(count_in_log "$LOG")"
  a="${a:-0}"
  if [ "$COUNT_B" -gt 0 ] 2>/dev/null; then
    b="$(count_in_log "$LOG_B")"
    echo $((a + ${b:-0}))
  else
    echo "$a"
  fi
}

case "${1:-}" in
  --stop)
    dc exec -T ueransim pkill nr-ue >/dev/null 2>&1
    echo "all nr-ue processes stopped"
    exit 0
    ;;
  --status)
    _tot="$COUNT"
    [ "$COUNT_B" -gt 0 ] 2>/dev/null && _tot=$((COUNT + COUNT_B))
    echo "UEs registered: $(registered_count) / requested $_tot"
    dc exec -T ueransim ./nr-cli --dump | grep -c imsi- | xargs -I{} echo "UE instances running: {}"
    exit 0
    ;;
esac

if ! dc ps --services --filter status=running | grep -q '^ueransim$'; then
  echo "gNB (ueransim) is not running. Start it first:"
  echo "  cd deployments/compose && docker compose up -d --no-deps ueransim"
  exit 1
fi

echo "==================================================================="
echo "launching $COUNT UE(s)  (stagger ${TEMPO}ms, settle ${SETTLE}s)"
echo "==================================================================="

TOTAL="$COUNT"
[ "$COUNT_B" -gt 0 ] 2>/dev/null && TOTAL=$((COUNT + COUNT_B))

dc exec -T ueransim pkill nr-ue >/dev/null 2>&1
sleep 2
dc exec -T ueransim sh -c "rm -f $LOG $LOG_B" >/dev/null 2>&1
dc exec -d ueransim sh -c "./nr-ue -c $UE_CFG -n $COUNT -t $TEMPO > $LOG 2>&1" >/dev/null
if [ "$COUNT_B" -gt 0 ] 2>/dev/null; then
  echo "  group A: $COUNT UE(s) via $UE_CFG"
  echo "  group B: $COUNT_B UE(s) via $UE_CFG_B"
  sleep 2
  dc exec -d ueransim sh -c "./nr-ue -c $UE_CFG_B -n $COUNT_B -t $TEMPO > $LOG_B 2>&1" >/dev/null
fi

echo "launched; waiting ${SETTLE}s for registration to settle ..."
sleep "$SETTLE"

got="$(registered_count)"
echo "-------------------------------------------------------------------"
echo "UEs REGISTERED: $got / $TOTAL"

if [ "${got:-0}" -ge "$TOTAL" ] 2>/dev/null; then
  echo "UE REGISTRATION: PASS"
  exit 0
fi
echo "UE REGISTRATION: INCOMPLETE (some UEs may still be retrying; re-check with --status)"
exit 1
