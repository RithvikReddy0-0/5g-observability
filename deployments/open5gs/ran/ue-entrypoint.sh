#!/bin/sh
# ue-entrypoint.sh — run and SUPERVISE the UEs (Phase 2b: 100) as this container's own workload.
#
# WHY THE UEs LIVE HERE AND NOT IN `docker exec`
# The Docker engine on this host runs inside the Ubuntu WSL distro, and WSL shuts that distro
# down — engine included — soon after the last terminal closes (docs/open5gs.md, "Engine
# restarts"). restart: unless-stopped brings every container back, but processes started
# with `docker exec` are not part of a container's definition and are simply gone. Launched
# from the entrypoint, the UEs come back with the container. No re-provisioning is needed:
# when a UE's SQN is out of step, Open5GS completes AUTS resynchronisation on the retry.
#
# WHY ONE nr-ue PROCESS PER UE, AND WHY SUPERVISED
# UERANSIM declares radio link failure when a UE hears nothing from the gNB for 2 s
# (HEARTBEAT_THRESHOLD in src/ue/rls/udp_task.cpp). The UE then recovers its registration with
# a Service Request, but its user plane does not come back: the gNB logs "Uplink data failure,
# PDU session not found" for every packet, while the UE still shows CM-CONNECTED and the core
# still counts the session. Only probing from the UE side reveals it.
#
# So each UE is its own process (`nr-ue -i <imsi>`), and every CHECK_EVERY seconds each one
# pings its slice's UPF gateway through its own interface. A UE that fails CHECK_STRIKES
# checks in a row is restarted on its own — every other UE is untouched.
#
# WHY UEs CAN START IN PARALLEL (ADR-014)
# nr-ue names its interface <prefix><first free index>, and picks a policy-routing table id by
# reading /etc/iproute2/rt_tables and appending the first free one. With a shared prefix, UEs
# starting together raced for the same interface name ("TUNSETIFF: Device or resource busy"),
# so 20 UEs were started one at a time. For 100 that is minutes after every engine restart.
# Now every UE gets its OWN interface prefix (tunName, from its IMSI) and its routing table is
# registered here, with a unique id, before any UE starts.
# A third shared thing: every nr-ue registers itself in /tmp/UERANSIM.proc-table/ (for nr-cli),
# creating the directory if it is missing — check, then mkdir, and a hard failure on EEXIST
# (io::CreateDirectory). On a fresh container the first UEs to start together raced for it and
# the losers ABORTED ("could not be created: File exists"): 8 of 25 in one burst. The directory is
# created here first, so nothing is left to race for.
# START_BATCH UEs are started at once; START_BATCH=1 restores one at a time.
set -u

CFG=/etc/ueransim
RUN=/tmp/ue-cfg
CHECK_EVERY="${CHECK_EVERY:-10}"
CHECK_STRIKES="${CHECK_STRIKES:-3}"
START_WAIT="${UE_START_WAIT:-60}"
START_BATCH="${START_BATCH:-10}"
FIRST_IMSI="${FIRST_IMSI:-208930000000001}"
STATE=/tmp/ues
mkdir -p "$STATE" "$RUN"
mkdir -p /tmp/UERANSIM.proc-table && chmod 777 /tmp/UERANSIM.proc-table   # see "a third shared thing"

# Slice plan, in IMSI order: <config>:<UEs>:<gateway>:<name>. IMSIs are assigned contiguously
# from FIRST_IMSI in this order — the same rule as scripts/open5gs/provision_subscribers.py, and
# the counts must equal O5GS_UES in deployments/slices.env (tools/check_open5gs_slices.py).
UE_PLAN="${UE_PLAN:-ue-slice-a.yaml:20:10.45.0.1:eMBB ue-slice-b.yaml:10:10.46.0.1:URLLC ue-slice-c.yaml:70:10.47.0.1:mMTC}"

log() { echo "ue-entrypoint: $(date -u +%H:%M:%S) $*"; }

# Per-slice reachability/latency exporter on :9121, measured from these UEs' own interfaces.
# Runs here rather than as a sidecar: a sidecar sharing this network namespace is stranded
# in a dead one when this container restarts. Respawned if it ever exits.
( while true; do python3 -u /opt/o5gs-obs/ue_probe_exporter.py; sleep 2; done ) &

plan() {   # every UE, one per line: <imsi> <config> <gateway> <slice>
  imsi=$FIRST_IMSI
  for s in $UE_PLAN; do
    cfg=${s%%:*}; rest=${s#*:}; n=${rest%%:*}; rest=${rest#*:}; gw=${rest%%:*}; name=${rest#*:}
    k=0
    while [ "$k" -lt "$n" ]; do
      echo "$imsi $cfg $gw $name"
      imsi=$((imsi + 1)); k=$((k + 1))
    done
  done
}

total=$(plan | wc -l)
if [ "$total" -le 0 ]; then
  log "UE_PLAN is empty, idling"; exec sleep infinity
fi

tag_of() { printf '%04d' $(( $1 % 10000 )); }   # <imsi> -> its last four digits

# Each UE's own interface prefix, "uesimtun" + the last four digits of its IMSI (UERANSIM allows
# 12 characters). Its one session is then uesimtun<NNNN>0 — still matched by every tool that
# looks for "uesimtun" — and no prefix is the start of another, so no two UEs can collide.
# The routing table nr-ue will ask for is rt_<interface>; give each a unique id up front, from
# 2000 up — nr-ue itself allocates from 1000, so an entry left by an older run cannot collide.
plan | while read -r imsi cfg gw name; do
  t=$(tag_of "$imsi")
  { cat "$CFG/$cfg"; echo; echo "tunName: \"uesimtun$t\""; } > "$RUN/$imsi.yaml"
  grep -q "[[:space:]]rt_uesimtun${t}0\$" /etc/iproute2/rt_tables 2>/dev/null \
    || printf '%d\trt_uesimtun%s0\n' $(( 2000 + imsi % 10000 )) "$t" >> /etc/iproute2/rt_tables
done

# A UE is identified by its PDU-session ADDRESS, never by its interface NAME. With the old shared
# prefix, names were reused after a session dropped, and checking by name once marked two dead
# UEs healthy. Addresses are allocated uniquely by the SMF, so an address can only ever mean one
# live session; with per-UE prefixes this is now doubly safe.
ip_of() {    # <imsi> -> the UE's latest session address from its own log, or empty
  sed -n 's/.*TUN interface\[uesimtun[0-9]*, \([0-9.]*\)\].*/\1/p' "/tmp/ue-$1.log" 2>/dev/null | tail -1
}

tun_of() {   # <imsi> -> the interface CURRENTLY holding that UE's address, or empty
  a=$(ip_of "$1")
  [ -n "$a" ] || return 0
  ip -4 -o addr show 2>/dev/null | awk -v a="$a" '/uesimtun/ { split($4, p, "/"); if (p[1] == a) { print $2; exit } }'
}

launch_ue() {   # <imsi>: start it, do not wait
  : > "/tmp/ue-$1.log"
  nr-ue -c "$RUN/$1.yaml" -i "imsi-$1" < /dev/null >> "/tmp/ue-$1.log" 2>&1 &
  echo $! > "$STATE/$1.pid"
  echo 0 > "$STATE/$1.strikes"
}

wait_up() {     # <imsi>...: wait until each has its interface, has died, or START_WAIT passes
  w=0
  while [ "$w" -lt "$START_WAIT" ]; do
    pending=0
    for i in "$@"; do
      [ -n "$(tun_of "$i")" ] && continue
      kill -0 "$(cat "$STATE/$i.pid")" 2>/dev/null && pending=$((pending + 1))
    done
    [ "$pending" -eq 0 ] && return 0
    w=$((w + 1)); sleep 1
  done
}

stop_ue() {   # <imsi>
  pid=$(cat "$STATE/$1.pid" 2>/dev/null)
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
  kill -9 "$pid" 2>/dev/null
}

log "starting $total UEs ($UE_PLAN), $START_BATCH at a time"
started=$(date +%s)
plan > "$STATE/plan"
batch=""
n=0
while read -r imsi cfg gw name; do
  launch_ue "$imsi"
  batch="$batch $imsi"; n=$((n + 1))
  if [ "$n" -ge "$START_BATCH" ]; then
    # shellcheck disable=SC2086
    wait_up $batch; batch=""; n=0
  fi
done < "$STATE/plan"
# shellcheck disable=SC2086
[ -n "$batch" ] && wait_up $batch

up=0
while read -r imsi cfg gw name; do
  if [ -n "$(tun_of "$imsi")" ]; then up=$((up + 1))
  else log "$name imsi-$imsi did not come up within ${START_WAIT}s; the supervisor will retry"; fi
done < "$STATE/plan"
log "$up/$total UEs up in $(( $(date +%s) - started ))s"
echo "$up $total $(( $(date +%s) - started )) $START_BATCH" > "$STATE/startup"

log "supervising: ping each UE's gateway every ${CHECK_EVERY}s, restart after ${CHECK_STRIKES} misses"
while true; do
  sleep "$CHECK_EVERY"
  while read -r imsi cfg gw name; do
    pid=$(cat "$STATE/$imsi.pid" 2>/dev/null)
    t=$(tun_of "$imsi")
    if ! kill -0 "$pid" 2>/dev/null; then
      reason="process exited"
    elif [ -z "$t" ]; then
      reason="its session address $(ip_of "$imsi") is on no interface"
    elif ping -I "$t" -c 1 -W 2 "$gw" >/dev/null 2>&1; then
      echo 0 > "$STATE/$imsi.strikes"; continue
    else
      reason="no reply through the UPF on $t ($(ip_of "$imsi"))"
    fi
    s=$(( $(cat "$STATE/$imsi.strikes" 2>/dev/null || echo 0) + 1 ))
    echo "$s" > "$STATE/$imsi.strikes"
    if [ "$s" -ge "$CHECK_STRIKES" ]; then
      log "$name imsi-$imsi unhealthy ($reason, $s checks): restarting this UE only"
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) imsi-$imsi $name $reason" >> "$STATE/restarts.log"
      stop_ue "$imsi"
      launch_ue "$imsi"
      wait_up "$imsi"
      if [ -n "$(tun_of "$imsi")" ]; then log "$name imsi-$imsi back on $(tun_of "$imsi")"
      else log "$name imsi-$imsi restart did not complete; will retry"; fi
    fi
  done < "$STATE/plan"
done
