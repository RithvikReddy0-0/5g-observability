#!/bin/sh
# ue-slice.sh — park or unpark one slice's UEs, for the network orchestrator (ADR-017).
#
#   docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh park   <slice>
#   docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh unpark <slice>
#   docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh status
#
# park: the slice is added to the parked list FIRST, so the supervisor in ue-entrypoint.sh stops
#   treating its UEs as failures, then every UE of the slice deregisters (switch-off), which
#   releases its session in the core, and its process is stopped. A second sweep catches a UE the
#   supervisor was restarting at that moment.
# unpark: the slice leaves the list and all its UEs start together (their own interface prefixes
#   and routing tables make that safe, ADR-014), then this waits until they are up.
# release: OTHER slices' devices drop their second session on <slice> (ADR-020) — they stay
#   registered and keep their home session; only the PDU session on <slice> is released.
# establish: those devices open their second session on <slice> again.
# sessions: every live PDU session, "<imsi> <psi> <interface> <address> <home|second>" (PSI 1 = home).
# Prints one line: "<op> <slice> <UEs> <up> <seconds>".
set -u
. /etc/ueransim/ue-lib.sh

op="${1:-status}"
slice="${2:-}"
[ -f "$STATE/plan" ] || { echo "error: no UE plan yet ($STATE/plan)"; exit 2; }

ues_of() { awk -v s="$1" '$4 == s { print $1 }' "$STATE/plan"; }
others_of() { awk -v s="$1" '$4 != s { print $1, $2, $4 }' "$STATE/plan"; }   # <imsi> <cfg> <home>
pool_of() { awk -v s="$1" '$4 == s { split($3, g, "."); print g[1] "." g[2] "."; exit }' "$STATE/plan"; }
# <imsi> <pool> [table]: the PSI of the UE's live session with an address in the pool, if any
psi_in() { printf '%s\n' "${3:-$(sessions_table)}" | awk -v i="$1" -v p="$2" '$1 == i && index($4, p) == 1 { print $2; exit }'; }
count_up() { n=0; for i in $(ues_of "$1"); do [ -n "$(tun_of "$i")" ] && n=$((n + 1)); done; echo "$n"; }

case "$op" in
  park)
    [ -n "$(ues_of "$slice")" ] || { echo "error: no UEs for slice '$slice'"; exit 2; }
    started=$(date +%s)
    grep -qx "$slice" "$STATE/parked" 2>/dev/null || echo "$slice" >> "$STATE/parked"
    prune_proc_table                    # or nr-cli may address a dead process (ue-lib.sh)
    # nr-cli answers "triggered" before the UE has sent its Deregistration Request; the UE then
    # switches itself off within about half a second. Killing it on nr-cli's answer cut most
    # deregistrations short and left their sessions in the core, so wait for the UE to exit on its
    # own and kill only a straggler. Ten at a time.
    n=0
    for i in $(ues_of "$slice"); do
      ( timeout 8 nr-cli "imsi-$i" -e "deregister switch-off" >/dev/null 2>&1
        pid=$(cat "$STATE/$i.pid" 2>/dev/null)
        for _ in 1 2 3 4 5 6; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        stop_ue "$i" ) &
      n=$((n + 1))
      [ $((n % 10)) -eq 0 ] && wait
    done
    wait
    sleep 2
    for i in $(ues_of "$slice"); do stop_ue "$i"; done
    echo "park $slice $(ues_of "$slice" | wc -l) $(count_up "$slice") $(( $(date +%s) - started ))"
    ;;
  unpark)
    [ -n "$(ues_of "$slice")" ] || { echo "error: no UEs for slice '$slice'"; exit 2; }
    started=$(date +%s)
    if [ -f "$STATE/parked" ]; then
      grep -vx "$slice" "$STATE/parked" > "$STATE/parked.new" || true
      mv "$STATE/parked.new" "$STATE/parked"
    fi
    for i in $(ues_of "$slice"); do
      kill -0 "$(cat "$STATE/$i.pid" 2>/dev/null)" 2>/dev/null || launch_ue "$i"
    done
    # shellcheck disable=SC2046
    wait_up $(ues_of "$slice")
    prune_proc_table
    jobs_extra=""
    for i in $(ues_of "$slice"); do                   # their extra sessions (ADR-020)
      ensure_extra "$i" "$(awk -v i="$i" '$1 == i { print $2; exit }' "$STATE/plan")" force &
      jobs_extra="$jobs_extra $!"
    done
    # only these jobs: the UEs just launched are children of this shell, and a bare `wait`
    # waited for them until the 180 s timeout (an activation rolled back on it)
    # shellcheck disable=SC2086
    [ -n "$jobs_extra" ] && wait $jobs_extra
    echo "unpark $slice $(ues_of "$slice" | wc -l) $(count_up "$slice") $(( $(date +%s) - started ))"
    ;;
  release)
    pool=$(pool_of "$slice"); [ -n "$pool" ] || { echo "error: no slice '$slice'"; exit 2; }
    started=$(date +%s); prune_proc_table
    hc=$(awk -v s="$slice" '$4 == s { print $2; exit }' "$STATE/plan")
    dnn=$(sessions_of "$hc" sessions | head -1 | cut -d' ' -f1)
    # first mark it released, so neither the supervisor nor a UE restart opens it again
    dnn_released "$dnn" || echo "$dnn" >> "$STATE/released"
    tab=$(sessions_table)
    for line in $(others_of "$slice" | tr ' ' '|'); do
      imsi=${line%%|*}; home=${line##*|}
      parked "$home" && continue
      psi=$(psi_in "$imsi" "$pool" "$tab"); [ -n "$psi" ] || continue
      timeout 8 nr-cli "imsi-$imsi" -e "ps-release $psi" >/dev/null 2>&1 &
    done
    wait
    for _ in $(seq 15); do                      # until no other slice's device holds the pool
      tab=$(sessions_table); left=0
      for imsi in $(others_of "$slice" | awk '{print $1}'); do [ -n "$(psi_in "$imsi" "$pool" "$tab")" ] && left=$((left + 1)); done
      [ "$left" -eq 0 ] && break; sleep 1
    done
    echo "release $slice $left $(( $(date +%s) - started ))"
    ;;
  establish)
    pool=$(pool_of "$slice"); [ -n "$pool" ] || { echo "error: no slice '$slice'"; exit 2; }
    started=$(date +%s); prune_proc_table; want=0; tab=$(sessions_table)
    hc=$(awk -v s="$slice" '$4 == s { print $2; exit }' "$STATE/plan")
    set -- $(sessions_of "$hc" sessions | head -1)     # the slice's own session: dnn sst sd
    dnn=$1; sst=$2; sd=$3
    if [ -f "$STATE/released" ]; then                 # open again from now on
      grep -vx "$dnn" "$STATE/released" > "$STATE/released.new" || true
      mv "$STATE/released.new" "$STATE/released"
    fi
    for line in $(others_of "$slice" | tr ' ' '|'); do
      imsi=${line%%|*}; rest=${line#*|}; cfg=${rest%%|*}; home=${rest#*|}
      parked "$home" && continue
      sessions_of "$cfg" extraSessions | awk -v d="$dnn" '$1 == d {f=1} END {exit !f}' || continue
      want=$((want + 1))
      [ -n "$(psi_in "$imsi" "$pool" "$tab")" ] && continue
      timeout 8 nr-cli "imsi-$imsi" -e "ps-establish IPv4 --sst $sst --sd $sd --dnn $dnn" >/dev/null 2>&1 &
    done
    wait
    for _ in $(seq 30); do
      tab=$(sessions_table); got=0
      for imsi in $(others_of "$slice" | awk '{print $1}'); do [ -n "$(psi_in "$imsi" "$pool" "$tab")" ] && got=$((got + 1)); done
      [ "$got" -ge "$want" ] && break; sleep 1
    done
    echo "establish $slice $want $got $(( $(date +%s) - started ))"
    ;;
  sessions)    # every live PDU session: <imsi> <psi> <interface> <address> <home|second>
    sessions_table | sort | awk '{ print $0, ($2 == 1 ? "home" : "second") }'
    ;;
  status)
    prune_proc_table
    for s in $(awk '{ print $4 }' "$STATE/plan" | uniq); do
      if parked "$s"; then st=parked; else st=active; fi
      echo "$s $st $(ues_of "$s" | wc -l) $(count_up "$s")"
    done
    ;;
  *)
    echo "usage: ue-slice.sh park|unpark|release|establish <slice> | status | sessions"; exit 2 ;;
esac
