#!/usr/bin/env python3
"""
kpi_gate.py — KPI validation as a pass/fail gate for the deployment pipeline.

This is the piece that turns observability into enforcement. Everything before it *showed*
whether a deployment worked; this decides, and a failing decision is meant to stop a pipeline.

    make gate                                  # run the gate against the live stack
    tools/kpi-gate/kpi_gate.py --self-test     # prove the gate can fail (no stack needed)

The thresholds live in deployments/kpi-gates.json, not here, so CI and the reports read the
same numbers. See docs/kpi-gate.md.

Exit codes
    0   every runnable gate passed
    1   at least one gate failed (the pipeline should stop)
    2   the gate could not run at all (Prometheus unreachable, bad definitions)

Deliberate design decisions, because they are the difference between a gate and a placebo:

  * NODATA is a FAILURE for gate-severity KPIs. A KPI that cannot be measured has not been
    met. Silently passing on an empty query is how a gate becomes decorative.
  * Gates requiring the ODE report SKIP-ODE and do NOT pass. Same vocabulary as
    tests/acceptance.sh — a green run on a non-baseline host does not mean the gate was met.
  * Only the standard library is used. The gate must run on a bare runner and on the host
    without a pip install standing between a deployment and its verdict.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DEFS = os.path.join(REPO_ROOT, "deployments", "kpi-gates.json")

# Comparison operators available to a gate definition. Kept explicit rather than eval'd.
OPS = {
    "lt":  (lambda v, t: v < t,  "<"),
    "lte": (lambda v, t: v <= t, "<="),
    "gt":  (lambda v, t: v > t,  ">"),
    "gte": (lambda v, t: v >= t, ">="),
    "eq":  (lambda v, t: v == t, "=="),
    "ne":  (lambda v, t: v != t, "!="),
}

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP-ODE", "WARN"

C = {
    PASS: "\033[32m", FAIL: "\033[31m", SKIP: "\033[36m", WARN: "\033[33m",
    "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m",
}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


# --------------------------------------------------------------------------- Prometheus

def promql(base, expr, timeout=10):
    """Run an instant query. Returns the result vector, or raises."""
    url = base.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        doc = json.loads(r.read().decode())
    if doc.get("status") != "success":
        raise RuntimeError(doc.get("error", "query rejected by Prometheus"))
    return doc["data"]["result"]


def to_scalar(result):
    """
    Collapse a result vector to one number.

    A gate expression must resolve to a single value. If it returns several series the
    definition is ambiguous, and guessing which series to test would make the verdict
    depend on scrape order — so it is reported as an error instead.
    """
    if not result:
        return None, "no data — the metric is not being reported"
    if len(result) > 1:
        return None, "expression returned %d series; aggregate it (sum/max/count)" % len(result)
    raw = result[0]["value"][1]
    if raw in ("NaN", "+Inf", "-Inf"):
        return None, "expression evaluated to %s" % raw
    return float(raw), None


# --------------------------------------------------------------------------- evaluation

def fmt(v):
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    if isinstance(v, float):
        return "%.4g" % v
    return str(v)


def evaluate(gate, value, err):
    """Decide one gate. Pure: no I/O, so --self-test can drive it directly."""
    if gate.get("requires") == "ode":
        return SKIP, "requires the user plane (gtp5g); not runnable on this host"

    op_fn, op_sym = OPS[gate["op"]]
    threshold = gate["threshold"]

    if value is None:
        # A KPI that cannot be measured has not been met.
        status = WARN if gate.get("severity") == "advisory" else FAIL
        return status, err or "no value"

    unit = gate.get("unit", "")
    if op_fn(value, threshold):
        return PASS, ("%s %s %s %s" % (fmt(value), op_sym, fmt(threshold), unit)).strip()

    status = WARN if gate.get("severity") == "advisory" else FAIL
    return status, ("%s %s — required %s %s"
                    % (fmt(value), unit, op_sym, fmt(threshold))).replace("  ", " ").strip()


def load_defs(path):
    with open(path) as f:
        doc = json.load(f)
    if doc.get("schema_version") != "1":
        raise SystemExit("%s: unexpected schema_version %r" % (path, doc.get("schema_version")))
    for g in doc["gates"]:
        for field in ("id", "title", "expr", "op", "threshold", "severity", "why"):
            if field not in g:
                raise SystemExit("%s: gate %r is missing %r" % (path, g.get("id", "?"), field))
        if g["op"] not in OPS:
            raise SystemExit("%s: gate %r has unknown op %r" % (path, g["id"], g["op"]))
        if g["severity"] not in ("gate", "advisory"):
            raise SystemExit("%s: gate %r has unknown severity %r"
                             % (path, g["id"], g["severity"]))
    ids = [g["id"] for g in doc["gates"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit("%s: duplicate gate ids %s" % (path, dupes))
    return doc


# --------------------------------------------------------------------------- self-test

# (description, gate, value, err, expected status)
SELF_TEST_CASES = [
    ("gte satisfied",
     {"op": "gte", "threshold": 8, "severity": "gate"}, 8.0, None, PASS),
    ("gte breached",
     {"op": "gte", "threshold": 8, "severity": "gate"}, 7.0, None, FAIL),
    ("lte satisfied",
     {"op": "lte", "threshold": 1.0, "severity": "gate"}, 0.4, None, PASS),
    ("lte breached",
     {"op": "lte", "threshold": 1.0, "severity": "gate"}, 2.5, None, FAIL),
    ("eq satisfied",
     {"op": "eq", "threshold": 0, "severity": "gate"}, 0.0, None, PASS),
    ("eq breached",
     {"op": "eq", "threshold": 0, "severity": "gate"}, 3.0, None, FAIL),
    ("missing data fails a gate",
     {"op": "gte", "threshold": 1, "severity": "gate"}, None, "no data", FAIL),
    ("missing data only warns for advisory",
     {"op": "gte", "threshold": 1, "severity": "advisory"}, None, "no data", WARN),
    ("advisory breach warns, never fails",
     {"op": "lte", "threshold": 0.95, "severity": "advisory"}, 0.99, None, WARN),
    ("ODE gate is skipped, not passed",
     {"op": "gte", "threshold": 0.99, "severity": "gate", "requires": "ode"}, 0.5, None, SKIP),
]


def self_test(defs_path):
    """
    Prove the gate can actually fail.

    A gate nobody has seen reject anything is indistinguishable from a gate that always
    passes. This drives the decision logic over known inputs, and validates the real
    definitions file — so CI exercises the gate on every push without needing a 5G core.
    """
    print("%skpi_gate self-test%s" % (C["bold"], C["off"]))
    print("  verifying the decision logic, including that breaches are rejected\n")

    bad = 0
    for desc, gate, value, err, expected in SELF_TEST_CASES:
        got, detail = evaluate(gate, value, err)
        ok = got == expected
        bad += 0 if ok else 1
        mark = "%sok%s" % (C[PASS], C["off"]) if ok else "%sFAILED%s" % (C[FAIL], C["off"])
        print("  [%s] %-45s -> %s" % (mark, desc, got))
        if not ok:
            print("          expected %s, got %s (%s)" % (expected, got, detail))

    print("\n%sdefinitions%s" % (C["bold"], C["off"]))
    doc = load_defs(defs_path)
    enforcing = [g for g in doc["gates"] if g["severity"] == "gate"]
    ode = [g for g in doc["gates"] if g.get("requires") == "ode"]
    print("  %d KPIs defined and structurally valid (%d enforcing, %d ODE-only)"
          % (len(doc["gates"]), len(enforcing), len(ode)))
    for g in doc["gates"]:
        if len(g["why"]) < 40:
            print("  %swarn%s  gate %r has a thin rationale" % (C[WARN], C["off"], g["id"]))

    print()
    if bad:
        print("%sself-test FAILED: %d case(s) wrong%s" % (C[FAIL], bad, C["off"]))
        return 1
    print("%sself-test passed%s — the gate rejects breaches and accepts conforming values"
          % (C[PASS], C["off"]))
    return 0


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="KPI validation gate for the 5G deployment.")
    ap.add_argument("--defs", default=DEFAULT_DEFS, help="KPI definitions (JSON)")
    ap.add_argument("--prometheus", default=None, help="Prometheus base URL")
    ap.add_argument("--json", dest="json_out", help="write the verdict to this file")
    ap.add_argument("--wait", type=int, default=0,
                    help="seconds to wait for Prometheus to become reachable")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the decision logic and the definitions; no stack needed")
    args = ap.parse_args()

    if args.self_test:
        return self_test(args.defs)

    doc = load_defs(args.defs)
    base = args.prometheus or os.environ.get("PROMETHEUS_URL") or doc["prometheus"]

    deadline = time.time() + args.wait
    while True:
        try:
            promql(base, "up", timeout=5)
            break
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            if time.time() >= deadline:
                sys.stderr.write("%sPrometheus unreachable at %s: %s%s\n"
                                 % (C[FAIL], base, e, C["off"]))
                sys.stderr.write("  the gate cannot run, so the deployment is NOT verified.\n")
                sys.stderr.write("  start the stack with 'make up', or set PROMETHEUS_URL.\n")
                return 2
            time.sleep(3)

    print("=" * 78)
    print("KPI gate — %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    print("Prometheus: %s   definitions: %s"
          % (base, os.path.relpath(args.defs, REPO_ROOT)))
    print("=" * 78)

    results = []
    counts = {PASS: 0, FAIL: 0, SKIP: 0, WARN: 0}

    for gate in doc["gates"]:
        if gate.get("requires") == "ode":
            value, err = None, None
        else:
            try:
                value, err = to_scalar(promql(base, gate["expr"]))
            except Exception as e:                    # query rejected, or network dropped
                value, err = None, "query failed: %s" % e

        status, detail = evaluate(gate, value, err)
        counts[status] += 1
        results.append({
            "id": gate["id"], "title": gate["title"], "status": status,
            "value": value, "threshold": gate["threshold"], "op": gate["op"],
            "unit": gate.get("unit", ""), "severity": gate["severity"], "detail": detail,
        })

        print("\n  [%s%s%s] %s%s%s"
              % (C[status], status.center(8), C["off"], C["bold"], gate["title"], C["off"]))
        print("             %s" % detail)
        if status in (FAIL, WARN):
            print("             %swhy this matters: %s%s" % (C["dim"], gate["why"], C["off"]))

    print("\n" + "=" * 78)
    print("  %d passed   %d failed   %d advisory   %d skipped (ODE)"
          % (counts[PASS], counts[FAIL], counts[WARN], counts[SKIP]))
    print("=" * 78)

    if counts[FAIL]:
        print("\n%sGATE FAILED — this deployment must not be promoted.%s"
              % (C[FAIL], C["off"]))
    else:
        tail = ""
        if counts[SKIP]:
            tail = (" %s(%d KPIs still require the ODE — Phase 1 is not frozen)%s"
                    % (C["dim"], counts[SKIP], C["off"]))
        print("\n%sGATE PASSED%s%s" % (C[PASS], C["off"], tail))

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "prometheus": base,
                "verdict": "FAILED" if counts[FAIL] else "PASSED",
                "summary": {"passed": counts[PASS], "failed": counts[FAIL],
                            "advisory": counts[WARN], "skipped_ode": counts[SKIP]},
                "results": results,
            }, f, indent=2)
        print("  verdict written to %s" % args.json_out)

    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
