#!/usr/bin/env python3
"""
simulate.py — admission policies and capacity sizing compared over many demands (ADR-018).

It drives the orchestrator's REAL decision code (decide(), kaufman_roberts(), kr_size()) with a
simulated clock: Poisson arrivals per traffic class, each admitted demand holding its rate for a
fixed time, nothing executed. Admission is bookkeeping, so the network adds nothing to these two
questions — and a simulation can run tens of thousands of demands, where a live run sees a few
hundred. The latency controller is compared live instead (controller_benchmark.sh).

  A. admission     URLLC contested by control (critical) and blog demands: greedy, reservation,
                   pricing. Blocking per class, and value carried.
  B. sizing        eMBB with video and file demands: capacity from mean x 1.25 (ours) and from
                   Kaufman-Roberts for 1 % blocking; the blocking each actually gives, and how well
                   Kaufman-Roberts predicts it.

Usage: python3 tools/slice-orchestrator/simulate.py [out dir]      (deterministic: fixed seeds)
"""

import importlib.util
import json
import math
import os
import random
import sys
import time as _time

HERE = os.path.dirname(os.path.abspath(__file__))
HOLD = 20.0


class Clock:
    """Stands in for the time module inside the orchestrator: time() is the simulated clock."""
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(_time, name)


def orchestrator(**env):
    base = {"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "FLOW_TTL": str(int(HOLD)),
            "CONTROL": "0"}
    base.update(env)
    saved = {k: os.environ.get(k) for k in base}
    os.environ.update(base)
    try:
        spec = importlib.util.spec_from_file_location("orch_sim_%d" % random.getrandbits(30),
                                                      os.path.join(HERE, "orchestrator.py"))
        o = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(o)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    phones = ["imsi-2089300000%05d" % i for i in range(1, 31)]
    iot = ["imsi-2089300000%05d" % i for i in range(31, 101)]
    subs = {p: ["1/010203", "2/112233"] for p in phones}
    subs.update({i: ["3/334455"] for i in iot})
    o.subscribers = lambda: subs
    o.measured_mbps = lambda: {}
    o.time = Clock()
    o._phones = phones
    return o


def run(o, rates, seconds, seed, capacity=None):
    """Poisson arrivals per class for `seconds`. -> per-class stats and value carried."""
    rng = random.Random(seed)
    if capacity:
        for name, c in capacity.items():
            o.limits[name] = c
            next(s for s in o.SLICES if s["name"] == name)["capacity_mbps"] = c
    events = []
    for cls, lam in rates.items():
        t = rng.expovariate(lam)
        while t < seconds:
            events.append((t, cls))
            t += rng.expovariate(lam)
    events.sort()
    stats = {c: {"offered": 0, "admitted": 0} for c in rates}
    value = 0.0
    for t, cls in events:
        o.time.now = t
        r = o.decide(rng.choice(o._phones), cls)
        stats[cls]["offered"] += 1
        if r.get("admitted"):
            stats[cls]["admitted"] += 1
            value += o.CLASS_VALUE.get(cls, 1.0) * o.TRAFFIC_CLASSES[cls]["mbps"] * HOLD
    for c, st in stats.items():
        st["blocking"] = 1 - st["admitted"] / st["offered"] if st["offered"] else 0.0
    return stats, value


def admission(out):
    rates = {"control": 0.5, "blog": 0.6}            # x 20 s x 1 Mbps = 10 + 12 Erlangs on 20 Mbps
    rows = []
    for name, env in [("greedy (previous default)", {"ADMISSION_POLICY": "greedy"}),
                      ("trunk reservation, 4 Mbps", {"ADMISSION_POLICY": "reservation", "RESERVE": "URLLC=4",
                                                     "RESERVE_EXEMPT": "control"}),
                      ("threshold pricing", {"ADMISSION_POLICY": "pricing"})]:
        o = orchestrator(**env)
        stats, value = run(o, rates, 20000, seed=7)
        rows.append({"policy": name,
                     "control_blocking_pct": round(100 * stats["control"]["blocking"], 2),
                     "blog_blocking_pct": round(100 * stats["blog"]["blocking"], 2),
                     "value_carried": round(value / 1e3, 1),
                     "demands": sum(s["offered"] for s in stats.values())})
    return rows


def sizing(out):
    rates = {"video": 1.0, "file": 0.4}               # 20 E x 8 Mbps + 8 E x 15 Mbps = 280 Mbps mean
    o = orchestrator()
    mean = sum(rates[c] * HOLD * o.TRAFFIC_CLASSES[c]["mbps"] for c in rates)
    loads = [(rates[c] * HOLD, o.TRAFFIC_CLASSES[c]["mbps"]) for c in rates]
    kr_mbps, unit, kr_pred = o.kr_size(loads, target=0.01)
    rows = []
    for name, cap in [("mean x 1.25 (previous default)", 1.25 * mean), ("Kaufman-Roberts, 1 % target", kr_mbps)]:
        units = int(cap / unit)
        predicted = o.kaufman_roberts([(a, max(1, int(round(m / unit)))) for a, m in loads], units)
        oo = orchestrator()
        stats, _ = run(oo, rates, 20000, seed=11, capacity={"eMBB": cap})
        rows.append({"method": name, "capacity_mbps": round(cap, 1),
                     "video_blocking_pct": round(100 * stats["video"]["blocking"], 2),
                     "file_blocking_pct": round(100 * stats["file"]["blocking"], 2),
                     "kr_predicted_video_pct": round(100 * predicted[0], 2),
                     "kr_predicted_file_pct": round(100 * predicted[1], 2)})
    return {"mean_offered_mbps": mean, "rows": rows}


def main(out):
    os.makedirs(out, exist_ok=True)
    a = admission(out)
    b = sizing(out)
    json.dump({"admission": a, "sizing": b}, open(os.path.join(out, "simulation.json"), "w"), indent=2)
    lines = ["## A. Admission on a contested URLLC slice (20 Mbps; control 10 E + blog 12 E offered; 20 000 s)", "",
             "| Policy | control blocked | blog blocked | value carried (k units) |", "|---|---|---|---|"]
    for r in a:
        lines.append("| %s | %.2f %% | %.2f %% | %.1f |" % (r["policy"], r["control_blocking_pct"],
                                                          r["blog_blocking_pct"], r["value_carried"]))
    lines += ["", "## B. Sizing eMBB (video 20 E x 8 Mbps + file 8 E x 15 Mbps = %.0f Mbps mean; 20 000 s)"
              % b["mean_offered_mbps"], "",
              "| Method | capacity | video blocked (simulated / KR predicted) | file blocked (simulated / KR predicted) |",
              "|---|---|---|---|"]
    for r in b["rows"]:
        lines.append("| %s | %.0f Mbps | %.2f %% / %.2f %% | %.2f %% / %.2f %% |" % (
            r["method"], r["capacity_mbps"], r["video_blocking_pct"], r["kr_predicted_video_pct"],
            r["file_blocking_pct"], r["kr_predicted_file_pct"]))
    text = "\n".join(lines) + "\n"
    open(os.path.join(out, "simulation.md"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "..", "..", "docs", "evidence", "open5gs-algorithm-comparison", "simulation"))
