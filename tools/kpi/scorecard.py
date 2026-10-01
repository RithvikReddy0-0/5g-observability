#!/usr/bin/env python3
"""
scorecard.py — score measured KPIs against deployments/open5gs/kpi-scorecard.json (Phase 2b, Track 3).

    tools/kpi/scorecard.py --check                 validate the definitions (CI runs this)
    tools/kpi/scorecard.py --self-test             prove the scoring rules on known values
    tools/kpi/scorecard.py --instance run.json     score one measurement instance and print it

The definitions file is the only place a KPI, its target or its weight lives. Scoring rules are
stated there and implemented here; see score_kpi(). Stdlib only, like the KPI gate.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_DEFS = os.path.join(REPO, "deployments", "open5gs", "kpi-scorecard.json")
REQUIRED = ("id", "name", "measure", "unit", "better", "weight", "origin", "why")


def load(path=DEFAULT_DEFS):
    d = json.load(open(path, encoding="utf-8"))
    problems = []
    ids = set()
    for s in d["slices"]:
        wsum = 0.0
        for k in s["kpis"]:
            for f in REQUIRED:
                if f not in k:
                    problems.append("%s: missing %r" % (k.get("id", "?"), f))
            if k.get("id") in ids:
                problems.append("duplicate id %s" % k.get("id"))
            ids.add(k.get("id"))
            if k.get("better") not in ("lower", "higher"):
                problems.append("%s: better must be lower or higher" % k.get("id"))
            if k.get("origin") not in ("notes", "added"):
                problems.append("%s: origin must be notes or added" % k.get("id"))
            w = float(k.get("weight", 0))
            if w < 0:
                problems.append("%s: negative weight" % k["id"])
            if w > 0 and target_of(k) is None:
                problems.append("%s: scored (weight %g) but has no target" % (k["id"], w))
            wsum += w
        if abs(wsum - 1.0) > 1e-6:
            problems.append("slice %s: weights sum to %g, must be 1" % (s["name"], wsum))
    if problems:
        raise ValueError("invalid scorecard definitions:\n  " + "\n  ".join(problems))
    return d


def target_of(k):
    t = k.get("project_target")
    return t if t is not None else k.get("research_target")


def score_kpi(k, measured):
    """-> (score 0..1 or None, met True/False/None) for one KPI and its measured value."""
    t = target_of(k)
    if measured is None or t is None:
        return None, None
    if k["better"] == "lower":
        met = measured <= t
        if met:
            return 1.0, True
        return (t / measured if measured > 0 else 0.0), False
    met = measured >= t
    return (min(1.0, measured / t) if t > 0 else 1.0), met


def score(defs, measured):
    """measured: {"urllc.owd_mean_ms": 1.9, ...} -> scorecard rows and slice/overall scores."""
    rows, slices = [], []
    for s in defs["slices"]:
        total, have_all = 0.0, True
        for k in s["kpis"]:
            m = measured.get(k["measure"])
            sc, met = score_kpi(k, m)
            w = float(k["weight"])
            if w > 0:
                if sc is None:
                    have_all = False
                else:
                    total += w * sc
            b = (k.get("benchmark") or {}).get("value")
            rows.append({
                "slice": s["name"], "id": k["id"], "name": k["name"], "unit": k["unit"],
                "better": k["better"], "measured": m,
                "project_target": k.get("project_target"), "research_target": k.get("research_target"),
                "target_used": target_of(k), "met": met, "score": None if sc is None else round(sc, 4),
                "weight": w, "benchmark": b, "benchmark_source": (k.get("benchmark") or {}).get("source"),
                "vs_benchmark": ratio(m, b, k["better"]), "origin": k["origin"],
            })
        slices.append({"slice": s["name"], "score": round(total, 4) if have_all else None,
                       "complete": have_all})
    scored = [x["score"] for x in slices if x["score"] is not None]
    overall = round(sum(scored) / len(scored), 4) if len(scored) == len(slices) and scored else None
    return {"rows": rows, "slices": slices, "overall": overall}


def ratio(m, b, better):
    """How far the measurement is from the industry benchmark, as a fraction of it (1 = equal)."""
    if m is None or b in (None, 0):
        return None
    if better == "lower":
        return round(b / m, 4) if m > 0 else None
    return round(m / b, 4)


def fmt(v, unit=""):
    if v is None:
        return "—"
    if unit == "ratio":
        return "%.4f %%" % (100 * v)
    if isinstance(v, float):
        return "%.3g" % v if abs(v) < 1000 else "%.0f" % v
    return str(v)


def print_card(card):
    for s in card["slices"]:
        print("\n%s  slice score %s" % (s["slice"], "—" if s["score"] is None else "%.2f" % s["score"]))
        for r in [r for r in card["rows"] if r["slice"] == s["slice"]]:
            status = {True: "MET ", False: "MISS", None: "    "}[r["met"]]
            print("  [%s] %-58s %12s  target %-10s w %.2f  score %s"
                  % (status, r["name"][:58], fmt(r["measured"], r["unit"]) + ("" if r["unit"] == "ratio" else " " + r["unit"]),
                     fmt(r["target_used"], r["unit"]), r["weight"], "—" if r["score"] is None else "%.2f" % r["score"]))
    print("\noverall score (mean of slices): %s" % ("—" if card["overall"] is None else "%.2f" % card["overall"]))


def self_test(defs):
    k_low = {"better": "lower", "project_target": 10}
    k_high = {"better": "higher", "project_target": 50}
    cases = [(k_low, 5, (1.0, True)), (k_low, 20, (0.5, False)), (k_high, 25, (0.5, False)),
             (k_high, 100, (1.0, True)), (k_low, None, (None, None)), ({"better": "higher"}, 3, (None, None))]
    for k, m, want in cases:
        got = score_kpi(k, m)
        if got != want:
            print("self-test FAILED: %s measured %s -> %s, want %s" % (k, m, got, want))
            return 1
    perfect = {}
    for s in defs["slices"]:
        for k in s["kpis"]:
            t = target_of(k)
            if t is not None:
                perfect[k["measure"]] = t
    card = score(defs, perfect)
    if card["overall"] != 1.0:
        print("self-test FAILED: measurements exactly at target must score 1.0, got %s" % card["overall"])
        return 1
    print("self-test passed — at-target scores 1.0, misses score target/measured, unknown is not scored")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--defs", default=DEFAULT_DEFS)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--instance", help="instance JSON written by tools/kpi/run_kpis.py")
    a = ap.parse_args()
    try:
        defs = load(a.defs)
    except (ValueError, KeyError) as e:
        print(e)
        return 1
    n = sum(len(s["kpis"]) for s in defs["slices"])
    scored = sum(1 for s in defs["slices"] for k in s["kpis"] if float(k["weight"]) > 0)
    if a.check:
        print("%d KPIs across %d slices, %d scored; definitions valid" % (n, len(defs["slices"]), scored))
        return 0
    if a.self_test:
        return self_test(defs)
    if a.instance:
        inst = json.load(open(a.instance, encoding="utf-8"))
        print_card(score(defs, inst["measured"]))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
