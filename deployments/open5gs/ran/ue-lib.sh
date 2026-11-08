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
# A UE may hold several sessions (ADR-020). Its home slice's session is the first in its config,
# so it is PDU session 1 (PSI 1): sessions are requested in config order. Its INTERFACE name says
# nothing: UERANSIM numbers interfaces in the order sessions finish setting up, and a URLLC device's
# eMBB session was found on uesimtun<NNNN>0 — the supervisor then pinged the wrong gateway and
# restarted healthy UEs. "The UE's address" means the PSI-1 address everywhere.
ip_of() {    # <imsi> -> the UE's latest HOME-session (PSI 1) address from its own log, or empty
  sed -n 's/.*PDU session\[1\] is successful, TUN interface\[[^,]*, \([0-9.]*\)\].*/\1/p' "/tmp/ue-$1.log" 2>/dev/null | tail -1
}

# <cfg> <block>: "<dnn> <sst> <sd>" of each session in a block of a UE config ("sessions" = the
# default ones, home first; "extraSessions" = opened on demand, ADR-020)
sessions_of() { awk -v b="$2:" '$1 == b {in_s=1; next} in_s && /^[^ #-]/ {in_s=0}
  in_s && /apn:/ {gsub(/[" ]/, "", $2); d=$2} in_s && /sst:/ {t=$2} in_s && /sd:/ {print d, t, $2}' "$CFG/$1"; }

# <dnn> -> its address pool ("10.46."), from the plan: the slice whose home session uses that DNN.
pool_of_dnn() {
  awk '{print $2, $3}' "$STATE/plan" | sort -u | while read -r c g; do
    [ "$(sessions_of "$c" sessions | head -1 | cut -d' ' -f1)" = "$1" ] && { echo "$g" | cut -d. -f1,2 | sed 's/$/./'; break; }
  done
}

# <imsi>: "<psi> <interface> <address>" of each live session of one UE, from its own log
ue_sessions() {
  live=$(ip -4 -o addr show 2>/dev/null | awk '/uesimtun/ { split($4, p, "/"); print $2 " " p[1] }')
  sed -n 's/.*PDU session\[\([0-9]*\)\] is successful, TUN interface\[\([^,]*\), \([0-9.]*\)\].*/\1 \2 \3/p' "/tmp/ue-$1.log" 2>/dev/null |
    awk '{ last[$1] = $2 " " $3 } END { for (p in last) print p, last[p] }' | while read -r psi ifc addr; do
      echo "$live" | grep -qx "$ifc $addr" && echo "$psi $ifc $addr"
    done
}

# A DNN is RELEASED while its slice is off (ue-slice.sh release): its extra sessions stay closed.
dnn_released() { grep -qx "$1" "$STATE/released" 2>/dev/null; }

# <imsi> <cfg> [force]: open each extra session that is not live and whose DNN is not released.
# Without force, at most once a minute per UE: a request takes a moment to complete, and a second
# request for the same DNN would open a duplicate session.
ensure_extra() {
  [ -n "$(sessions_of "$2" extraSessions)" ] || return 0
  now=$(date +%s); last=$(cat "$STATE/$1.extra" 2>/dev/null || echo 0)
  [ "${3:-}" = force ] || [ $((now - last)) -ge 60 ] || return 0
  echo "$now" > "$STATE/$1.extra"
  mine=$(ue_sessions "$1")
  sessions_of "$2" extraSessions | while read -r dnn sst sd; do
    dnn_released "$dnn" && continue
    pool=$(pool_of_dnn "$dnn"); [ -n "$pool" ] || continue
    echo "$mine" | awk -v p="$pool" 'index($3, p) == 1 {f=1} END {exit !f}' && continue
    timeout 8 nr-cli "imsi-$1" -e "ps-establish IPv4 --sst $sst --sd $sd --dnn $dnn" >/dev/null 2>&1
  done
}

# Every live PDU session, from the UEs' own logs: "<imsi> <psi> <interface> <address>", only those
# whose address is on an interface right now (a released session's log line stays behind).
sessions_table() {
  live=$(ip -4 -o addr show 2>/dev/null | awk '/uesimtun/ { split($4, p, "/"); print $2 " " p[1] }')
  for f in /tmp/ue-[0-9]*.log; do
    [ -f "$f" ] || continue
    i=${f#/tmp/ue-}; i=${i%.log}
    sed -n 's/.*PDU session\[\([0-9]*\)\] is successful, TUN interface\[\([^,]*\), \([0-9.]*\)\].*/\1 \2 \3/p' "$f" |
      awk -v i="$i" '{ last[$1] = $2 " " $3 } END { for (p in last) print i, p, last[p] }'
  done | while read -r i psi ifc addr; do
    echo "$live" | grep -qx "$ifc $addr" && echo "$i $psi $ifc $addr"
  done
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
