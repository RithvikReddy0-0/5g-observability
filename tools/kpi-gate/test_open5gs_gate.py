#!/usr/bin/env python3
"""
test_open5gs_gate.py — run the Open5GS KPI gate against a healthy stub stack and against
each fault, and require the exact verdict for every one.

For each scenario in stub_open5gs.SCENARIOS this starts the stub on a free local port, runs the
real gate (kpi_gate.py --defs deployments/open5gs/kpi-gates.json) as a separate process, and
checks its JSON verdict:

  * the exit code (0 for the healthy stack, 1 for a fault),
  * the set of FAILED gates is exactly the expected one — so a fault is caught by the KPI that
    is meant to catch it, and nothing else fails by accident,
  * the set of WARNed advisories is exactly the expected one.

Finally, every enforcing gate in the definitions file must have been seen to FAIL in at least
one scenario, and every advisory to WARN: a gate nobody has watched reject anything is
indistinguishable from one that always passes. Adding a gate therefore needs a scenario here.

Standard library only.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stub_open5gs  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))
DEFS = os.path.join(REPO, "deployments", "open5gs", "kpi-gates.json")
GATE = os.path.join(HERE, "kpi_gate.py")


def run(scenario):
    server = HTTPServer(("127.0.0.1", 0), stub_open5gs.make_handler(scenario))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fd, verdict_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        proc = subprocess.run(
            [sys.executable, GATE, "--defs", DEFS, "--json", verdict_path,
             "--prometheus", "http://127.0.0.1:%d" % server.server_address[1]],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            env=dict(os.environ, NO_COLOR="1", PYTHONIOENCODING="utf-8"))
        with open(verdict_path) as f:
            verdict = json.load(f)
    finally:
        server.shutdown()
        server.server_close()
        os.unlink(verdict_path)
    return proc, verdict


def main():
    defs = json.load(open(DEFS, encoding="utf-8"))["gates"]
    enforcing = {g["id"] for g in defs if g["severity"] == "gate"}
    advisory = {g["id"] for g in defs if g["severity"] == "advisory"}
    seen_fail, seen_warn, bad = set(), set(), 0

    for scenario, (_fault, want_rc, want_fail, want_warn) in stub_open5gs.SCENARIOS.items():
        proc, verdict = run(scenario)
        by_status = {}
        for r in verdict["results"]:
            by_status.setdefault(r["status"], {})[r["id"]] = r["detail"]
        got_fail = set(by_status.get("FAIL", {}))
        got_warn = set(by_status.get("WARN", {}))
        seen_fail |= got_fail
        seen_warn |= got_warn

        problems = []
        if proc.returncode != want_rc:
            problems.append("exit %d, expected %d" % (proc.returncode, want_rc))
        if got_fail != want_fail:
            problems.append("FAIL %s, expected %s" % (sorted(got_fail), sorted(want_fail)))
        if got_warn != want_warn:
            problems.append("WARN %s, expected %s" % (sorted(got_warn), sorted(want_warn)))

        s = verdict["summary"]
        print("[%s] %-21s exit %d  %s  (%d passed, %d failed, %d advisory)"
              % ("ok" if not problems else "FAILED", scenario, proc.returncode,
                 verdict["verdict"], s["passed"], s["failed"], s["advisory"]))
        for gid in sorted(got_fail):
            print("       FAIL %-30s %s" % (gid, by_status["FAIL"][gid]))
        for p in problems:
            bad += 1
            print("::error::scenario %s: %s" % (scenario, p))
        if problems:
            print(proc.stdout)

    for gid in sorted(enforcing - seen_fail):
        bad += 1
        print("::error::gate %r was never seen to FAIL — add a scenario to stub_open5gs.py" % gid)
    for gid in sorted(advisory - seen_warn):
        bad += 1
        print("::error::advisory %r was never seen to WARN — add a scenario to stub_open5gs.py"
              % gid)
    print("\n%d enforcing gates each seen to FAIL, %d advisories each seen to WARN, %d problem(s)"
          % (len(enforcing & seen_fail), len(advisory & seen_warn), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
