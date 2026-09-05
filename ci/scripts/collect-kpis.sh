#!/usr/bin/env bash
# collect-kpis.sh — snapshot the KPI verdict as pipeline evidence.
#
# This file was a 0-byte stub ported from the previous project (as were its prometheus.yml
# and alert-rules.yml). It now does the one job its name promises.
#
# It deliberately does NOT define or evaluate any KPI itself. It delegates to
# tools/kpi-gate/kpi_gate.py, which reads deployments/kpi-gates.json — so the numbers a
# pipeline reports can never drift from the numbers a pipeline enforces. A second copy of
# the thresholds here would be a bug waiting to happen.
#
# Usage:
#   ci/scripts/collect-kpis.sh [output-dir]
#
# Exit code is the gate's own: 0 pass, 1 a KPI failed, 2 the gate could not run.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT" || exit 2

OUT_DIR="${1:-docs/evidence/kpi}"
STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
mkdir -p "$OUT_DIR"

JSON="$OUT_DIR/kpi-$STAMP.json"
TXT="$OUT_DIR/kpi-$STAMP.txt"

echo "Collecting KPI verdict -> $JSON"

# --wait: right after a deploy, Prometheus needs a scrape interval or two before the series
# exist. Without this the gate would fail on timing rather than on the deployment.
NO_COLOR=1 python3 tools/kpi-gate/kpi_gate.py \
  --wait "${KPI_WAIT:-30}" \
  --json "$JSON" | tee "$TXT"

rc=${PIPESTATUS[0]}

echo
case $rc in
  0) echo "KPI gate PASSED — evidence in $JSON" ;;
  1) echo "KPI gate FAILED — this build must not be promoted. See $TXT" ;;
  *) echo "KPI gate could not run (exit $rc). The deployment is UNVERIFIED, not verified." ;;
esac

exit "$rc"
