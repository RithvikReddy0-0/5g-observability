#!/usr/bin/env bash
# scheduler_experiment.sh — the packet scheduler measured on the live stack (ADR-019).
# Isolation (off vs FIFO cell vs sliced), ARP tiers inside URLLC, and the guaranteed-share sweep.
# See tools/packet-scheduler/experiment.py. ~25 min (QUICK=1: ~8 min). Evidence in
# docs/evidence/open5gs-scheduler/<timestamp>/.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2
bash scripts/open5gs/start_scheduler.sh >/dev/null || exit 1
# a running slice orchestrator would add its own flows; the measurement must be the only load
pgrep -f "[o]rchestrator.py" >/dev/null && { echo "stop the slice orchestrator first (scripts/open5gs/start_orchestrator.sh --stop)"; exit 1; }
python3 tools/packet-scheduler/experiment.py
