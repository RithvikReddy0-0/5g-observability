#!/bin/sh
# gnb-entrypoint.sh <config> — run one UERANSIM gNB, and exit when its N2 link to the AMF is down,
# so that Docker (restart: unless-stopped) or Kubernetes (restartPolicy: Always) starts a fresh one.
#
# WHY: UERANSIM v3.3.0's gNB connects to the AMF once. A refused connection is logged
# ("Connecting to ... failed", src/gnb/sctp/task.cpp) and never retried; an association that
# ends later is dropped without reconnecting (handleAssociationShutdown, src/gnb/ngap/
# interface.cpp). The process stays up either way, with its cell barred.
# After a Docker engine restart every container starts at once: two of the three gNBs reached the
# AMF before it listened, logged "Connection refused" and stayed like that — 80 of 100 UEs could
# not attach, and nothing restarted them (ADR-014).
#
# So: no NG Setup within NG_SETUP_WAIT seconds, or "Association terminated" later -> exit 1.
set -u

CFG="$1"
WAIT="${NG_SETUP_WAIT:-30}"
LOG=/tmp/gnb.log
: > "$LOG"

nr-gnb -c "$CFG" < /dev/null > "$LOG" 2>&1 &
PID=$!
tail -n +1 -F "$LOG" 2>/dev/null &     # keep the gNB's own output in `docker logs`
TAIL=$!

say()  { echo "gnb-entrypoint: $*"; }
stop() { kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; kill "$TAIL" 2>/dev/null; }
trap 'stop; exit 143' TERM INT

w=0
until grep -q "NG Setup procedure is successful" "$LOG"; do
  if ! kill -0 "$PID" 2>/dev/null; then say "nr-gnb exited before NG Setup"; stop; exit 1; fi
  if [ "$w" -ge "$WAIT" ]; then
    say "no NG Setup with the AMF within ${WAIT}s — exiting so this gNB is restarted"; stop; exit 1
  fi
  w=$((w + 1)); sleep 1
done
say "NG Setup done; watching the N2 association"

# Only the recent end of the log is searched: it grows with every UE event.
while kill -0 "$PID" 2>/dev/null; do
  if tail -c 65536 "$LOG" | grep -q "Association terminated"; then
    say "N2 association to the AMF lost — exiting so this gNB is restarted"; stop; exit 1
  fi
  sleep 2
done
say "nr-gnb exited"; stop; exit 1
