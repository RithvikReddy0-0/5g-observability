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

dc() { (cd "$COMPOSE_DIR" && docker compose "$@" 2>/dev/null); }

registered_count() {
  dc exec -T ueransim sh -c \
    "grep 'Initial Registration is successful' $LOG 2>/dev/null | grep -oE '20893[0-9]+' | sort -u | wc -l" \
    | tr -d ' \r'
}

case "${1:-}" in
  --stop)
    dc exec -T ueransim pkill nr-ue >/dev/null 2>&1
    echo "all nr-ue processes stopped"
    exit 0
    ;;
  --status)
    echo "UEs registered: $(registered_count) / requested $COUNT"
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

dc exec -T ueransim pkill nr-ue >/dev/null 2>&1
sleep 2
dc exec -T ueransim sh -c "rm -f $LOG" >/dev/null 2>&1
dc exec -d ueransim sh -c "./nr-ue -c $UE_CFG -n $COUNT -t $TEMPO > $LOG 2>&1" >/dev/null

echo "launched; waiting ${SETTLE}s for registration to settle ..."
sleep "$SETTLE"

got="$(registered_count)"
echo "-------------------------------------------------------------------"
echo "UEs REGISTERED: $got / $COUNT"

if [ "${got:-0}" -ge "$COUNT" ] 2>/dev/null; then
  echo "UE REGISTRATION: PASS"
  exit 0
fi
echo "UE REGISTRATION: INCOMPLETE (some UEs may still be retrying; re-check with --status)"
exit 1
