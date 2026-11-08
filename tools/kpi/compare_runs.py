#!/usr/bin/env python3
"""
compare_runs.py — compare KPI runs grouped by experimental arm, with the statistics that say
whether a difference is supported (ADR-023).

    tools/kpi/compare_runs.py --arm off=DIR,DIR --arm sliced250=DIR,DIR,DIR [--arm ...]
                              [--baseline off] [--pdb-ms 10] [--boot 10000] [--markdown]

Each DIR is a run directory of tools/kpi/run_kpis.py (its instances.jsonl); the instance used is
the latest one labelled "standard" (--label to choose another).

TWO KINDS OF SAMPLE, and why both
  Per run    one value per run directory: p99 latency, loss, aggregate throughput, scores. This is
             the experimental unit — the arm (scheduler mode, cell) is set per run — so it is the
             one the tests are about. It is small: 2-3 runs per arm. The exact Mann-Whitney test
             cannot give p < 0.05 with fewer than 4 runs in each arm (min p is printed).
  Per device instances.jsonl also keeps each device's own result: an eMBB device's download rate,
             a URLLC device's mean latency and loss, an IoT device's mean latency. These are many
             (20 / 10 / 70 per run), but devices in one run share the same laptop, the same eMBB
             load and the same moment, so they are not independent: pooling them across runs
             treats 2 runs as if they were 40. They are reported with a two-stage (cluster)
             bootstrap that resamples runs first, and their Mann-Whitney p-values are marked as
             ignoring the clustering (anti-conservative): descriptive, not a test of the arm.
  Per packet instances.jsonl does not keep per-packet delays (only counts and percentiles), so
             latency tails can be compared per run only. Packet COUNTS are kept, which is what a
             reliability bound needs.

RELIABILITY (URLLC)
  Loss: k lost of n sent per arm (sequence numbers), with the Clopper-Pearson bound — the rule of
  three when k = 0 — on the loss rate, and the packets a zero-loss run needs to show a given rate.
  Late packets: TS 22.261 (clause 3.1) counts a packet as delivered only within the service's time
  constraint, and TS 23.501 (5.7.3.4/5.7.3.5) counts a delay-critical GBR packet later than the PDB
  as lost. The runs keep percentiles, not every delay, so the late fraction is bounded from them:
  p99 > PDB means more than ~1 % late, max <= PDB means none.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stats as S  # noqa: E402

URLLC_PPS_PER_DEVICE = 50     # 64-byte packet every 20 ms (traffic_agent.py default profile)

# (key, label, unit, better) - per-run values; "card:" keys come from the scorecard.
RUN_KPIS = [
    ("urllc.owd_mean_ms", "URLLC latency mean", "ms", "lower"),
    ("urllc.owd_p95_ms", "URLLC latency p95", "ms", "lower"),
    ("urllc.owd_p99_ms", "URLLC latency p99", "ms", "lower"),
    ("urllc.owd_max_ms", "URLLC latency max", "ms", "lower"),
    ("urllc.jitter_ms", "URLLC jitter (mean abs. IPDV)", "ms", "lower"),
    ("urllc.loss_ratio", "URLLC loss", "ratio", "lower"),
    ("embb.dl_p5_mbps", "eMBB DL p5 per device", "Mbps", "higher"),
    ("embb.dl_mean_mbps", "eMBB DL mean per device", "Mbps", "higher"),
    ("embb.dl_aggregate_mbps", "eMBB DL aggregate", "Mbps", "higher"),
    ("embb.flows_completed", "eMBB downloads completed", "of 20", "higher"),
    ("mmtc.loss_ratio", "mMTC report loss", "ratio", "lower"),
    ("mmtc.owd_p99_ms", "mMTC latency p99", "ms", "lower"),
    ("card:URLLC", "URLLC score", "", "higher"),
    ("card:eMBB", "eMBB score", "", "higher"),
    ("card:mMTC", "mMTC score", "", "higher"),
    ("card:overall", "Overall score", "", "higher"),
]


def load_run(run_dir, label="standard"):
    """The latest instance with this label (a re-run replaces an earlier record, as in run_kpis)."""
    path = os.path.join(run_dir, "instances.jsonl")
    with open(path, encoding="utf-8") as f:
        recs = [json.loads(line) for line in f if line.strip()]
    recs = [r for r in recs if r.get("label") == label]
    if not recs:
        raise SystemExit("%s has no instance labelled %r" % (path, label))
    rec = recs[-1]
    rec["_dir"] = run_dir
    return rec


def run_value(rec, key):
    if key.startswith("card:"):
        name = key[5:]
        if name == "overall":
            return rec["card"].get("overall")
        return next((s["score"] for s in rec["card"]["slices"] if s["slice"] == name), None)
    return rec["measured"].get(key)


def per_device(rec, kind):
    """Per-device samples of one run."""
    if kind == "embb_mbps":
        return [f["mbps"] for f in rec.get("embb_flows") or [] if f.get("mbps") is not None]
    if kind == "urllc_owd_mean_ms":
        return [d.get("owd_mean_ms") for d in (rec.get("urllc_per_device") or {}).values()]
    if kind == "urllc_loss":
        return [1 - d["received"] / d["expected"] for d in (rec.get("urllc_per_device") or {}).values() if d["expected"]]
    if kind == "mmtc_owd_mean_ms":
        return [d.get("owd_mean_ms") for d in (rec.get("mmtc_per_device") or {}).values()]
    raise KeyError(kind)


DEVICE_KPIS = [
    ("embb_mbps", "eMBB DL per device", "Mbps"),
    ("urllc_owd_mean_ms", "URLLC mean latency per device", "ms"),
    ("urllc_loss", "URLLC loss per device", "ratio"),
    ("mmtc_owd_mean_ms", "mMTC mean latency per device", "ms"),
]


# ------------------------------------------------------------------ formatting

def fmt(v, unit=""):
    if v is None:
        return "-"
    if unit == "ratio":
        return "%.3g %%" % (100 * v)
    if isinstance(v, float):
        if abs(v) >= 100:
            return "%.0f" % v
        if abs(v) >= 10:
            return "%.1f" % v
        return "%.2f" % v
    return str(v)


def fmt_p(p):
    return "-" if p is None else ("%.3f" % p if p >= 0.001 else "%.1e" % p)


class Out:
    def __init__(self, markdown):
        self.md = markdown

    def h(self, text, level=2):
        print()
        print(("#" * level + " " + text) if self.md else (text + "\n" + "=" * len(text)))
        print()

    def table(self, head, rows):
        if self.md:
            print("| " + " | ".join(head) + " |")
            print("|" + "|".join("---" for _ in head) + "|")
            for r in rows:
                print("| " + " | ".join(str(c) for c in r) + " |")
        else:
            widths = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(head)]
            print("  ".join(str(h).ljust(w) for h, w in zip(head, widths)))
            for r in rows:
                print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
        print()

    def p(self, text=""):
        print(text)


# ------------------------------------------------------------------ analysis

def late_bounds(m, budget_ms):
    """(low, high) bounds on the fraction of URLLC packets later than budget_ms, from the run's
    stored quantiles: a quantile q above the budget means more than 1 - q of packets are late; the
    maximum at or under it means none are."""
    quantiles = [(q, m.get("urllc.owd_%s_ms" % name)) for q, name in ((0.95, "p95"), (0.99, "p99"))]
    low = max([1 - q for q, v in quantiles if v is not None and v > budget_ms] + [0.0])
    mx = m.get("urllc.owd_max_ms")
    if mx is not None and mx <= budget_ms:
        return low, 0.0
    high = min([1 - q for q, v in quantiles if v is not None and v <= budget_ms] + [1])
    return low, high


def pairs(arms, baseline):
    names = list(arms)
    if baseline:
        return [(a, baseline) for a in names if a != baseline]
    return [(names[i], names[j]) for i in range(len(names)) for j in range(i + 1, len(names))]


def verdict(p, n1, n2):
    if p is None:
        return "-"
    if p < 0.05:
        return "supported (p < 0.05)"
    if S.min_exact_p(n1, n2) >= 0.05:
        return "not testable at this n"
    return "not supported"


def analyse(arms, args, out):
    boot = dict(n_boot=args.boot, seed=args.seed)

    out.h("Runs")
    rows = []
    for name, recs in arms.items():
        for r in recs:
            m = r["measured"]
            dev = m.get("urllc.devices") or 0
            window = (m.get("urllc.packets") or 0) / ((1 - (m.get("urllc.loss_ratio") or 0)) or 1) / (dev * URLLC_PPS_PER_DEVICE) if dev else None
            flag = ""
            if window and abs(window - r["duration_s"]) > 0.15 * r["duration_s"]:
                flag = "URLLC window ~%.0f s, not %s s" % (window, r["duration_s"])
            rows.append([name, os.path.basename(os.path.normpath(r["_dir"])), r.get("started", ""), r["duration_s"], flag or "ok"])
    out.table(["arm", "run", "started", "window (s)", "URLLC window check"], rows)
    out.p("The URLLC window is estimated from the packets the collector expected (10 devices x %d packets/s). "
          "A window much longer than the instance means URLLC statistics include time without the eMBB load." % URLLC_PPS_PER_DEVICE)

    out.h("Per run: medians and 95 % bootstrap intervals")
    head = ["KPI"] + ["%s (n=%d)" % (a, len(r)) for a, r in arms.items()]
    rows = []
    for key, label, unit, _ in RUN_KPIS:
        row = [label + (" (%s)" % unit if unit and unit != "ratio" else "")]
        for recs in arms.values():
            vals = [run_value(r, key) for r in recs]
            vals = [v for v in vals if v is not None]
            if not vals:
                row.append("-")
                continue
            lo, hi = S.bootstrap_ci(vals, **boot)
            row.append("%s [%s, %s]" % (fmt(S.median(vals), unit), fmt(lo, unit), fmt(hi, unit)))
        rows.append(row)
    out.table(head, rows)
    out.p("With n = 2 the interval is the range of the two runs and with n = 3 nearly so: it describes the spread, "
          "it is not a 95 % confidence statement.")

    out.h("Per run: pairwise tests (exact Mann-Whitney U, two-sided; Cliff's delta)")
    rows = []
    for a, b in pairs(arms, args.baseline):
        for key, label, unit, better in RUN_KPIS:
            x = [run_value(r, key) for r in arms[a]]
            y = [run_value(r, key) for r in arms[b]]
            x = [v for v in x if v is not None]
            y = [v for v in y if v is not None]
            if not x or not y:
                continue
            t = S.mann_whitney_u(x, y)
            d = S.cliffs_delta(x, y)
            rows.append(["%s vs %s" % (a, b), label, fmt(S.median(x), unit), fmt(S.median(y), unit),
                         "%+.2f" % d, fmt_p(t["p"]), t["method"], fmt_p(S.min_exact_p(len(x), len(y))),
                         verdict(t["p"], len(x), len(y))])
    out.table(["comparison", "KPI", "median A", "median B", "Cliff's delta", "p", "test", "min p possible", "verdict"], rows)
    out.p("Cliff's delta: +1 = every run of A above every run of B, -1 = every run below, 0 = no ordering. "
          "'not testable at this n': even a complete separation could not reach p < 0.05. 'permutation': exact test "
          "over every split of the runs, used when values tie (e.g. two runs with zero loss).")
    out.p()
    out.p("Smallest two-sided p the exact test can give, by runs per arm: " + ", ".join(
        "%d vs %d: %s" % (a, b, fmt_p(S.min_exact_p(a, b))) for a, b in ((2, 2), (2, 3), (3, 3), (2, 7), (3, 4), (4, 4), (5, 5))) + ".")

    out.h("Per device (pooled over runs; clustered by run)")
    rows = []
    for kind, label, unit in DEVICE_KPIS:
        for name, recs in arms.items():
            groups = [per_device(r, kind) for r in recs]
            pooled = [v for g in groups for v in g if v is not None]
            if not pooled:
                continue
            lo, hi = S.cluster_bootstrap_ci(groups, **boot)
            rows.append([label, name, len(pooled), len(groups), fmt(S.median(pooled), unit), "[%s, %s]" % (fmt(lo, unit), fmt(hi, unit))])
    out.table(["KPI", "arm", "devices", "runs", "median", "95 % cluster-bootstrap CI"], rows)

    rows = []
    for a, b in pairs(arms, args.baseline):
        for kind, label, unit in DEVICE_KPIS:
            x = [v for r in arms[a] for v in per_device(r, kind) if v is not None]
            y = [v for r in arms[b] for v in per_device(r, kind) if v is not None]
            if not x or not y:
                continue
            t = S.mann_whitney_u(x, y)
            lo, hi = S.bootstrap_diff_ci(x, y, **boot)
            rows.append(["%s vs %s" % (a, b), label, "%d / %d" % (len(x), len(y)), "%+.2f" % S.cliffs_delta(x, y),
                         "%s [%s, %s]" % (fmt(S.median(x) - S.median(y), unit), fmt(lo, unit), fmt(hi, unit)), fmt_p(t["p"])])
    out.table(["comparison", "KPI", "n A / n B", "Cliff's delta", "median difference [pooled 95 % CI]", "p (ignores clustering)"], rows)
    out.p("These p-values treat every device of every run as independent. Devices of one run share the host, the eMBB "
          "load and the moment, so the effective sample size is closer to the number of runs: read these as a description "
          "of how consistent the per-device ordering is, not as tests of the arm.")

    out.h("URLLC reliability")
    pdb = args.pdb_ms
    rows = []
    for name, recs in arms.items():
        rec_n = rec_k = 0
        late_lo, late_hi = [], []
        for r in recs:
            m = r["measured"]
            recv = m.get("urllc.packets") or 0
            loss = m.get("urllc.loss_ratio") or 0.0
            sent = int(round(recv / (1 - loss))) if loss < 1 else recv
            rec_n += sent
            rec_k += sent - recv
            lo, hi = late_bounds(m, pdb)
            late_lo.append(lo)
            late_hi.append(hi)
        lo95, hi95 = S.clopper_pearson(rec_k, rec_n)
        upper1 = S.clopper_pearson_upper(rec_k, rec_n)
        rows.append([name, rec_n, rec_k, fmt(rec_k / rec_n if rec_n else None, "ratio"),
                     "[%s, %s]" % (fmt(lo95, "ratio"), fmt(hi95, "ratio")),
                     fmt(upper1, "ratio") + (" (rule of 3: %s)" % fmt(S.rule_of_three(rec_n), "ratio") if rec_k == 0 else ""),
                     "%.4f %%" % (100 * (1 - upper1)),
                     ", ".join("%s-%s" % (fmt(a, "ratio"), fmt(b, "ratio")) for a, b in zip(late_lo, late_hi))])
    out.table(["arm", "packets sent", "lost", "loss", "95 % CI (Clopper-Pearson)", "one-sided 95 % upper bound",
               "delivery >= (95 %)", "late > %g ms, per run (bounds from percentiles)" % pdb], rows)
    out.p("'delivery >=' counts arrival at any time. Counting late packets as failures (TS 22.261 3.1, TS 23.501 5.7.3.4) "
          "lowers reliability by the late fraction, bounded per run from p95/p99/max.")
    out.p("Losses within a run are not independent (a radio-link failure loses a burst from one device), so the binomial "
          "bound is optimistic when k > 0; with k = 0 it is the bound the data allow, assuming independent packets.")
    out.p()
    rows = []
    for target, what in ((1e-4, "5QI 82 PER 10^-4 (TS 23.501 Table 5.7.4-1)"), (1e-5, "99.999 % reliability (TS 22.261 Table 7.2.3.2-1 example)"), (1e-6, "99.9999 %")):
        n = S.zero_failure_n(target)
        rows.append([what, n, "%.0f s (%.1f min)" % (n / (10.0 * URLLC_PPS_PER_DEVICE), n / (10.0 * URLLC_PPS_PER_DEVICE) / 60)])
    out.table(["claim (one-sided 95 %)", "consecutive packets with no loss and none late", "at 10 devices x 50 packets/s"], rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", action="append", required=True, metavar="NAME=DIR[,DIR...]")
    ap.add_argument("--baseline", help="compare every arm with this one only (default: all pairs)")
    ap.add_argument("--label", default="standard", help="instance label to use from each run (default: standard)")
    ap.add_argument("--pdb-ms", type=float, default=10.0, help="URLLC delay budget for the late-packet bounds (5QI 82: 10 ms)")
    ap.add_argument("--boot", type=int, default=S.DEFAULT_BOOT)
    ap.add_argument("--seed", type=int, default=S.DEFAULT_SEED)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)
    arms = {}
    for spec in args.arm:
        if "=" not in spec:
            ap.error("--arm takes NAME=DIR[,DIR...], got %r" % spec)
        name, dirs = spec.split("=", 1)
        arms[name] = [load_run(d, args.label) for d in dirs.split(",") if d]
    if args.baseline and args.baseline not in arms:
        ap.error("baseline %r is not an arm" % args.baseline)
    analyse(arms, args, Out(args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
