#!/usr/bin/env python3
"""
simulate_autoscale.py — when to switch an on-demand slice on and off, compared over hours (ADR-018).

Drives the network orchestrator's REAL closed-loop decision (auto_tick, next_onset) with a simulated
clock. The slice takes ACTIVATION seconds to come up and DEACTIVATION to go down — the times measured
on the live stack (ADR-017) — and serves demand only while ACTIVE. Demand is periodic IoT reporting:
a burst of reports every PERIOD seconds (+- jitter), each burst lasting BURST seconds.

  always on      never switched off: no refusals, the slice runs all the time
  reactive       switch on when demand appears, off after IDLE_SECONDS without it (the original)
  predictive     reactive + learn the period between bursts, switch on ACTIVATE_LEAD s before the next

Measured: demands refused because the slice was not up, the share of time the slice is up (what it
costs), and how many switch operations it took. Usage: python3 simulate_autoscale.py [out dir]
"""

import importlib.util
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVATION, DEACTIVATION = 17.0, 14.5       # seconds, measured live (ADR-017)
TICK, WINDOW = 10.0, 30.0


def netorch(**env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("netorch_sim_%d" % random.getrandbits(30),
                                                      os.path.join(HERE, "netorch.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    mod.orch = lambda *a, **k: {}
    return mod


def demand_times(period, burst, hours, rate, jitter, seed):
    rng = random.Random(seed)
    out, t = [], 60.0
    while t < hours * 3600:
        start = t + rng.uniform(-jitter, jitter)
        k = start
        while k < start + burst:
            out.append(k)
            k += rng.expovariate(rate)
        t += period
    return out


def run(policy, period, burst, hours=6, rate=1.0, jitter=10.0, seed=3, idle=120.0):
    demands = demand_times(period, burst, hours, rate, jitter, seed)
    end = hours * 3600
    if policy == "always on":
        return {"refused": 0, "offered": len(demands), "up_share": 1.0, "switches": 0}
    o = netorch(AUTO_POLICY="predictive" if policy == "predictive" else "reactive", ON_DEMAND="mMTC",
                IDLE_SECONDS=str(idle), MIN_UP="30", PLAN_WINDOW=str(int(WINDOW)), ACTIVATE_LEAD="30")
    state, change_at, target, switches = "ACTIVE", None, None, 0

    def submit(kind, name, reason="", **a):
        nonlocal state, change_at, target, switches
        switches += 1
        state = "ACTIVATING" if kind == "activate" else "DEACTIVATING"
        change_at = now + (ACTIVATION if kind == "activate" else DEACTIVATION)
        target = "ACTIVE" if kind == "activate" else "DORMANT"
        return object(), None
    o.submit = submit

    refused = 0
    up_time = 0.0
    di = 0
    now = 0.0
    while now < end:
        if change_at is not None and now >= change_at:
            state, change_at = target, None
            o.last_change["mMTC"] = now
        # demands in this tick: served only while ACTIVE (refused ones are still demand the plan sees)
        recent = 0
        while di < len(demands) and demands[di] < now + TICK:
            if state != "ACTIVE":
                refused += 1
            di += 1
        lo = now + TICK - WINDOW
        recent = sum(1 for d in demands[max(0, di - 200):di] if d >= lo)
        if state == "ACTIVE":
            up_time += TICK
        plan = {"slices": [{"slice": "mMTC", "needed": recent > 0, "demand_mbps": 0.01 * recent}]}
        states = {"mMTC": {"state": state, "settled_state": state}}
        now += TICK
        o.auto_tick(now=now, plan=plan, states=states)
    return {"refused": refused, "offered": len(demands), "up_share": up_time / end, "switches": switches}


def main(out):
    os.makedirs(out, exist_ok=True)
    rows = []
    for period, burst in [(300, 60), (600, 60)]:
        for policy in ("always on", "reactive", "predictive"):
            r = run(policy, period, burst)
            r.update(policy=policy, period=period, burst=burst)
            rows.append(r)
    json.dump(rows, open(os.path.join(out, "autoscale.json"), "w"), indent=2)
    lines = ["## C. Switching mMTC on and off: periodic IoT bursts, 6 simulated hours, activation 17 s", "",
             "| Burst every | Policy | demands refused | slice up (share of time) | switch operations |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %d s (%d s long) | %s | %d of %d (%.1f %%) | %.0f %% | %d |" % (
            r["period"], r["burst"], r["policy"], r["refused"], r["offered"],
            100.0 * r["refused"] / r["offered"], 100 * r["up_share"], r["switches"]))
    text = "\n".join(lines) + "\n"
    open(os.path.join(out, "autoscale.md"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "..", "..", "docs", "evidence", "open5gs-algorithm-comparison", "simulation"))
