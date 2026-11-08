#!/usr/bin/env python3
"""
compare_arms.py — tables for a same-day comparison of KPI runs grouped into arms.

Input: a campaign index (TSV with at least the columns `arm` and `run_dir`; run_dir relative to the
index's `kpi/` folder or to the repo) such as docs/evidence/open5gs-scheduler-algorithms/campaign.tsv.
Each run directory is a tools/kpi/run_kpis.py run (instances.jsonl, standard instance).

Output (stdout, Markdown): every run in the order it ran, then each arm's median with its min-max
range. Nothing is averaged away: the per-run table is the evidence, the summary only condenses it.

    python3 tools/kpi/compare_arms.py docs/evidence/open5gs-scheduler-algorithms/campaign.tsv [arm ...]
    python3 tools/kpi/compare_arms.py --stats <index> "a,b:c,d" ...   permutation tests on URLLC p99
        (difference of medians, group a+b against c+d, 20 000 shuffles, fixed seed) and the Spearman
        rank correlation of URLLC p99 with every CPU and load figure over all runs.
"""

import csv
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

COLS = [  # (header, key, format)
    ("URLLC p50", "urllc.owd_p50_ms", "%.2f"), ("p95", "urllc.owd_p95_ms", "%.2f"), ("p99", "urllc.owd_p99_ms", "%.2f"),
    ("p99.9", "urllc.owd_p999_ms", "%.1f"), ("max", "urllc.owd_max_ms", "%.0f"),
    (">10 ms", "urllc.late_over_10ms", "%d"), ("URLLC pkts", "urllc.packets", "%d"), ("URLLC loss", "urllc.loss_ratio", "%.2g"),
    ("devs w/ loss", "urllc.devices_with_loss", "%d"), ("worst dev lost", "urllc.worst_device_lost", "%d"),
    ("eMBB agg Mbps", "embb.dl_aggregate_mbps", "%.0f"), ("eMBB p5", "embb.dl_p5_mbps", "%.1f"),
    ("flows", "embb.flows_completed", "%d"), ("mMTC loss", "mmtc.loss_ratio", "%.2g"), ("mMTC p99", "mmtc.owd_p99_ms", "%.0f"),
    ("UE CPU %", "cpu.ue", "%.0f"), ("load", "host.load1_end", "%.1f"),
]
SCORES = ["URLLC", "eMBB", "mMTC"]


def load(index):
    base = os.path.dirname(os.path.abspath(index))
    runs = []
    with open(index, encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            d = row["run_dir"]
            for cand in (os.path.join(base, "kpi", d), os.path.join(base, d), os.path.join(REPO, d)):
                if os.path.exists(os.path.join(cand, "instances.jsonl")):
                    break
            else:
                continue
            with open(os.path.join(cand, "instances.jsonl"), encoding="utf-8") as g:
                recs = [json.loads(l) for l in g if l.strip()]
            rec = [r for r in recs if r["label"] == "standard"][-1]
            m = dict(rec["measured"])
            # loss per device: one device's radio-link failure is not loss spread over the slice
            lost = {d: v["expected"] - v["received"] for d, v in (rec.get("urllc_per_device") or {}).items()}
            m["urllc.devices_with_loss"] = sum(1 for v in lost.values() if v > 0)
            m["urllc.worst_device_lost"] = max(lost.values()) if lost else None
            # a stuck eMBB download makes the runner wait up to 60 s more: URLLC measured over a longer window
            m["window_extended"] = (m.get("embb.flows_completed") or 0) < (m.get("embb.ues_loaded") or 0)
            for s in rec["card"]["slices"]:
                m["score." + s["slice"]] = s["score"]
            m["score.overall"] = rec["card"]["overall"]
            runs.append({"arm": row["arm"], "run": os.path.basename(cand), "seq": row.get("seq", ""), "round": row.get("round"),
                         "broken": row.get("broken_after", ""), "m": m})
    return runs


def fmt(v, f):
    if v is None:
        return "—"
    try:
        return f % v
    except TypeError:
        return str(v)


def ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0] * len(v)
    for k, i in enumerate(order):
        r[i] = k
    return r


def spearman(x, y):
    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


def permutation(a, b, n=20000, seed=1):
    """Two-sided permutation test on the difference of medians."""
    import random
    rnd = random.Random(seed)
    obs = statistics.median(a) - statistics.median(b)
    pool, hits = list(a) + list(b), 0
    for _ in range(n):
        rnd.shuffle(pool)
        if abs(statistics.median(pool[:len(a)]) - statistics.median(pool[len(a):])) >= abs(obs) - 1e-12:
            hits += 1
    return obs, hits / n


def stats(index, comparisons, key="urllc.owd_p99_ms"):
    runs = load(index)
    val = lambda arms: [r["m"][key] for r in runs if r["arm"] in arms and r["m"].get(key) is not None]
    print("| Comparison (%s) | n | Median difference | p |" % key)
    print("|---|---|---|---|")
    for c in comparisons:
        left, right = (s.split(",") for s in c.split(":"))
        a, b = val(left), val(right)
        d, p = permutation(a, b)
        print("| %s − %s | %d / %d | %+.2f | %.2f |" % ("+".join(left), "+".join(right), len(a), len(b), d, p))
    print()
    y = [r["m"][key] for r in runs]
    print("| Spearman ρ with %s over %d runs | ρ |" % (key, len(runs)))
    print("|---|---|")
    keys = sorted(k for k in runs[0]["m"] if k.startswith(("cpu.", "host."))) + ["embb.dl_aggregate_mbps"]
    for k in keys:
        x = [r["m"].get(k) for r in runs]
        if None not in x:
            print("| %s | %+.2f |" % (k, spearman(x, y)))
    print("| run order | %+.2f |" % spearman([int(r["seq"] or 0) for r in runs], y))
    blocked(runs, key)


def blocked(runs, key):
    """Within-round ranks (each round contains every arm once, so ranking inside a round removes
    the drift between rounds) and Friedman's test over the complete rounds."""
    rounds = {}
    for r in runs:
        rounds.setdefault(r["round"], {})[r["arm"]] = r["m"].get(key)
    arms = sorted({r["arm"] for r in runs})
    full = [v for v in rounds.values() if set(v) == set(arms) and None not in v.values()]
    if len(full) < 2 or len(arms) < 2:
        return
    sums = {a: 0.0 for a in arms}
    for v in full:
        order = sorted(arms, key=lambda a: v[a])
        for i, a in enumerate(order, start=1):
            sums[a] += i
    n, k = len(full), len(arms)
    chi2 = 12.0 / (n * k * (k + 1)) * sum(s * s for s in sums.values()) - 3 * n * (k + 1)
    df = k - 1
    p = None
    if df % 2 == 0:                                   # chi-square survival function, even df: closed form
        x, term, tot = chi2 / 2, 1.0, 1.0
        for i in range(1, df // 2):
            term *= x / i
            tot += term
        p = math.exp(-x) * tot
    print()
    print("| Arm | mean rank of %s within a round (1 = shortest), %d complete rounds |" % (key, n))
    print("|---|---|")
    for a in sorted(arms, key=lambda a: sums[a]):
        print("| %s | %.2f |" % (a, sums[a] / n))
    print()
    print("Friedman chi-square %.2f, df %d%s" % (chi2, df, "" if p is None else ", p = %.2f" % p))


def main(argv):
    if argv and argv[0] == "--stats":
        return stats(argv[1], argv[2:])
    runs = load(argv[0])
    arms = argv[1:] or list(dict.fromkeys(r["arm"] for r in runs))
    runs = [r for r in runs if r["arm"] in arms]
    head = ["#", "Run", "Arm"] + [h for h, _, _ in COLS] + ["Scores U / e / m", "Overall", "broken after"]
    for r in runs:
        if r["m"].get("window_extended"):
            r["run"] += " (stuck download: window +≤60 s)"
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in runs:
        m = r["m"]
        print("| %s | `%s` | %s | %s | %s | %s | %s |" % (
            r["seq"], r["run"], r["arm"], " | ".join(fmt(m.get(k), f) for _, k, f in COLS),
            " / ".join(fmt(m.get("score." + s), "%.2f") for s in SCORES), fmt(m.get("score.overall"), "%.2f"), r["broken"]))
    print()
    keys = [("URLLC p99 (ms)", "urllc.owd_p99_ms", "%.2f"), ("URLLC p99.9 (ms)", "urllc.owd_p999_ms", "%.1f"),
            ("URLLC >10 ms (pkts)", "urllc.late_over_10ms", "%.0f"), ("URLLC loss", "urllc.loss_ratio", "%.2g"),
            ("eMBB agg (Mbps)", "embb.dl_aggregate_mbps", "%.0f"), ("eMBB p5 (Mbps)", "embb.dl_p5_mbps", "%.1f"),
            ("mMTC p99 (ms)", "mmtc.owd_p99_ms", "%.0f"), ("UE CPU (%)", "cpu.ue", "%.0f"), ("Overall", "score.overall", "%.2f")]
    print("| Arm | runs | " + " | ".join(h for h, _, _ in keys) + " |")
    print("|" + "---|" * (len(keys) + 2))
    for a in arms:
        sub = [r["m"] for r in runs if r["arm"] == a]
        cells = []
        for _, k, f in keys:
            v = [x.get(k) for x in sub if x.get(k) is not None]
            cells.append("—" if not v else "%s (%s–%s)" % (fmt(statistics.median(v), f), fmt(min(v), f), fmt(max(v), f)))
        print("| %s | %d | %s |" % (a, len(sub), " | ".join(cells)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
