#!/usr/bin/env bash
# start_ues.sh — attach the UEs (Phase 2b: 100) to the Open5GS core, a PDU session on each.
#
# eMBB 20 (DNN internet, 10.45/16), URLLC 10 (urllc, 10.46/16), mMTC 70 (iot, 10.47/16) — the
# UE container's UE_PLAN, which must match O5GS_UES in deployments/slices.env. Subscribers are
# (re-)provisioned first so every QoS value matches deployments/slices.env.
#
# Uses plain `docker`, never `docker compose`, for the same reason as the free5GC scripts:
# the compose plugin disappears whenever Docker Desktop restarts.
#
# Usage: scripts/open5gs/start_ues.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

UE=o5gs-ue
GNBS="o5gs-gnb-embb o5gs-gnb-urllc o5gs-gnb-mmtc"   # one gNB per slice (ADR-011)
UPFS="o5gs-upf-embb o5gs-upf-urllc o5gs-upf-mmtc"
WAIT="${UE_WAIT:-240}"

running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

for c in o5gs-mongodb o5gs-amf o5gs-smf $UPFS $GNBS "$UE"; do
  running "$c" || { echo "error: $c is not running — start the stack with 'make o5gs-up'"; exit 1; }
done

# The plan, from the UE container itself: <config>:<UEs>:<gateway>:<slice> per slice.
PLAN="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$UE" | sed -n 's/^UE_PLAN=//p')"
[ -n "$PLAN" ] || { echo "error: $UE has no UE_PLAN"; exit 1; }

# Only the CURRENT run of the gNB counts. `docker logs` keeps lines from before a container
# restart, so an unscoped grep would accept an NG Setup that belongs to a dead association.
# grep -c, not grep -q: under `set -o pipefail`, grep -q exits on the first match, docker logs
# dies of SIGPIPE (141), and the pipeline reports failure even though the line was found.
gnb_ng_setup() {   # <gnb container>
  local n
  n=$(docker logs --since "$(docker inspect -f '{{.State.StartedAt}}' "$1")" "$1" 2>&1 \
        | grep -c "NG Setup procedure is successful")
  [ "${n:-0}" -gt 0 ]
}
for g in $GNBS; do
  for _ in $(seq 30); do gnb_ng_setup "$g" && break; sleep 1; done
  if ! gnb_ng_setup "$g"; then
    echo "error: $g has not completed NG Setup with the AMF since it last started"
    docker logs --tail 5 "$g" 2>&1 | sed 's/^/  gnb: /'
    exit 1
  fi
done

# Every UPF must be associated with the SMF SINCE THAT UPF LAST STARTED. An association the SMF
# made with a previous UPF process looks identical in `docker ps`, but sessions sent through it
# never reach the running UPF ("Cannot find PFCP-Node"): after one deploy all 10 URLLC UEs got
# addresses and no user plane (ADR-014). The SMF re-associates on its own within ~20 s.
for u in $UPFS; do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$u")
  since=$(docker inspect -f '{{.State.StartedAt}}' "$u")
  assoc() { docker logs --since "$since" o5gs-smf 2>&1 | grep -c "PFCP associated \[$ip\]"; }
  for _ in $(seq 60); do [ "$(assoc)" -gt 0 ] && break; sleep 1; done
  if [ "$(assoc)" -eq 0 ]; then
    echo "error: the SMF has not associated with $u ($ip) since it started at $since"
    exit 1
  fi
done

echo "== provisioning subscribers"
python3 "$REPO_ROOT/scripts/open5gs/provision_subscribers.py" || exit 1

# Interfaces holding an address from a pool (gateway 10.45.0.1 -> pool 10.45.).
tuns() { docker exec "$UE" sh -c "ip -4 -o addr show | grep -c 'uesimtun.* ${1//./\\.}'" | tr -d '\r'; }

# The UEs are the o5gs-ue container's own workload (deployments/open5gs/ran/ue-entrypoint.sh),
# so that an engine restart brings them back. Restarting the container relaunches all of them,
# one process per UE, START_BATCH at a time (ADR-014).
want=0
for s in $PLAN; do n=$(echo "$s" | cut -d: -f2); want=$((want + n)); done
echo "== restarting $UE (launches $want UEs: $PLAN)"
t0=$(date +%s)
docker restart "$UE" >/dev/null || exit 1

ok=1
for s in $PLAN; do
  n=$(echo "$s" | cut -d: -f2); gw=$(echo "$s" | cut -d: -f3); name=$(echo "$s" | cut -d: -f4)
  pool="${gw%.*.*}."            # 10.45.0.1 -> 10.45.
  printf "   waiting for %s %s interfaces on %s0.0/16 " "$n" "$name" "$pool"
  got=0
  for _ in $(seq "$WAIT"); do
    got=$(tuns "$pool"); [ "${got:-0}" -ge "$n" ] && break
    printf "."; sleep 1
  done
  if [ "${got:-0}" -ge "$n" ]; then echo " ok"; else echo " timed out ($got/$n)"; ok=0; fi
done
elapsed=$(( $(date +%s) - t0 ))

logs="/tmp/ue-2089*.log"   # one log per UE: ran/ue-entrypoint.sh runs a process per UE
# One log per UE, so count the LOGS containing each line. Single-UE nr-ue logs carry no IMSI
# tag; an earlier "[<imsi>|" pattern matched nothing and reported 0/20 with 20 UEs attached.
reg=$(docker exec "$UE" sh -c "grep -l 'Initial Registration is successful' $logs 2>/dev/null | wc -l" | tr -d '\r ')
pdu=$(docker exec "$UE" sh -c "grep -l 'PDU Session establishment is successful' $logs 2>/dev/null | wc -l" | tr -d '\r ')
tunfail=$(docker exec "$UE" sh -c "cat $logs | grep -c 'TUN allocation failure'" | tr -d '\r')

echo
echo "  UEs registered        : $reg / $want"
echo "  UEs with PDU session  : $pdu / $want"
for s in $PLAN; do
  n=$(echo "$s" | cut -d: -f2); gw=$(echo "$s" | cut -d: -f3); name=$(echo "$s" | cut -d: -f4)
  pool="${gw%.*.*}."
  printf "  %-6s interfaces     : %s / %s   (%s0.0/16)\n" "$name" "$(tuns "$pool")" "$n" "$pool"
done
echo "  TUN allocation errors : $tunfail"
echo "  all attached in       : ${elapsed}s"

# Success is judged on interfaces that actually exist, not on log lines: a session can be
# accepted by the core while the UE side failed to create its interface.
if [ "$ok" -ne 1 ] || [ "$tunfail" -gt 0 ]; then
  echo
  echo "not every UE has a working PDU session. Last errors:"
  docker exec "$UE" sh -c "cat $logs | grep -iE '\[error\]|reject' | tail -8" | sed 's/^/  /'
  exit 1
fi
