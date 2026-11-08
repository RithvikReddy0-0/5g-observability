#!/usr/bin/env python3
"""
reliability.py — URLLC reliability of one KPI run, in the sense of 3GPP TS 22.261 (clause 3.1:
"percentage value of the packets successfully delivered to a given system entity within the time
constraint required by the targeted service out of all the packets transmitted").

A packet counts as a failure if it was lost OR arrived later than the bound. For each bound:
failures k out of n sent, the measured reliability 1 - k/n, and an exact (Clopper-Pearson) 95 %
confidence interval for the failure probability; with k = 0 also the "rule of three" upper bound
3/n (Hanley & Lippman-Hand 1983), which is the one-sided 95 % bound to ~1 %.

n is the collector's "expected" count (sequence-number span per device and epoch, so losses inside
the window are counted; a packet lost after a device's last received one is not — see
kpi_collector.py). Late counts come from the collector's owd_over_ms.

    python3 tools/kpi/reliability.py <run dir>  [--target 0.99999]
"""

import json
import math
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stats import clopper_pearson, clopper_pearson_upper  # noqa: E402  (one implementation, tested)


def one_sided_upper(k, n, conf=0.95):
    return clopper_pearson_upper(k, n, conf=conf)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the table uses an en dash; Windows consoles are cp1252
    run = argv[0]
    target = float(argv[argv.index("--target") + 1]) if "--target" in argv else 0.99999
    recs = [json.loads(l) for l in open(os.path.join(run, "instances.jsonl"), encoding="utf-8") if l.strip()]
    rec = [r for r in recs if r["label"] == "standard"][-1]
    m = rec["measured"]
    n, received = m.get("urllc.expected"), m["urllc.packets"]
    lost = n - received
    over = rec.get("urllc_owd_over_ms") or {}
    print("Run %s: %s s, %d URLLC devices; sent (by sequence numbers) %d, received %d, lost %d"
          % (os.path.basename(os.path.normpath(run)), rec["duration_s"], m["urllc.devices"], n, received, lost))
    per = rec.get("urllc_per_device") or {}
    bad = {d: v["expected"] - v["received"] for d, v in per.items() if v["expected"] != v["received"]}
    print("devices with loss: %s" % (bad or "none"))
    print()
    print("| Failure = | k | n | Reliability measured | 95 %% CI of failure probability | One-sided 95 %% upper bound | Meets %.5g %%? |" % (100 * target))
    print("|---|---|---|---|---|---|---|")
    bounds = [("lost (no latency bound)", lost)] + [("lost or later than %s ms" % t, lost + over[t]) for t in sorted(over, key=float)]
    for what, k in bounds:
        lo, hi = clopper_pearson(k, n)
        up = 3.0 / n if k == 0 else one_sided_upper(k, n)
        verdict = "yes (95 %)" if up <= 1 - target else ("no: measured worse" if k / n > 1 - target else "not shown")
        print("| %s | %d | %d | %.6f %% | %.2e – %.2e | %.2e%s | %s |" % (
            what, k, n, 100 * (1 - k / n), lo, hi, up, " (3/n)" if k == 0 else "", verdict))
    print()
    print("To show %.5g %% with zero failures at 95 %% confidence needs n >= %d packets (rule of three)."
          % (100 * target, math.ceil(3 / (1 - target))))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
