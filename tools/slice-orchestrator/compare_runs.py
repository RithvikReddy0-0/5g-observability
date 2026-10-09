#!/usr/bin/env python3
"""
compare_runs.py <evidence dir> <seconds per run> — one row per control law, from the files
controller_benchmark.sh saved (ADR-018). Writes comparison.md, comparison.csv, comparison.xlsx.

Columns, and why each is there:
  URLLC time over budget   share of 5 s samples where URLLC's p95 (last 10 s) exceeded its 10 ms
                           budget: what the controller exists to prevent
  URLLC p95 / p99 / mean   over the whole run, from every probe packet (the collector's run stats)
  URLLC flows met          the protected slice's own admitted flows that got their rate
  eMBB flows met           admitted eMBB flows that got their rate: admitting more than the host
                           can carry shows up here
  eMBB delivered           data actually delivered to eMBB devices, as an average rate over the run
  eMBB admitted / refused  demands
  limit mean / std, moves  where the law held eMBB's admission limit, and how restless it was
"""

import csv
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "kpi"))
BUDGET_MS = 10.0
FLOW_SECONDS = 20


def series(d, label, name):
    try:
        r = json.load(open(os.path.join(d, "series-%s-%s.json" % (label, name))))["data"]["result"]
        return [float(v) for _, v in r[0]["values"]] if r else []
    except Exception:
        return []


def run_row(d, label, policy, dur, extra):
    flows = json.load(open(os.path.join(d, "flows-%s.json" % label)))
    k = json.load(open(os.path.join(d, "urllc-kpi-%s.json" % label)))
    ctl = json.load(open(os.path.join(d, "control-%s.json" % label)))
    demand = open(os.path.join(d, "demand-%s.txt" % label)).read().splitlines()
    p95 = series(d, label, "kpi_owd_recent_ms")
    lim = series(d, label, "slice_admission_limit_mbps")
    met = lambda fs: 100.0 * sum(f["outcome"] == "met" for f in fs) / len(fs) if fs else float("nan")
    embb = [f for f in flows if f["slice"] == "eMBB" and f["outcome"] != "preempted"]
    urllc = [f for f in flows if f["slice"] == "URLLC" and f["outcome"] != "preempted"]
    owd = k.get("owd_ms", {})
    return {
        "policy": policy, "run": label,
        "urllc_time_over_budget_pct": 100.0 * sum(v > BUDGET_MS for v in p95) / len(p95) if p95 else float("nan"),
        "urllc_p95_ms": owd.get("p95"), "urllc_p99_ms": owd.get("p99"), "urllc_mean_ms": owd.get("mean"),
        "urllc_flows_met_pct": met(urllc),
        "embb_flows_met_pct": met(embb),
        "embb_delivered_mbps": sum(f["delivered_mbps"] for f in embb) * FLOW_SECONDS / dur,
        "embb_admitted": sum(l.startswith("  ADMIT") and " eMBB " in l for l in demand),
        "embb_refused": sum(l.startswith("  REFUSE") and "eMBB" in l for l in demand),
        "limit_mean_mbps": statistics.mean(lim) if lim else 500.0,
        "limit_std_mbps": statistics.pstdev(lim) if lim else 0.0,
        "limit_moves": len(ctl),
        "host_load": "%s -> %s" % (extra.get("load_before"), extra.get("load_after")),
        "leaked": extra.get("leaked"),
    }


COLS = [("policy", "Policy"), ("urllc_time_over_budget_pct", "URLLC time over 10 ms (%)"),
        ("urllc_p95_ms", "URLLC p95 (ms)"), ("urllc_p99_ms", "URLLC p99 (ms)"), ("urllc_mean_ms", "URLLC mean (ms)"),
        ("urllc_flows_met_pct", "URLLC flows met (%)"), ("embb_flows_met_pct", "eMBB flows met (%)"),
        ("embb_delivered_mbps", "eMBB delivered (Mbps)"), ("embb_admitted", "eMBB admitted"),
        ("embb_refused", "eMBB refused"), ("limit_mean_mbps", "Limit mean (Mbps)"),
        ("limit_std_mbps", "Limit std (Mbps)"), ("limit_moves", "Limit moves")]


def fmt(v):
    if isinstance(v, float):
        return "%.1f" % v if v == v else "-"
    return str(v)


def main(d, dur):
    rows = []
    for line in open(os.path.join(d, "runs.txt")):
        parts = line.split()
        if len(parts) < 4:
            continue
        extra = dict(x.split("=", 1) for x in parts[4:] if "=" in x)
        rows.append(run_row(d, parts[0], parts[1], dur, extra))
    # One line per policy: the mean over its rounds.
    by = {}
    for r in rows:
        by.setdefault(r["policy"], []).append(r)
    summary = []
    for p, rs in by.items():
        s = {"policy": p}
        for key, _ in COLS[1:]:
            vals = [r[key] for r in rs if isinstance(r[key], (int, float)) and r[key] == r[key]]
            s[key] = statistics.mean(vals) if vals else float("nan")
        s["rounds"] = len(rs)
        summary.append(s)
    with open(os.path.join(d, "comparison.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run"] + [h for _, h in COLS] + ["host load", "leaked"])
        for r in rows:
            w.writerow([r["run"]] + [fmt(r[k]) for k, _ in COLS] + [r["host_load"], r["leaked"]])
    md = ["| " + " | ".join(h for _, h in COLS) + " |", "|" + "---|" * len(COLS)]
    for s in summary:
        md.append("| " + " | ".join(fmt(s[k]) for k, _ in COLS) + " |")
    open(os.path.join(d, "comparison.md"), "w").write(
        "Control laws compared, %d s per run, mean over rounds\n\n" % dur + "\n".join(md) + "\n")
    try:
        import xlsx
        xlsx.write(os.path.join(d, "comparison.xlsx"), [
            xlsx.Sheet("summary", [[h for _, h in COLS]] + [[s[k] for k, _ in COLS] for s in summary]),
            xlsx.Sheet("every run", [["run"] + [h for _, h in COLS] + ["host load", "leaked"]]
                       + [[r["run"]] + [r[k] for k, _ in COLS] + [r["host_load"], r["leaked"]] for r in rows]),
        ])
    except Exception as e:
        print("(no Excel file: %s)" % e)
    print("\n".join(md))


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]))
