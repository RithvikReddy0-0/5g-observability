#!/usr/bin/env bash
# orchestrator_dynamic.sh — the Phase 2b orchestrator extensions on real traffic (ADR-016).
#
#   1. priority    URLLC is filled by its standard devices (ARP 2). A standard demand is then
#                  refused; demands from the critical devices (ARP 1) are admitted by pre-empting
#                  standard flows, whose real traffic is stopped. Outcomes per ARP level.
#   2. controller  the same saturating demand twice, back to back: CONTROL=0 (admit up to the
#                  policy capacity, 500 Mbps on eMBB) and CONTROL=1 (the feedback controller moves
#                  eMBB's admission limit from URLLC p95 latency and eMBB delivery). Compared on
#                  URLLC latency over the run, eMBB flows that received their rate, and refusals.
#   3. planning    which slices the demand observed in run 2 needs, and two given demand sets.
#   4. diagnosis   what inflates URLLC's latency tail: the same generator idle, with URLLC demand
#                  only, and with eMBB demand only (controller off). Explains what the controller
#                  must throttle.
#
# Every run records the host load before and after, and checks that no flow process outlived its
# flow (leaked iperf3 clients once loaded the host through whole experiments, ADR-016).
#
# Output: docs/evidence/open5gs-orchestrator-dynamic-<UTC timestamp>/
# Usage:  scripts/open5gs/orchestrator_dynamic.sh [seconds-per-controller-run=180] [demands-per-second=3]
#         ONLY=priority|controller|planning|diagnosis runs one experiment.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
D="${1:-180}"
RATE="${2:-3}"
ONLY="${ONLY:-}"
OUT="${OUT:-docs/evidence/open5gs-orchestrator-dynamic-$(date -u +%Y%m%d-%H%M%SZ)}"
mkdir -p "$OUT"
ORCH=http://localhost:9111
URLLC_KPI=http://10.53.0.52:9130
PROM=http://localhost:9091
MIX="${MIX:-video:3 file:3 blog:1 control:1}"

restart() {   # restart the orchestrator with fresh state; extra env as arguments
  bash scripts/open5gs/start_orchestrator.sh --stop >/dev/null
  env "$@" bash scripts/open5gs/start_orchestrator.sh >/dev/null || { echo "orchestrator failed to start" >&2; exit 1; }
}

leaks() {   # flow processes older than any flow should live (40 s flows + margins)
  ps -eo etimes=,args= | awk '(/[i]perf3/ || /[p]ython3 - (send|recv)/) && $1 > 90' | wc -l
}

load1() { cut -d' ' -f1 /proc/loadavg; }

req() {   # <supi> <class> -> one line
  curl -s -X POST "$ORCH/request" -H 'Content-Type: application/json' \
    -d "{\"supi\":\"$1\",\"traffic_class\":\"$2\"}" | python3 -c "import json,sys
r = json.load(sys.stdin)
if r.get('admitted'):
    pre = ', '.join('%s ARP %d' % (v['cls'], v['arp']) for v in r.get('preempted', []))
    print('ADMIT  %s ARP %d %-7s -> %s  %.1f/%.0f Mbps%s' % ('$1'[-3:], r['arp']['priority'], r['traffic_class'],
          r['slice']['name'], r['slice_used_mbps'], r['slice_limit_mbps'], ('  pre-empted: ' + pre) if pre else ''))
else:
    print('REFUSE %s %-7s  %s' % ('$1'[-3:], '$2', r.get('reason')))"
}

{
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "git: $(git rev-parse --short HEAD) $(git diff --quiet || echo '(working tree modified)')"
  echo "controller_run_seconds: $D"; echo "demands_per_second: $RATE"; echo "mix: $MIX"
  echo "ue_sessions: $(docker exec o5gs-ue ip -4 -o addr show | grep -c uesimtun)"
  echo "host_load: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "free5gc_running: $(docker ps --format '{{.Names}}' | grep -cv '^o5gs-') containers (left up, as in the KPI baseline)"
} > "$OUT/meta.txt"

# ───────────────────────────── 1. priority inside URLLC ─────────────────────────────
if [ -z "$ONLY" ] || [ "$ONLY" = priority ]; then
  restart FLOW_SECONDS=40 CONTROL=0
  {
    echo "== experiment 1: priority inside the URLLC slice (ARP tiers)"
    echo "   critical devices ...021-023: ARP 1, may pre-empt, not pre-emptable"
    echo "   standard devices ...024-030: ARP 2, may not pre-empt, pre-emptable"
    echo "   each control demand is 1 Mbps for 40 s; URLLC admits 20 Mbps"
    echo
    echo "-- the standard devices fill the slice"
    for i in $(seq 0 19); do req "imsi-2089300000000$((24 + i % 7))" control; done
    echo
    echo "-- one more standard demand (an equal priority cannot pre-empt)"
    req imsi-208930000000027 control
    echo
    echo "-- critical demands"
    for i in 1 2 3 1 2; do req "imsi-20893000000002$i" control; done
    echo
    echo "-- a standard demand again"
    req imsi-208930000000025 control
    echo
    echo "-- IoT devices: sensor reports go to mMTC"
    for i in 31 55 100; do req "imsi-208930000000$(printf %03d $i)" sensor; done
  } > "$OUT/1-priority.txt" 2>&1
  cat "$OUT/1-priority.txt"
  echo "   waiting for the flows to end"; sleep 55
  {
    echo
    echo "--- every executed flow, by requester ARP ---"
    curl -s "$ORCH/flows" | python3 -c "import json,sys,collections
f = json.load(sys.stdin)
by = collections.defaultdict(collections.Counter)
for r in f: by[(r['slice'], r['arp'])][r['outcome']] += 1
for (sl, arp), c in sorted(by.items()):
    print('  %-6s ARP %-2d  %s' % (sl, arp, '  '.join('%s %d' % kv for kv in sorted(c.items()))))
pre = [r for r in f if r['outcome'] == 'preempted']
print('  pre-empted flows delivered before being stopped: %s' % ', '.join('%.2f' % r['delivered_mbps'] for r in pre))"
    echo
    echo "--- decisions by priority (orchestrator metrics) ---"
    curl -s "$ORCH/metrics" | grep -E '^slice_(decisions_by_priority|preempted)_total' | sed 's/^/  /'
  } >> "$OUT/1-priority.txt" 2>&1
  tail -14 "$OUT/1-priority.txt"
fi

# ───────────────────────────── 2. feedback controller ─────────────────────────────
controller_run() {   # <label> <CONTROL 0|1>
  local label="$1" ctl="$2" f="$OUT/2-controller-$1.txt"
  restart FLOW_SECONDS=20 CONTROL="$ctl"
  curl -s "$URLLC_KPI/run/start" >/dev/null
  local t0; t0=$(date +%s)
  local l0; l0=$(load1)
  echo "== controller run '$label' (CONTROL=$ctl): ${D}s at ${RATE} demands/s, mix $MIX" | tee "$f"
  ( cd tools/slice-orchestrator && CORE=open5gs DB_CONTAINER=o5gs-mongodb ORCH_URL="$ORCH" \
      DURATION="$D" RATE="$RATE" MIX="$MIX" python3 traffic_gen.py ) >> "$f" 2>&1
  curl -s "$URLLC_KPI/run/stats" > "$OUT/2-urllc-kpi-$label.json"
  sleep 30                                   # the last flows finish
  curl -s "$ORCH/flows" > "$OUT/2-flows-$label.json"
  curl -s "$ORCH/control" > "$OUT/2-control-log-$label.json"
  curl -s "$ORCH/metrics" > "$OUT/2-metrics-$label.txt"
  local t1; t1=$(date +%s)
  for m in slice_admission_limit_mbps slice_measured_mbps slice_allocated_mbps; do
    curl -s --get "$PROM/api/v1/query_range" --data-urlencode "query=${m}{slice=\"eMBB\"}" \
      --data-urlencode "start=$t0" --data-urlencode "end=$t1" --data-urlencode "step=5" \
      > "$OUT/2-series-$label-$m.json"
  done
  curl -s --get "$PROM/api/v1/query_range" --data-urlencode 'query=kpi_owd_recent_ms{slice="URLLC",stat="p95"}' \
    --data-urlencode "start=$t0" --data-urlencode "end=$t1" --data-urlencode "step=5" \
    > "$OUT/2-series-$label-urllc_p95.json"
  echo "$label $t0 $t1 load_before=$l0 load_after=$(load1) leaked_flow_processes=$(leaks)" >> "$OUT/2-runs.txt"
  echo "   done; $(grep -c ADMIT "$f") admitted, $(grep -c REFUSE "$f") refused"
}

if [ -z "$ONLY" ] || [ "$ONLY" = controller ]; then
  : > "$OUT/2-runs.txt"
  controller_run static 0
  sleep 20                                   # the slices drain between runs
  controller_run controlled 1
  python3 - "$OUT" "$D" > "$OUT/2-comparison.txt" <<'PY'
import json, os, statistics, sys
out, dur = sys.argv[1], float(sys.argv[2])
def series(label, m):
    try:
        r = json.load(open(os.path.join(out, "2-series-%s-%s.json" % (label, m))))["data"]["result"]
        return [float(v) for _, v in r[0]["values"]] if r else []
    except Exception:
        return []
runs = {}
for line in open(os.path.join(out, "2-runs.txt")):
    parts = line.split()
    runs[parts[0]] = dict(kv.split("=") for kv in parts[3:])
rows = []
for label in ("static", "controlled"):
    flows = json.load(open(os.path.join(out, "2-flows-%s.json" % label)))
    k = json.load(open(os.path.join(out, "2-urllc-kpi-%s.json" % label)))
    gen = open(os.path.join(out, "2-controller-%s.txt" % label)).read().splitlines()
    log = json.load(open(os.path.join(out, "2-control-log-%s.json" % label)))
    embb = [f for f in flows if f["slice"] == "eMBB"]
    urllc = [f for f in flows if f["slice"] == "URLLC"]
    met = lambda fs: (100.0 * sum(f["outcome"] == "met" for f in fs) / len(fs)) if fs else float("nan")
    # Data actually delivered to devices, as an average rate over the run.
    delivered = sum(f["delivered_mbps"] for f in embb) * 20 / dur
    owd = k.get("owd_ms", {})
    lim = series(label, "slice_admission_limit_mbps")
    meas = series(label, "slice_measured_mbps")
    rows.append([label,
                 sum(l.startswith("  ADMIT") and " eMBB " in l for l in gen),
                 sum(l.startswith("  REFUSE") and "eMBB" in l for l in gen),
                 len(embb), met(embb), delivered,
                 max(meas) if meas else float("nan"),
                 "%.0f-%.0f" % (min(lim), max(lim)) if lim else "-",
                 sum(1 for e in log if e["action"] == "cut"), sum(1 for e in log if e["action"] == "raise"),
                 met(urllc), owd.get("mean"), owd.get("p95"), owd.get("p99"), k.get("loss_ratio"),
                 "%s -> %s" % (runs[label].get("load_before"), runs[label].get("load_after")),
                 runs[label].get("leaked_flow_processes")])
hdr = ["run", "eMBB admitted", "eMBB refused", "eMBB flows", "eMBB flows met %",
       "eMBB delivered (avg Mbps)", "eMBB measured peak", "eMBB limit range", "cuts", "raises",
       "URLLC flows met %", "URLLC probe mean ms", "p95 ms", "p99 ms", "probe loss",
       "host load 1 min, before -> after", "leaked flow processes after"]
print("Feedback controller: the same demand with CONTROL=0 and CONTROL=1\n")
for i, h in enumerate(hdr):
    vals = []
    for r in rows:
        v = r[i]
        vals.append(("%.2f" % v) if isinstance(v, float) else str(v))
    print("  %-28s %14s %14s" % (h, vals[0], vals[1]))
PY
  cat "$OUT/2-comparison.txt"
fi

# ───────────────────────────── 3. planning ─────────────────────────────
if [ -z "$ONLY" ] || [ "$ONLY" = planning ]; then
  {
    echo "== experiment 3: how many slices, and how much capacity"
    echo
    echo "-- demand observed by the orchestrator over the last ${D}s (GET /plan)"
    curl -s "$ORCH/plan?window=$D"
    echo
    echo "-- the Phase 2b device mix: 10 control, 20 video, 70 sensor (POST /plan)"
    curl -s -X POST "$ORCH/plan" -H 'Content-Type: application/json' \
      -d '{"demands":[{"class":"control","count":10},{"class":"video","count":20},{"class":"sensor","count":70}]}'
    echo
    echo "-- delay-tolerant demand only: 30 video, 10 file"
    curl -s -X POST "$ORCH/plan" -H 'Content-Type: application/json' \
      -d '{"demands":[{"class":"video","count":30},{"class":"file","count":10}]}'
  } > "$OUT/3-planning.txt" 2>&1
  python3 - "$OUT/3-planning.txt" <<'PY'
import json, re, sys
text = open(sys.argv[1]).read()
for title, body in re.findall(r"-- (.*?)\n(\{.*?\n\})", text, re.S):
    r = json.loads(body)
    print("%s\n  slices needed: %d" % (title, r["slices_needed"]))
    for s in r["slices"]:
        if s["needed"]:
            print("    %-6s demand %7.2f Mbps -> needs %7.2f (limit now %6.1f, fits now: %s)  %s"
                  % (s["slice"], s["demand_mbps"], s["required_mbps"], s["current_limit_mbps"],
                     s["fits_now"], s["classes"]))
PY
fi
# ───────────────────────────── 4. diagnosis ─────────────────────────────
diag_run() {   # <label> <mix|idle>
  restart FLOW_SECONDS=20 CONTROL=0
  curl -s "$URLLC_KPI/run/start" >/dev/null
  local l0; l0=$(load1)
  if [ "$2" = idle ]; then
    sleep "$DD"
  else
    ( cd tools/slice-orchestrator && CORE=open5gs DB_CONTAINER=o5gs-mongodb ORCH_URL="$ORCH" \
        DURATION="$DD" RATE="$RATE" MIX="$2" python3 traffic_gen.py ) > "$OUT/4-diag-$1.txt" 2>&1
  fi
  curl -s "$URLLC_KPI/run/stats" | python3 -c "import json,sys
k = json.load(sys.stdin); o = k['owd_ms']
print('  %-11s %-20s URLLC probe mean %5.2f  p95 %6.2f  p99 %6.2f ms  loss %.4f' % ('$1', '$2', o['mean'], o['p95'], o['p99'], k['loss_ratio']), end='')"
  echo "   host load $l0 -> $(load1)"
  sleep 30
  echo "              leaked flow processes after: $(leaks)"
}

if [ -z "$ONLY" ] || [ "$ONLY" = diagnosis ]; then
  DD="${DD:-120}"
  {
    echo "== experiment 4: what inflates URLLC's tail? ${DD}s each, ${RATE} demands/s, controller off"
    diag_run idle idle
    diag_run urllc-only "control:1 blog:1"
    diag_run embb-only "video:1 file:1"
  } 2>&1 | tee "$OUT/4-diagnosis.txt"
fi

echo
echo "evidence: $OUT"
