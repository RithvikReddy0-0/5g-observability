#!/bin/sh
# ue-lib.sh — UE process helpers shared by ue-entrypoint.sh (start + supervise every UE) and
# ue-slice.sh (park / unpark one slice's UEs for the network orchestrator, ADR-017).
# Sourced, not run. Expects UE_PLAN and FIRST_IMSI to be set when plan() is used.

CFG=/etc/ueransim
RUN=/tmp/ue-cfg
STATE=/tmp/ues
START_WAIT="${UE_START_WAIT:-60}"

log() { echo "ue-entrypoint: $(date -u +%H:%M:%S) $*"; }

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

tag_of() { printf '%04d' $(( $1 % 10000 )); }   # <imsi> -> its last four digits

# A slice is PARKED when the network orchestrator has deactivated it (ADR-017): its UEs are
# deliberately down, so the supervisor must neither count them as failures nor restart them.
# The list lives in /tmp, which survives a restart of this container — so does the decision.
parked() {   # <slice name> -> true if parked
  grep -qx "$1" "$STATE/parked" 2>/dev/null
}

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

# nr-cli finds a UE through /tmp/UERANSIM.proc-table/, one file per nr-ue process:
# "<version x3> <pid> <cli port> <node count> <node names>". A process that dies leaves its file,
# and /tmp survives container restarts: 154 files were found for 100 UEs, four for one UE. nr-cli
# then sends a command to a stale entry's port and it is silently lost — 38 of 70 "deregister
# switch-off" commands never reached their UE. PIDs are reused across container restarts (two of
# UE 001's stale entries carried its live PID), so an entry is live only if its CLI port is bound.
prune_proc_table() {
  bound=$(awk 'NR > 1 { split($2, a, ":"); print a[2] }' /proc/net/udp /proc/net/udp6 2>/dev/null | sort -u)
  for f in /tmp/UERANSIM.proc-table/*; do
    [ -f "$f" ] || continue
    port=$(printf '%04X' "$(awk '{ print $5 }' "$f")")
    echo "$bound" | grep -qx "$port" || rm -f "$f"
  done
}

stop_ue() {   # <imsi>
  pid=$(cat "$STATE/$1.pid" 2>/dev/null)
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null
  for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
  kill -9 "$pid" 2>/dev/null
}
