#!/usr/bin/env python3
"""
compare_overload.py — one table over several experiment.py part-D runs (ADR-021): every
measurement in the order it ran, then per mode the radio-link failures, loss and latency
(median, min-max), and permutation tests of each mode against a reference mode.

    python3 tools/packet-scheduler/compare_overload.py <run dir> [<run dir> ...] [--ref sliced]
"""

import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "kpi"))
from compare_arms import permutation  # noqa: E402

PROBES = [("URLLC critical", "URLLC crit"), ("URLLC standard", "URLLC std"), ("mMTC", "mMTC"), ("eMBB unloaded", "eMBB idle dev")]


def fmt(v):
    return "—" if v is None else ("%.1f" % v if v < 1000 else "%.0f" % v)


def main(argv):
    ref = "sliced"
    if "--ref" in argv:
        ref = argv[argv.index("--ref") + 1]
        argv = [a for a in argv if a not in ("--ref", ref)]
    runs = []
    for d in argv:
        res = json.load(open(os.path.join(d, "results.json")))
        for r in res["D"]:
            runs.append(dict(r, file=os.path.basename(os.path.normpath(d))))
    print("| Experiment | Run | Re-attached before | RLF | UEs w/o data path after | URLLC crit p50 / p99 / loss | URLLC std p50 / p99 / loss | mMTC p50 / p99 / loss | eMBB idle device p99 / loss | eMBB delivered (Mbps) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        p = r["probes"]
        cells = []
        for name, _ in PROBES[:3]:
            x = p[name]
            cells.append("%s / %s / %s %%" % (fmt(x["p50"]), fmt(x["p99"]), x["loss_percent"]))
        e = p["eMBB unloaded"]
        print("| `%s` | %s | %s | %d | %d | %s | %s / %s %% | %s |" % (
            r["file"], r["label"], "yes" if r["restored_before"] else "no", r["radio_link_failures"],
            r["ues_without_data_path_after"], " | ".join(cells), fmt(e["p99"]), e["loss_percent"],
            r["load"].get("eMBB", {}).get("delivered_mbps")))
    modes = list(dict.fromkeys(r["mode"] for r in runs))
    print()
    print("| Mode | runs | RLF per run | URLLC crit p50 (ms) | URLLC crit p99 (ms) | URLLC loss (crit, std) | mMTC p99 (ms) | mMTC loss | eMBB delivered (Mbps) |")
    print("|---|---|---|---|---|---|---|---|---|")

    def med(vals, f="%.1f"):
        vals = [v for v in vals if v is not None]
        return "—" if not vals else "%s (%s–%s)" % (f % statistics.median(vals), f % min(vals), f % max(vals))

    for m in modes:
        sub = [r for r in runs if r["mode"] == m]
        print("| %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
            m, len(sub), ", ".join(str(r["radio_link_failures"]) for r in sub),
            med([r["probes"]["URLLC critical"]["p50"] for r in sub]), med([r["probes"]["URLLC critical"]["p99"] for r in sub]),
            ", ".join("%g/%g %%" % (r["probes"]["URLLC critical"]["loss_percent"], r["probes"]["URLLC standard"]["loss_percent"]) for r in sub),
            med([r["probes"]["mMTC"]["p99"] for r in sub]), ", ".join("%g %%" % r["probes"]["mMTC"]["loss_percent"] for r in sub),
            med([r["load"].get("eMBB", {}).get("delivered_mbps") for r in sub])))
    print()
    print("Permutation tests against %s (difference of medians, two-sided):" % ref)
    print()
    print("| Mode − %s | URLLC crit p50: diff, p | URLLC crit p99: diff, p | mMTC p99: diff, p |" % ref)
    print("|---|---|---|---|")
    base = [r for r in runs if r["mode"] == ref]
    for m in modes:
        sub = [r for r in runs if r["mode"] == m]
        if m == ref or len(sub) < 2:
            continue
        cells = []
        for probe, stat in (("URLLC critical", "p50"), ("URLLC critical", "p99"), ("mMTC", "p99")):
            a = [r["probes"][probe][stat] for r in sub]
            b = [r["probes"][probe][stat] for r in base]
            if None in a or None in b:
                cells.append("— (no replies)")
                continue
            d, p = permutation(a, b)
            cells.append("%+.1f ms, p = %.2f" % (d, p))
        print("| %s (%d vs %d) | %s |" % (m, len(sub), len(base), " | ".join(cells)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
