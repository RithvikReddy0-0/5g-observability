#!/usr/bin/env python3
"""
check_metric_names.py — every repo-exported metric a KPI gate queries must really be exported.

WHY THIS EXISTS
Three free5GC gates queried `slice_ues_observed_registered` and `slice_provisioned_subscribers`
while the exporter emits `free5gc_slice_…`. They never matched a single series. CI did not
notice, because the stub Prometheus that proves the gate can fail was keyed by the SAME wrong
names — a stub built from the gate's own expressions can only confirm the gate agrees with
itself. Found only when the gate first ran against a live deployment (2026-09-14).

This closes that gap without a running stack: for every metric name in a gate expression
that belongs to one of THIS repository's exporters (identified by prefix), the name must
appear as a literal in that exporter's source. Metrics exported natively by free5GC or
Open5GS are out of scope — their names were verified against live /metrics output.

    tools/kpi-gate/check_metric_names.py [defs.json ...]
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# prefix -> source files that export metrics with that prefix
EXPORTERS = {
    "free5gc_slice_": ["observability/slice-exporter/slice_exporter.py"],
    "slice_": ["tools/slice-orchestrator/orchestrator.py"],
    "upf_slice_": ["deployments/open5gs/observability/upf_slice_exporter.py"],
    "ue_probe_": ["deployments/open5gs/observability/ue_probe_exporter.py"],
}
DEFAULT_DEFS = ["deployments/kpi-gates.json", "deployments/open5gs/kpi-gates.json"]

# PromQL identifiers; functions and keywords are filtered out by the prefix match below.
IDENT = re.compile(r"(?<![A-Za-z0-9_:\"])([A-Za-z_][A-Za-z0-9_]*)(?=\s*[{\[)\s=<>!]|$)")


def owner(name):
    # Longest matching prefix wins: "free5gc_slice_x" belongs to the slice exporter, not "slice_".
    hits = [p for p in EXPORTERS if name.startswith(p)]
    return max(hits, key=len) if hits else None


def main(paths):
    sources = {p: "\n".join(open(os.path.join(REPO, f), encoding="utf-8").read() for f in files)
               for p, files in EXPORTERS.items()}
    bad = checked = 0
    for defs in paths:
        doc = json.load(open(os.path.join(REPO, defs) if not os.path.isabs(defs) else defs,
                             encoding="utf-8"))
        for g in doc["gates"]:
            if g.get("requires") == "ode":
                continue                    # declares a metric that cannot exist yet
            for name in sorted(set(IDENT.findall(g["expr"]))):
                prefix = owner(name)
                if prefix is None:
                    continue
                checked += 1
                if not re.search(r"[\"']%s[\"'{]" % re.escape(name), sources[prefix]):
                    bad += 1
                    print("::error file=%s::gate %r queries %r, which %s never exports"
                          % (defs, g["id"], name, " / ".join(EXPORTERS[prefix])))
    print("%d repo-exported metric reference(s) checked, %d not exported" % (checked, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or DEFAULT_DEFS))
