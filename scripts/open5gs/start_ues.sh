#!/usr/bin/env bash
# start_ues.sh — attach 20 UEs to the Open5GS core and open a PDU session on each.
#
# 10 UEs on slice A (eMBB, DNN internet) and 10 on slice B (URLLC, DNN urllc). Subscribers
# are (re-)provisioned first so every QoS value matches deployments/slices.env.
#
# Uses plain `docker`, never `docker compose`, for the same reason as the free5GC scripts:
# the compose plugin disappears whenever Docker Desktop restarts.
#
# Usage: scripts/open5gs/start_ues.sh
# The per-slice count is UES_PER_SLICE in deployments/open5gs/docker-compose.yaml (10).

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

N="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' o5gs-ue 2>/dev/null | sed -n 's/^UES_PER_SLICE=//p')"
N="${N:-10}"
UE=o5gs-ue
GNB=o5gs-gnb
WAIT="${UE_WAIT:-90}"

running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

for c in o5gs-mongodb o5gs-amf o5gs-smf o5gs-upf "$GNB" "$UE"; do
  running "$c" || { echo "error: $c is not running — start the stack with 'make o5gs-up'"; exit 1; }
done

# Only the CURRENT run of the gNB counts. `docker logs` keeps lines from before a container
# restart, so an unscoped grep would accept an NG Setup that belongs to a dead association.
# grep -c, not grep -q: under `set -o pipefail`, grep -q exits on the first match, docker logs
# dies of SIGPIPE (141), and the pipeline reports failure even though the line was found.
gnb_ng_setup() {
  local n
  n=$(docker logs --since "$(docker inspect -f '{{.State.StartedAt}}' "$GNB")" "$GNB" 2>&1 \
        | grep -c "NG Setup procedure is successful")
  [ "${n:-0}" -gt 0 ]
}
for _ in $(seq 30); do gnb_ng_setup && break; sleep 1; done
if ! gnb_ng_setup; then
  echo "error: the gNB has not completed NG Setup with the AMF since it last started"
  docker logs --tail 5 "$GNB" 2>&1 | sed 's/^/  gnb: /'
  exit 1
fi

echo "== provisioning subscribers"
python3 "$REPO_ROOT/scripts/open5gs/provision_subscribers.py" || exit 1

# Count UE interfaces holding an address from a pool.
tuns() { docker exec "$UE" sh -c "ip -4 -o addr show | grep -c 'uesimtun.* $1'" | tr -d '\r'; }

# Wait until <count> interfaces in <pool> exist.
wait_tuns() {   # <pool-regex> <count> <label>
  printf "   waiting for %s interfaces on %s " "$2" "$3"
  for _ in $(seq "$WAIT"); do
    [ "$(tuns "$1")" -ge "$2" ] && { echo " ok"; return 0; }
    printf "."; sleep 1
  done
  echo " timed out ($(tuns "$1")/$2)"; return 1
}

# The UEs are the o5gs-ue container's own workload (deployments/open5gs/ran/ue-entrypoint.sh),
# so that an engine restart brings them back. Restarting the container relaunches all 20, one
# process per UE, started one at a time (processes starting together race for TUN names).
echo "== restarting $UE (launches $N UEs per slice)"
docker restart "$UE" >/dev/null || exit 1
wait_tuns '10\.45\.' "$N" "slice A (eMBB)"
wait_tuns '10\.46\.' "$N" "slice B (URLLC)"

want=$((N * 2))
logs="/tmp/ue-2089*.log"   # one log per UE: ran/ue-entrypoint.sh runs a process per UE
reg=$(docker exec "$UE" sh -c "cat $logs | grep 'Initial Registration is successful' | grep -oE '\[[0-9]{15}' | sort -u | wc -l" | tr -d '\r ')
pdu=$(docker exec "$UE" sh -c "cat $logs | grep 'PDU Session establishment is successful' | grep -oE '\[[0-9]{15}' | sort -u | wc -l" | tr -d '\r ')
tunfail=$(docker exec "$UE" sh -c "cat $logs | grep -c 'TUN allocation failure'" | tr -d '\r')
tuns_a=$(tuns '10\.45\.'); tuns_b=$(tuns '10\.46\.')

echo
echo "  UEs registered        : $reg / $want"
echo "  UEs with PDU session  : $pdu / $want"
echo "  slice A interfaces    : $tuns_a / $N   (10.45.0.0/16, eMBB)"
echo "  slice B interfaces    : $tuns_b / $N   (10.46.0.0/16, URLLC)"
echo "  TUN allocation errors : $tunfail"

# Success is judged on interfaces that actually exist, not on log lines: a session can be
# accepted by the core while the UE side failed to create its interface.
if [ "$tuns_a" -lt "$N" ] || [ "$tuns_b" -lt "$N" ] || [ "$tunfail" -gt 0 ]; then
  echo
  echo "not every UE has a working PDU session. Last errors:"
  docker exec "$UE" sh -c "cat $logs | grep -iE '\[error\]|reject' | tail -8" | sed 's/^/  /'
  exit 1
fi
