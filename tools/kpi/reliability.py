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


def binom_cdf(k, n, p):
    """P(X <= k), X ~ Bin(n, p), summed in log space (fine for k up to a few thousand)."""
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0 if k < n else 1.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(0, k + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return min(1.0, total)


def bisect(f, lo, hi, it=200):
    for _ in range(it):
        mid = (lo + hi) / 2
        if f(mid):
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def clopper_pearson(k, n, conf=0.95):
    a = (1 - conf) / 2
    lo = 0.0 if k == 0 else bisect(lambda p: 1 - binom_cdf(k - 1, n, p) >= a, 0.0, 1.0)
    hi = 1.0 if k == n else bisect(lambda p: binom_cdf(k, n, p) <= a, 0.0, 1.0)
    return lo, hi


def one_sided_upper(k, n, conf=0.95):
    return 1.0 if k == n else bisect(lambda p: binom_cdf(k, n, p) <= 1 - conf, 0.0, 1.0)


def main(argv):
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
