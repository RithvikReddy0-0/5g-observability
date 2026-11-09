"""Analysis of the 250 vs 600 Mbps cell campaign (docs/evidence/open5gs-cell-capacity)."""
import csv
import json
import os
import statistics as st
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
sys.path.insert(0, REPO + "/tools/kpi")
import stats as S  # noqa: E402

base = REPO + "/docs/evidence/open5gs-cell-capacity"
rows = list(csv.DictReader(open(base + "/campaign.tsv", encoding="utf-8"), delimiter="\t"))
data = {}
for r in rows:
    d = os.path.join(base, "kpi", r["run_dir"])
    rec = [json.loads(l) for l in open(d + "/instances.jsonl", encoding="utf-8") if l.strip()]
    rec = [x for x in rec if x["label"] == "standard"][-1]
    m, c = rec["measured"], rec["card"]
    sc = {s["slice"]: s["score"] for s in c["slices"]}
    per = rec.get("urllc_per_device") or {}
    lost_devs = sum(1 for v in per.values() if v["expected"] != v["received"])
    mode = open(d + "/scheduler-mode.txt", encoding="utf-8").read().strip()
    data.setdefault(r["arm"], []).append(dict(
        round=int(r["round"]), run=r["run_dir"], mode=mode, broken=int(r["broken_after"]),
        overall=c["overall"], urllc=sc["URLLC"], embb=sc["eMBB"], mmtc=sc["mMTC"],
        embb_agg=m["embb.dl_aggregate_mbps"], embb_p5=m["embb.dl_p5_mbps"], flows=m["embb.flows_completed"],
        p99=m["urllc.owd_p99_ms"], late10=m.get("urllc.late_over_10ms"), loss=m["urllc.loss_ratio"],
        lost_devs=lost_devs, mmtc_loss=m["mmtc.loss_ratio"]))

for arm, rs in data.items():
    print("==", arm)
    for x in sorted(rs, key=lambda x: x["round"]):
        print("  r%d %s [%s] overall %.2f (U %.2f E %.2f M %.2f) eMBB %.0f p5 %.1f flows %d | URLLC p99 %.2f late %s loss %.4f devs %d | mMTC loss %.4f | broken %d"
              % (x["round"], x["run"], x["mode"], x["overall"], x["urllc"], x["embb"], x["mmtc"], x["embb_agg"], x["embb_p5"],
                 x["flows"], x["p99"], x["late10"], x["loss"], x["lost_devs"], x["mmtc_loss"], x["broken"]))

def compare(label, keep):
    a = [r for r in data["cell250"] if keep(r)]
    b = [r for r in data["cell600"] if keep(r)]
    print()
    print("### %s: rounds %s" % (label, sorted({r["round"] for r in a + b})))
    for k in ("overall", "embb_agg", "embb", "urllc", "p99", "late10", "loss"):
        x = [r[k] for r in a]
        y = [r[k] for r in b]
        mw = S.mann_whitney_u(x, y)
        print("%-9s 250: median %8.3f [%s]   600: median %8.3f [%s]   p = %.3f (%s)  Cliff's delta(250 vs 600) = %+.2f"
              % (k, st.median(x), ", ".join("%.3g" % v for v in x), st.median(y), ", ".join("%.3g" % v for v in y),
                 mw["p"], mw["method"], S.cliffs_delta(x, y)))
    for k in ("overall", "embb_agg", "p99"):
        by = {r["round"]: r[k] for r in a}
        d = [ry[k] - by[ry["round"]] for ry in b if ry["round"] in by]
        print("paired 600-250 by round, %s: %s" % (k, ["%+.3g" % v for v in d]))


compare("primary: rounds 2-5 (round 1's 250 run hit the collector stall)", lambda r: r["round"] >= 2)
compare("all runs, round 1 included", lambda r: True)
