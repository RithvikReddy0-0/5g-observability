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
# Prints one line: "<op> <slice> <UEs> <up> <seconds>".
set -u
. /etc/ueransim/ue-lib.sh

op="${1:-status}"
slice="${2:-}"
[ -f "$STATE/plan" ] || { echo "error: no UE plan yet ($STATE/plan)"; exit 2; }

ues_of() { awk -v s="$1" '$4 == s { print $1 }' "$STATE/plan"; }
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
    echo "unpark $slice $(ues_of "$slice" | wc -l) $(count_up "$slice") $(( $(date +%s) - started ))"
    ;;
  status)
    prune_proc_table
    for s in $(awk '{ print $4 }' "$STATE/plan" | uniq); do
      if parked "$s"; then st=parked; else st=active; fi
      echo "$s $st $(ues_of "$s" | wc -l) $(count_up "$s")"
    done
    ;;
  *)
    echo "usage: ue-slice.sh park|unpark <slice> | status"; exit 2 ;;
esac
