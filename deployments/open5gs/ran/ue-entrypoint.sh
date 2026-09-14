#!/bin/sh
# ue-entrypoint.sh — run and SUPERVISE the 20 UEs as this container's own workload.
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
# (HEARTBEAT_THRESHOLD in src/ue/rls/udp_task.cpp) — any 2-second stall of the simulator is
# enough. The UE then recovers its registration with a Service Request, but its user plane
# does not come back: the gNB logs "Uplink data failure, PDU session not found" for every
# packet, while the UE still shows CM-CONNECTED and the core still counts the session.
# Observed for 3 of 20 UEs within 20 minutes; only probing from the UE side revealed it.
#
# So each UE is its own process (`nr-ue -i <imsi>`), and every CHECK_EVERY seconds each one
# pings its slice's UPF gateway through its own interface. A UE that fails CHECK_STRIKES
# checks in a row is restarted on its own — the other 19 are untouched.
#
# UEs are started ONE AT A TIME: nr-ue takes the next free uesimtunN name, and processes
# starting together race for the same name ("TUNSETIFF: Device or resource busy").
set -u

N="${UES_PER_SLICE:-10}"
CFG=/etc/ueransim
CHECK_EVERY="${CHECK_EVERY:-10}"
CHECK_STRIKES="${CHECK_STRIKES:-3}"
START_WAIT="${UE_START_WAIT:-30}"
STATE=/tmp/ues
mkdir -p "$STATE"

# Slice plan: <config>:<first imsi>:<gateway>:<name>. Matches deployments/slices.env and the
# address pools in config/smf.yaml.
SLICES="ue-slice-a.yaml:208930000000001:10.45.0.1:eMBB ue-slice-b.yaml:208930000000011:10.46.0.1:URLLC"

log() { echo "ue-entrypoint: $*"; }

# Per-slice reachability/latency exporter on :9121, measured from these UEs' own interfaces.
# Runs here rather than as a sidecar: a sidecar sharing this network namespace is stranded
# in a dead one when this container restarts. Respawned if it ever exits.
( while true; do python3 -u /opt/o5gs-obs/ue_probe_exporter.py; sleep 2; done ) &

if [ "$N" -le 0 ]; then
  log "UES_PER_SLICE=0, idling"; exec sleep infinity
fi

# A UE is identified by its PDU-session ADDRESS, never by its interface NAME. Names are reused:
# when a session drops, its uesimtunN is destroyed and the next session anywhere may take the
# same name. Checking by name once marked two dead UEs healthy — their logs named an interface
# that now belonged to another UE, and pings through it succeeded. Addresses are allocated
# uniquely by the SMF, so an address can only ever mean one live session.
ip_of() {    # <imsi> -> the UE's latest session address from its own log, or empty
  sed -n 's/.*TUN interface\[uesimtun[0-9]*, \([0-9.]*\)\].*/\1/p' "/tmp/ue-$1.log" 2>/dev/null | tail -1
}

tun_of() {   # <imsi> -> the interface CURRENTLY holding that UE's address, or empty
  a=$(ip_of "$1")
  [ -n "$a" ] || return 0
  ip -4 -o addr show 2>/dev/null | awk -v a="$a" '/uesimtun/ { split($4, p, "/"); if (p[1] == a) { print $2; exit } }'
}

start_ue() {   # <imsi> <config>
  : > "/tmp/ue-$1.log"
  nr-ue -c "$CFG/$2" -i "imsi-$1" >> "/tmp/ue-$1.log" 2>&1 &
  echo $! > "$STATE/$1.pid"
  echo 0 > "$STATE/$1.strikes"
  # Wait for this UE's interface before starting the next one (TUN name race).
  w=0
  while [ "$w" -lt "$START_WAIT" ]; do
    [ -n "$(tun_of "$1")" ] && return 0
    kill -0 "$(cat "$STATE/$1.pid")" 2>/dev/null || return 1
    w=$((w + 1)); sleep 1
  done
  return 1
}

stop_ue() {   # <imsi>
  pid=$(cat "$STATE/$1.pid" 2>/dev/null)
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
  kill -9 "$pid" 2>/dev/null
}

plan() {   # every UE, one per line: <imsi> <config> <gateway> <slice>
  for s in $SLICES; do
    cfg=${s%%:*}; rest=${s#*:}; first=${rest%%:*}; rest=${rest#*:}; gw=${rest%%:*}; name=${rest#*:}
    k=0
    while [ "$k" -lt "$N" ]; do
      echo "$((first + k)) $cfg $gw $name"
      k=$((k + 1))
    done
  done
}

log "starting $N UEs per slice, one process each"
plan | while read -r imsi cfg gw name; do
  if start_ue "$imsi" "$cfg"; then log "$name imsi-$imsi up on $(tun_of "$imsi")"
  else log "$name imsi-$imsi did not come up within ${START_WAIT}s; the supervisor will retry"; fi
done

log "supervising: ping each UE's gateway every ${CHECK_EVERY}s, restart after ${CHECK_STRIKES} misses"
while true; do
  sleep "$CHECK_EVERY"
  plan | while read -r imsi cfg gw name; do
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
      if start_ue "$imsi" "$cfg"; then log "$name imsi-$imsi back on $(tun_of "$imsi")"
      else log "$name imsi-$imsi restart did not complete; will retry"; fi
    fi
  done
done
