#!/usr/bin/env bash
# capture_report_figures.sh — every figure for the report's System Implementation and System
# Testing chapters, from the running Open5GS stack and the committed evidence.
#
#   live      commands run now against the stack (containers, NG Setup, sessions, user plane,
#             KPI gate, lifecycle operations, tests); Grafana/Prometheus under live traffic
#   evidence  experiments already run and committed (KPI run, scale, deploy/rollback, orchestrator
#             experiments, algorithm comparison): the figure shows the evidence file itself
#
# Terminal figures: tools/report/term_shot.py (real output, a .txt sidecar beside each PNG).
# Output: docs/report/figures/       Usage: tools/report/capture_report_figures.sh [impl|test|web|sched|multi|all]

set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
F=docs/report/figures
mkdir -p "$F"
T="python3 tools/report/term_shot.py"
EV=docs/evidence
PART="${1:-all}"
IMAGE="${CHROME_IMAGE:-zenika/alpine-chrome:latest}"

web() {  # web <file> <width> <height> <url> [network]
  docker run --rm --network "${5:-o5gs_net}" -v "$PWD/$F:/out" --entrypoint chromium-browser "$IMAGE" \
    --headless --disable-gpu --no-sandbox --hide-scrollbars --window-size="$2,$3" \
    --virtual-time-budget=25000 --screenshot="/out/$1" "$4" >/dev/null 2>&1
  [ -s "$F/$1" ] && echo "  [OK] $1" || echo "  [FAIL] $1"
}

if [ "$PART" = all ] || [ "$PART" = impl ]; then
  echo "== implementation (live)"
  $T $F/impl-01-containers.png "docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' --filter name=o5gs- | sort" --width 900
  $T $F/impl-02-slice-consistency.png "python3 tools/check_open5gs_slices.py"
  $T $F/impl-03-provisioning.png "python3 scripts/open5gs/provision_subscribers.py" --width 950
  $T $F/impl-04-gnb-ng-setup.png 'for g in o5gs-gnb-embb o5gs-gnb-urllc o5gs-gnb-mmtc; do echo "== $g"; docker logs $g 2>&1 | grep -E "NG Setup procedure is successful|NG Setup done" | tail -2; done' --width 950 \
     --show 'for g in o5gs-gnb-embb o5gs-gnb-urllc o5gs-gnb-mmtc; do docker logs $g | grep "NG Setup"; done'
  $T $F/impl-05-ue-status.png 'docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh status; echo; echo "PDU sessions by pool and kind (PSI 1 = home, ADR-020):"; docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh sessions | awk "{split(\$4,a,\".\"); print a[1]\".\"a[2], \$5}" | sort | uniq -c' \
     --width 900 --show 'docker exec o5gs-ue sh /etc/ueransim/ue-slice.sh status; ip -4 -o addr show | grep uesimtun'
  $T $F/impl-06-pdu-session-per-slice.png 'for i in 001 021 031; do echo "== imsi-208930000000$i"; docker exec o5gs-ue nr-cli imsi-208930000000$i -e ps-list | grep -E "state|apn|sst|sd:|address|ambr"; done' \
     --width 900 --show 'for i in 001 021 031; do docker exec o5gs-ue nr-cli imsi-208930000000$i -e ps-list; done'
  $T $F/impl-07-kpi-collectors.png 'for c in 10.53.0.52 10.53.0.53; do curl -s http://$c:9130/metrics | grep -E "^kpi_(owd_ms|packets_received_total|packets_expected_total|devices_active)"; done' --width 950 \
     --show 'curl -s http://10.53.0.52:9130/metrics; curl -s http://10.53.0.53:9130/metrics   # URLLC and mMTC KPI collectors'
  $T $F/impl-08-orchestrators.png 'python3 tools/network-orchestrator/netorch.py status; echo; curl -s localhost:9111/state | python3 -c "import json,sys; s=json.load(sys.stdin); print(\"slice orchestrator:\", s[\"core\"], \"| executing flows:\", s[\"executing\"]); [print(\"  %-6s active=%-5s capacity %5.0f Mbps  admission limit %6.1f Mbps  5QI %d\" % (x[\"name\"], x[\"active\"], x[\"capacity_mbps\"], x[\"admission_limit_mbps\"], x[\"five_qi\"])) for x in s[\"slices\"]]; c=s[\"control\"]; print(\"  controller: policy pi, protects\", c[\"protect\"], c[\"stat\"], \"- last reading\", c[\"latency_ms\"], \"ms\")"' \
     --show 'python3 tools/network-orchestrator/netorch.py status; curl -s localhost:9111/state'
  $T $F/impl-09-plan.png "curl -s -X POST localhost:9111/plan -H 'Content-Type: application/json' -d '{\"demands\":[{\"class\":\"control\",\"count\":10},{\"class\":\"video\",\"count\":20},{\"class\":\"sensor\",\"count\":70}]}' | python3 -c 'import json,sys; r=json.load(sys.stdin); print(\"slices needed:\", r[\"slices_needed\"]); [print(\"  %-6s demand %7.2f Mbps  required (Kaufman-Roberts, 1%% blocking) %7.2f Mbps  (old mean x 1.25: %7.2f)\" % (s[\"slice\"], s[\"demand_mbps\"], s[\"required_mbps\"], s[\"mean_headroom_mbps\"])) for s in r[\"slices\"] if s[\"needed\"]]'" \
     --show "curl -X POST localhost:9111/plan -d '{\"demands\":[{\"class\":\"control\",\"count\":10},{\"class\":\"video\",\"count\":20},{\"class\":\"sensor\",\"count\":70}]}'"
fi

if [ "$PART" = all ] || [ "$PART" = web ]; then
  echo "== Grafana and Prometheus under live traffic (150 s, both slices)"
  $T $F/test-04-traffic.png "bash scripts/open5gs/traffic.sh 150 3 down both" --max-lines 30 &
  TRAFFIC=$!
  sleep 70
  G="http://10.53.0.42:3000/d-solo/open5gs-slices/open5gs?orgId=1&from=now-15m&to=now&panelId"
  web impl-10-grafana-overview.png 1600 1150 "http://10.53.0.42:3000/d/open5gs-slices/open5gs?orgId=1&kiosk&from=now-15m&to=now"
  web impl-11-grafana-sessions-per-slice.png 1200 480 "$G=6"
  web impl-12-grafana-downlink-per-slice.png 1200 480 "$G=17"
  web impl-13-grafana-rtt-per-slice.png 1200 480 "$G=15"
  web impl-14-grafana-urllc-one-way-latency.png 1200 480 "$G=28"
  web impl-15-prometheus-sessions-by-slice.png 1400 620 "http://10.53.0.41:9090/query?g0.expr=fivegs_smffunction_sm_sessionnbr&g0.tab=table"
  wait "$TRAFFIC"
  web test-02-github-actions.png 1400 1000 "https://github.com/RithvikReddy0-0/5g-observability/actions" bridge
fi

if [ "$PART" = all ] || [ "$PART" = test ]; then
  echo "== testing"
  $T $F/test-01-unit-tests.png 'python3 tools/slice-orchestrator/test_decide.py 2>&1 | tail -3; python3 tools/network-orchestrator/test_netorch.py 2>&1 | tail -3; python3 tools/kpi/scorecard.py --self-test 2>&1 | tail -2; python3 tools/kpi-gate/kpi_gate.py --self-test --defs deployments/open5gs/kpi-gates.json 2>&1 | tail -2; python3 tools/packet-scheduler/test_scheduler.py 2>&1 | tail -3; python3 tools/check_open5gs_slices.py | tail -2' \
     --show 'python3 tools/slice-orchestrator/test_decide.py; python3 tools/network-orchestrator/test_netorch.py; python3 tools/kpi/scorecard.py --self-test; python3 tools/kpi-gate/kpi_gate.py --self-test; python3 tools/check_open5gs_slices.py'
  $T $F/test-03-user-plane.png "bash scripts/open5gs/verify_user_plane.sh" --max-lines 34
  # one line per KPI: the full output runs past what a figure can hold, and a cut once hid the failing line
  $T $F/test-05-kpi-gate.png 'NO_COLOR=1 python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json | grep -E "^ +\[|passed|GATE"' \
     --max-lines 40 --show 'python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json   # one line per KPI'
  KR=$(ls -d $EV/open5gs-kpi/2*/ | sort | tail -1)
  $T $F/test-06-kpi-scorecard.png "sed -n '1,20p' ${KR}scorecard.md" --max-lines 40 --width 1500
  # every run, shared vs dedicated (ADR-011's table): one run's tail once overstated the isolation
  $T $F/test-07-isolation.png 'sed -n "47,53p" docs/adr/ADR-011-dedicated-user-plane-per-slice.md' --width 1250      --show 'sed -n 47,53p docs/adr/ADR-011-dedicated-user-plane-per-slice.md   # isolation test, every run'
  $T $F/test-08-scale.png "sed -n '13,20p;/Part 2/,/^$/p' $EV/open5gs-scale/README.md" --max-lines 30
  $T $F/test-09-deploy-rollback.png "grep -h -E '^(== |DECISION|  \[  FAIL|  restored|  re-gated)' $EV/open5gs-deploy/deploy-20260914-0922*.txt" --max-lines 30
  $T $F/test-10-priority.png "sed -n '/critical demands/,\$p' $EV/open5gs-orchestrator-dynamic/1-priority.txt" --max-lines 30
  $T $F/test-11-controller-comparison.png "python3 -c \"
import csv
rows=list(csv.reader(open('$EV/open5gs-algorithm-comparison/controller/comparison.csv')))
h=rows[0]; keep=[0,2,3,4,7,8,9]
print('  '.join('%-24s'%h[i][:24] if j==0 else '%-14s'%h[i][:14] for j,i in enumerate(keep)))
for r in rows[1:]: print('  '.join('%-24s'%r[i] if j==0 else '%-14s'%r[i] for j,i in enumerate(keep)))
\"" --show "cat $EV/open5gs-algorithm-comparison/controller/comparison.csv   # every run, both rounds"
  $T $F/test-12-simulations.png "cat $EV/open5gs-algorithm-comparison/simulation/simulation.md $EV/open5gs-algorithm-comparison/simulation/autoscale.md" --max-lines 40
  $T $F/test-13-netorch-deactivate.png "python3 tools/network-orchestrator/netorch.py deactivate mMTC; python3 tools/network-orchestrator/netorch.py status"
  $T $F/test-14-netorch-activate.png "python3 tools/network-orchestrator/netorch.py activate mMTC; python3 tools/network-orchestrator/netorch.py status"
  $T $F/test-15-netorch-rollback.png "sed -n '1,12p' $EV/open5gs-network-orchestrator/2-rollback.txt"
  $T $F/test-16-closed-loop.png "sed -n '1,30p' $EV/open5gs-network-orchestrator/3-closed-loop.txt" --max-lines 32
  $T $F/test-17-acceptance.png "bash tests/acceptance-open5gs.sh" --max-lines 46
fi

# Packet scheduler (ADR-019): the live class tree and the kernel's own view of it, the tests, the
# experiment's tables, and Grafana over the experiment's own time window.
if [ "$PART" = all ] || [ "$PART" = sched ]; then
  echo "== packet scheduler"
  # the run carrying the combined A-D summary (experiment.py --merge), else the newest
  RUN=$(ls -d $EV/open5gs-scheduler/2*/ 2>/dev/null | sort | while read -r d; do [ -f "${d}results-merged.json" ] && echo "$d"; done | tail -1)
  [ -n "$RUN" ] || RUN=$(ls -d $EV/open5gs-scheduler/2*/ 2>/dev/null | sort | tail -1)
  DEV=$(curl -s localhost:9114/state | python3 -c "import json,sys; print(json.load(sys.stdin)['devices']['downlink'])")
  $T $F/impl-16-scheduler-classes.png "python3 tools/packet-scheduler/scheduler.py --show" --width 1150 --max-lines 30
  $T $F/impl-17-scheduler-tc.png "docker exec o5gs-scheduler tc filter show dev $DEV | grep -E 'flowid|match' | head -14; echo; docker exec o5gs-scheduler tc -s class show dev $DEV classid 1:31" \
     --width 1150 --max-lines 26 --show "tc filter show dev $DEV; tc -s class show dev $DEV classid 1:31   # the kernel's own view"
  $T $F/test-18-scheduler-unit-tests.png "python3 tools/packet-scheduler/test_scheduler.py -v 2>&1 | tail -28" --max-lines 30
  $T $F/test-19-scheduler-isolation.png "sed -n '/^## A/,/^## B/p' ${RUN}summary.md | grep -v '^## B'" --width 1600 --max-lines 30
  $T $F/test-20-scheduler-arp-tiers.png "sed -n '/^## B/,/^## C/p' ${RUN}summary.md | head -10" --width 1300
  $T $F/test-21-scheduler-shares.png "sed -n '/^## C/,\$p' ${RUN}summary.md" --width 1100
  # Grafana over the experiment's part A (isolation) window
  W=$(python3 -c "
import json,calendar,time
r=json.load(open('${RUN}results-merged.json'))
ts=[calendar.timegm(time.strptime(x['started'],'%Y-%m-%dT%H:%M:%SZ')) for x in r['A']]
print('from=%d&to=%d' % ((min(ts)-30)*1000, (max(ts)+r['seconds']+30)*1000))")
  G="http://10.53.0.42:3000/d-solo/open5gs-slices/open5gs?orgId=1&$W&panelId"
  web test-22-grafana-scheduler-downlink.png 1300 520 "$G=51"
  web test-23-grafana-scheduler-queues.png 1300 520 "$G=52"
  web test-24-grafana-scheduler-drops.png 1300 520 "$G=53"
fi

# Multi-session UEs (ADR-020): the latest multi_session_demo.sh run
if [ "$PART" = all ] || [ "$PART" = multi ]; then
  echo "== multi-session UEs"
  MS=$(ls -d $EV/open5gs-multi-session/2*/ 2>/dev/null | sort | tail -1)
  $T $F/impl-18-multi-session.png "cat ${MS}1-sessions.txt" --width 1000
  $T $F/test-25-multi-session-placement.png "cat ${MS}2-placement.txt" --width 1350
  $T $F/test-26-multi-session-lifecycle.png "cat ${MS}3-lifecycle.txt" --width 1350 --max-lines 50
fi
echo "figures: $F"
