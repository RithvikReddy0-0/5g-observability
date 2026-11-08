#!/usr/bin/env python3
"""
incidents.py — the recorded incidents of docs/evidence as diagnosis test cases, and the scorecard (ADR-024).

Each case turns one recorded run into the Facts the diagnosis rules read, and carries a LABEL: the
cause(s) the repository's own evidence documents for it. Labels are not invented here; each case
names the README/ADR that states its cause. Three kinds of label:

  expected    the documented cause(s); a correct diagnosis reports them
  acceptable  also true of that run but not its headline (e.g. the eMBB class of a sliced cell dropping
              packets while the finding of the run is CPU contention); reporting them is not an error
  None        the evidence does not settle the cause ("cause open" in the repo); the diagnosis is
              shown, but not scored

Some incidents survive only as numbers in a README (no raw series): those cases are marked
`reconstructed` and their facts are exactly the documented numbers.

Scoring, per case: reported = causes scoring >= 0.5. Precision = reported causes that are expected or
acceptable / all reported. Recall = expected causes reported / all expected. Top-1 = the first-ranked
cause is an expected one. A healthy case is correct when nothing critical or warning is reported.

    python3 tools/observability/incidents.py            the table and the totals
    python3 tools/observability/diagnose.py --validate  the same
"""

import calendar
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import correlate  # noqa: E402
import diagnose  # noqa: E402
import logparse  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
EVID = os.path.join(REPO, "docs", "evidence")
FIX = os.path.join(REPO, "tests", "fixtures", "observability")
NCPU_LAPTOP = 12        # "a load average of ~10 on 12 threads" (open5gs-scheduler/README.md, finding 5)


def case(cid, facts, expected, acceptable=(), source="", reconstructed=False, note=""):
    return {"id": cid, "facts": facts, "expected": None if expected is None else set(expected),
            "acceptable": set(acceptable), "source": source, "reconstructed": reconstructed, "note": note}


# ─────────────────────────── packet scheduler runs (ADR-019) ───────────────────────────

def _class_drops(classes):
    out = {}
    for k, v in (classes or {}).items():
        direction, _, cls = k.partition(" ")
        sl = cls.split(" ")[0]
        sl = {"all": "cell", "other": "default"}.get(sl, sl)
        out[sl] = out.get(sl, 0) + (v.get("drops") or 0)
    return {k: v for k, v in out.items() if v}


def _probe_facts(info, probes):
    def worst(keys, field):
        vals = [probes[k][field] for k in keys if k in probes and probes[k].get(field) is not None]
        return max(vals) if vals else None
    urllc = [k for k in probes if k.startswith("URLLC")]
    info["urllc_p99_ms"] = worst(urllc, "p99")
    info["urllc_loss_pct"] = worst(urllc, "loss_percent") or 0.0
    info["mmtc_p99_ms"] = worst(["mMTC"], "p99")
    info["mmtc_loss_pct"] = worst(["mMTC"], "loss_percent") or 0.0
    info["embb_p99_ms"] = worst(["eMBB unloaded"], "p99")
    info["embb_loss_pct"] = worst(["eMBB unloaded"], "loss_percent") or 0.0
    for sl in ("urllc", "mmtc", "embb"):
        info["%s_p99_ref" % sl] = "ping p99 from the slice's devices (experiment.py)"


def scheduler_cases():
    path = os.path.join(EVID, "open5gs-scheduler", "20261108-075626Z", "results-merged.json")
    doc = logparse.read_json(path)
    out = []
    for part in "ABCD":
        for i, r in enumerate(doc.get(part, [])):
            info = {"scheduler_mode": r["mode"], "rlf_count": r.get("radio_link_failures") or 0,
                    "ues_without_data_path": r.get("ues_without_data_path_after") or 0,
                    "broken_ref": "experiment.py: every UE checked after the run",
                    "offered_mbps": {k: v["offered_mbps"] for k, v in (r.get("load") or {}).items()},
                    "class_drops": _class_drops(r.get("classes")), "loss_ref": "ping loss per slice (experiment.py)",
                    "planned_ues": 100}
            if r["mode"] != "off":
                info["cell_dl_mbps"] = r["cell_mbps"]["downlink"]
            _probe_facts(info, r.get("probes") or {})
            label, mode = r["label"], r["mode"]
            src = "open5gs-scheduler/20261108-075626Z/results-merged.json %s[%d] (%s)" % (part, i, label)
            if "idle" in label:
                exp, acc, note = set(), (), "no load"
            elif mode == "fifo":
                exp, acc, note = {"cell-congestion", "rlf-no-data-path"}, (), "README finding 1: FIFO cell, 93-99 RLF"
            elif part == "A":
                exp = {"cpu-saturation"} | ({"cell-congestion"} if mode == "sliced" else set())
                acc, note = (), "README finding 5: CPU saturated (load ~10 on 12 threads); URLLC tail is CPU contention"
            elif part == "B":
                exp, acc, note = {"cell-congestion"}, (), "README finding 3: the standard tier floods the cell"
            elif part == "C":
                exp, acc = {"cell-congestion"}, ("cpu-saturation", "rlf-no-data-path")
                note = "README finding 4: 200 Mbps offered into a 60 Mbps cell"
            elif mode == "sliced":
                exp, acc, note = {"cell-congestion"}, (), "README finding 2: eMBB 150 into a 100 Mbps cell, URLLC protected"
            else:  # D, off: no cell
                exp = set() if (info["urllc_p99_ms"] or 0) <= 10 else None
                acc, note = (), "no cell; 150 Mbps is not an overload (README). p99 above budget: cause not stated"
            out.append(case("sched-%s%d-%s" % (part, i, re.sub(r"\W+", "-", label).strip("-")),
                            diagnose.Facts(info=info, source=src), exp, acc, src, note=note))
    return out


# ─────────────────────────── controller runs (ADR-016 / ADR-018) ───────────────────────────

def _hms_epoch(hms, ref_epoch, offset):
    """HH:MM:SS in the host's local time -> epoch, given the UTC offset, on ref_epoch's local day."""
    loc = time.gmtime(ref_epoch + offset)
    h, m, s = (int(x) for x in hms.split(":"))
    return calendar.timegm((loc.tm_year, loc.tm_mon, loc.tm_mday, h, m, s, 0, 0, 0)) - offset


def _tz_offset(flows):
    """Host UTC offset, from a flow that records both its end as epoch and as local HH:MM:SS."""
    for f in flows:
        if f.get("ended") and f.get("finished"):
            h, m, s = (int(x) for x in f["finished"].split(":"))
            utc = time.gmtime(f["ended"])
            delta = (h * 3600 + m * 60 + s) - (utc.tm_hour * 3600 + utc.tm_min * 60 + utc.tm_sec)
            delta = (delta + 12 * 3600) % (24 * 3600) - 12 * 3600
            return int(round(delta / 900.0)) * 900
    return 0


def _control_spans(log, start, offset):
    spans = []
    for e in log:
        t = _hms_epoch(e["t"], start, offset)
        spans.append({"service": "slice-orchestrator", "name": "controller.%s" % e["action"], "start": t, "end": t,
                      "parent_id": None, "status": "ok",
                      "attrs": {"slice": e["slice"], "action": e["action"], "from": e["from"], "to": e["to"],
                                "why": e["why"], "policy": e.get("policy")}})
    return spans


def _runs(path):
    out = {}
    for line in logparse.read_text(path).splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        kv = dict(x.split("=", 1) for x in p if "=" in x)
        nums = [float(x) for x in p if re.fullmatch(r"\d{9,}(\.\d+)?", x)]     # the run's start and end
        out[p[0]] = {"start": nums[0], "end": nums[1],
                     "load_after": float(kv.get("load_after", 0)), "load_before": float(kv.get("load_before", 0))}
    return out


def controller_case(cid, files, run, flows_path, control_path, controlled, throttles_urllc, src):
    series = {}
    names = {"urllc_p95": "urllc_owd_p95", "kpi_owd_recent_ms": "urllc_owd_p95",
             "slice_admission_limit_mbps": "admission_limit", "slice_measured_mbps": "measured_mbps"}
    for f in files:
        base = os.path.basename(f)
        name = next((v for k, v in names.items() if k in base), None)
        if name is None:
            continue
        doc = logparse.read_json(f)
        result = [s for s in doc["data"]["result"] if s["metric"].get("stat", "p95") == "p95"]
        series.update(correlate.matrix_to_series(name, result, None if name == "urllc_owd_p95" else "slice"))
    flows = logparse.read_json(flows_path) if os.path.exists(flows_path) else []
    control = logparse.read_json(control_path) if os.path.exists(control_path) else []
    offset = _tz_offset(flows)
    spans = _control_spans(control, run["start"], offset)
    # Baseline: the run's first 20 s, before the demand built up; window: the rest of the run.
    b0, b1 = run["start"], run["start"] + 20
    rep = correlate.correlate(series, [], spans, b1, run["end"], baseline=(b0, b1), trigger="urllc_owd_p95")
    met = {}
    for sl in ("eMBB", "URLLC"):
        fs = [f for f in flows if f.get("slice") == sl and f.get("outcome") != "preempted"]
        if fs:
            met[sl] = sum(f.get("outcome") == "met" for f in fs) / float(len(fs))
    info = {"host_load1": run["load_after"], "ncpu": NCPU_LAPTOP,
            "host_load_ref": "1-min load average at the end of the run (runs.txt)",
            "capacity_mbps": {"eMBB": 500.0, "URLLC": 20.0, "mMTC": 10.0}, "flows_met": met,
            "embb_flows_met": met.get("eMBB"), "protected_slice": "URLLC"}
    if controlled:
        exp, acc = {"orchestrator-throttling"}, ("cpu-saturation",)
    else:
        exp, acc = {"cpu-saturation"}, ()
    note = ("controller also throttles URLLC (CONTROL_THROTTLE=\"eMBB URLLC\")" if throttles_urllc else
            "controller on (eMBB throttled)" if controlled else "controller off: eMBB admitted to 500 Mbps")
    return case(cid, diagnose.Facts.from_report(rep, info), exp, acc, src, note=note)


def controller_cases():
    out = []
    for sub, urllc_too in (("", False), ("controller-throttling-urllc-too", True)):
        d = os.path.join(EVID, "open5gs-orchestrator-dynamic", sub)
        runs = _runs(os.path.join(d, "2-runs.txt"))
        for mode in ("static", "controlled"):
            files = glob.glob(os.path.join(d, "2-series-%s-*.json" % mode))
            src = "open5gs-orchestrator-dynamic/%s2-*-%s" % (sub + "/" if sub else "", mode)
            out.append(controller_case("dyn-%s%s" % ("urllc-too-" if urllc_too else "", mode), files, runs[mode],
                                       os.path.join(d, "2-flows-%s.json" % mode),
                                       os.path.join(d, "2-control-log-%s.json" % mode),
                                       mode == "controlled", urllc_too and mode == "controlled", src))
    d = os.path.join(EVID, "open5gs-algorithm-comparison", "controller")
    runs = _runs(os.path.join(d, "runs.txt"))
    for rid, run in sorted(runs.items()):
        policy = rid.split("-")[0]
        files = glob.glob(os.path.join(d, "series-%s-*.json" % rid))
        out.append(controller_case("algo-%s" % rid, files, run, os.path.join(d, "flows-%s.json" % rid),
                                   os.path.join(d, "control-%s.json" % rid), policy != "static", False,
                                   "open5gs-algorithm-comparison/controller/*-%s*" % rid))
    return out


# ─────────────────────────── KPI runs (ADR-015) ───────────────────────────

def kpi_cases():
    out = []
    for f in sorted(glob.glob(os.path.join(EVID, "open5gs-kpi", "2026*", "instances.jsonl"))):
        run = os.path.basename(os.path.dirname(f))
        mf = os.path.join(os.path.dirname(f), "scheduler-mode.txt")
        mode_txt = logparse.read_text(mf).strip() if os.path.exists(mf) else ""
        mode = mode_txt.split("-")[0] if mode_txt else None
        for i, line in enumerate(logparse.read_text(f).splitlines()):
            r = json.loads(line)
            m = r["measured"]
            info = {"urllc_p99_ms": m.get("urllc.owd_p99_ms"), "urllc_p99_ref": "one-way p99, kpi_collector",
                    "urllc_loss_pct": 100 * (m.get("urllc.loss_ratio") or 0), "loss_ref": "URLLC one-way loss, kpi_collector",
                    "container_cpu": {k[4:]: v for k, v in m.items() if k.startswith("cpu.")},
                    "offered_mbps": {"eMBB": m.get("embb.dl_aggregate_mbps") or 0}}
            if mode:
                info["scheduler_mode"] = mode
            mc = re.search(r"cell(\d+)", mode_txt)
            if mc:
                info["cell_dl_mbps"] = float(mc.group(1))
            p99 = info["urllc_p99_ms"] or 0
            if mode == "fifo":
                exp, note = {"cell-congestion"}, "same-day comparison: a FIFO cell lost 1.3-2.9 % of URLLC (README)"
            elif p99 <= 10 and info["urllc_loss_pct"] == 0:
                exp, note = set(), "URLLC within budget, no loss"
            else:
                exp, note = None, "URLLC p99 above 10 ms; the repository leaves this cause open"
            src = "open5gs-kpi/%s/instances.jsonl #%d (%s)" % (run, i, r["label"])
            out.append(case("kpi-%s-%d" % (run, i), diagnose.Facts(info=info, source=src), exp, (), src, note=note))
    return out


# ─────────────────────────── incidents from logs and READMEs ───────────────────────────

def _log_facts(files, start, end, info=None):
    events = []
    for name, container, comp in files:
        lines = logparse.read_text(os.path.join(FIX, name)).splitlines()
        imsi = re.search(r"ue-(\d+)\.log$", name)
        for line in lines:
            e = logparse.parse_line(line, container=container, component=comp, year=2026,
                                    imsi=("imsi-" + imsi.group(1)) if imsi else None, date=(2026, 11, 8))
            if e:
                events.append(e)
    rep = correlate.correlate({}, events, [], start, end, baseline=(start - 3600, start))
    return diagnose.Facts.from_report(rep, info or {})


def _utc(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def log_cases():
    out = []
    f = _log_facts([("o5gs-smf.log", "o5gs-smf", None)], _utc("2026-11-08 09:09:00"), _utc("2026-11-08 09:40:00"))
    out.append(case("pfcp-urllc-upf-20261108", f, {"pfcp-association-loss"}, (),
                    "tests/fixtures/observability/o5gs-smf.log (live SMF log, 09:09-09:37)",
                    note="SMF lost the URLLC and mMTC UPFs' heartbeats; 'Remote PFCP restarted', restoration"))
    f = _log_facts([("o5gs-gnb-urllc-n2-refused.log", "o5gs-gnb-urllc", None)],
                   _utc("2026-09-14 09:23:00"), _utc("2026-09-14 09:25:20"),
                   {"gate_failed": ["gnb-connected", "ues-on-ran", "pdu-sessions-at-upf", "upf-sessions-per-slice",
                                    "every-slice-has-sessions", "user-plane-flows-per-slice", "ues-reachable",
                                    "urllc-latency-budget"], "expected_gnbs": 2, "gnb_down_slices": ["URLLC"]})
    f.metrics["gnbs"] = {"worst": 1.0, "window_min": 1.0, "window_median": 1.0}
    out.append(case("deploy-gnb-port-typo-20260914", f, {"gnb-n2-down"}, ("registration-failure",),
                    "open5gs-deploy/deploy-20260914-092251Z.txt", note="URLLC gNB pointed at AMF port 38413; gate 8 failed"))
    f = _log_facts([("o5gs-amf.log", "o5gs-amf", None), ("o5gs-upf-urllc.log", "o5gs-upf-urllc", None),
                    ("o5gs-gnb-urllc.log", "o5gs-gnb-urllc", None),
                    ("ue-208930000000001.log", "o5gs-ue", "ue")],
                   _utc("2026-11-08 14:17:00"), _utc("2026-11-08 14:20:00"))
    out.append(case("healthy-attach-20261108", f, set(), (),
                    "tests/fixtures/observability: AMF, URLLC UPF, URLLC gNB, one UE (live, a normal re-attach)",
                    note="UEs re-attaching with both sessions; one routine NAS MAC failure"))
    # Documented only as numbers: the facts are exactly those numbers.
    out.append(case("netorch-down-after-wsl-restart-20261108",
                    diagnose.Facts(info={"jobs_down": {"network-orchestrator": 0}}), {"target-down"}, (),
                    "deployments/open5gs/kpi-gates.json (no-scrape-failures 'why')", reconstructed=True,
                    note="a stopped network orchestrator failed the gate on a healthy stack"))
    out.append(case("stale-ue-logs-240-of-100",
                    diagnose.Facts(info={"ue_log_count": 240, "ue_truth": 100, "planned_ues": 100}),
                    {"observability-artifact"}, (), "deployments/open5gs/ran/ue-entrypoint.sh, scripts/open5gs/start_ues.sh comments",
                    reconstructed=True, note="logs of an earlier 240-UE plan counted as UEs"))
    t0 = _utc("2026-09-25 04:21:16")
    bursts = [(0, 6), (32, 3), (63, 3), (95, 1), (126, 1)]
    out.append(case("clock-steps-rlf-20260925",
                    diagnose.Facts(info={"rlf_count": sum(n for _, n in bursts), "rlf_times": [t0 + d for d, _ in bursts],
                                         "host_load1": 0.47, "ncpu": NCPU_LAPTOP, "ues_without_data_path": 14,
                                         "broken_ref": "6 of 20 UEs reachable after 2 minutes"}),
                    {"rlf-no-data-path"}, (), "docs/evidence/open5gs-clock/README.md", reconstructed=True,
                    note="RLF bursts ~31 s apart from wall-clock steps; host idle"))
    out.append(case("kpi-crash-fifo600-20261108",
                    diagnose.Facts(info={"scheduler_mode": "fifo", "cell_dl_mbps": 600.0, "rlf_count": 4,
                                         "ues_without_data_path": 5, "broken_ref": "5 of 130 sessions without data path"}),
                    {"rlf-no-data-path"}, ("cell-congestion",), "docs/evidence/open5gs-kpi/crashed-runs/README.md",
                    reconstructed=True, note="after the second FIFO run: 4 RLF lines, 5 sessions broken"))
    f = diagnose.Facts(info={"slice_dormant": ["mMTC"]})
    f.metrics["sessions{mMTC}"] = {"window_last": 11.0, "worst": 11.0}
    out.append(case("stale-core-sessions-dormant-mmtc", f, {"observability-artifact"}, (),
                    "docs/evidence/open5gs-network-orchestrator/README.md (stale sessions under heavy load)",
                    reconstructed=True, note="core kept 11 mMTC sessions while the slice was off"))
    return out


def all_cases():
    return scheduler_cases() + controller_cases() + kpi_cases() + log_cases()


# ─────────────────────────── scoring ───────────────────────────

def evaluate(cases):
    rows, tp, rep_n, exp_n, exp_hit, top_ok, top_n, healthy_ok, healthy_n = [], 0, 0, 0, 0, 0, 0, 0, 0
    unlabelled = []
    for c in cases:
        res = diagnose.diagnose(c["facts"])
        got = [r["id"] for r in res["causes"]]
        row = {"id": c["id"], "got": got, "expected": c["expected"], "acceptable": c["acceptable"],
               "reconstructed": c["reconstructed"], "note": c["note"], "source": c["source"],
               "scores": {r["id"]: r["score"] for r in res["causes"]}}
        if c["expected"] is None:
            unlabelled.append(row)
            continue
        ok_set = c["expected"] | c["acceptable"]
        row["false_positives"] = [g for g in got if g not in ok_set]
        row["missed"] = sorted(c["expected"] - set(got))
        tp += sum(1 for g in got if g in ok_set)
        rep_n += len(got)
        exp_n += len(c["expected"])
        exp_hit += len(c["expected"] & set(got))
        if c["expected"]:
            top_n += 1
            top_ok += 1 if got and got[0] in c["expected"] else 0
        else:
            healthy_n += 1
            serious = [r for r in res["causes"] if r["severity"] in ("critical", "warning")]
            healthy_ok += 0 if serious else 1
        row["correct"] = not row["false_positives"] and not row["missed"]
        rows.append(row)
    totals = {"cases": len(rows), "unlabelled": len(unlabelled), "precision": tp / float(rep_n) if rep_n else None,
              "recall": exp_hit / float(exp_n) if exp_n else None, "top1": top_ok / float(top_n) if top_n else None,
              "top1_n": top_n, "healthy_ok": healthy_ok, "healthy_n": healthy_n, "reported": rep_n, "true_positive": tp,
              "expected": exp_n, "expected_found": exp_hit, "exact": sum(r["correct"] for r in rows)}
    return rows, unlabelled, totals


def main():
    rows, unlabelled, t = evaluate(all_cases())
    print("%-46s %-3s %-44s %s" % ("case", "ok", "diagnosed (score)", "expected [+acceptable]"))
    for r in rows:
        got = ", ".join("%s %.2f" % (g, r["scores"][g]) for g in r["got"]) or "healthy"
        exp = ", ".join(sorted(r["expected"])) or "healthy"
        if r["acceptable"]:
            exp += " [+" + ", ".join(sorted(r["acceptable"])) + "]"
        print("%-46s %-3s %-44s %s%s" % (r["id"][:46], "yes" if r["correct"] else "NO", got[:44], exp,
                                          "  (reconstructed)" if r["reconstructed"] else ""))
    print()
    print("unlabelled (cause not settled by the evidence; shown, not scored):")
    for r in unlabelled:
        print("  %-44s %s" % (r["id"][:44], ", ".join("%s %.2f" % (g, r["scores"][g]) for g in r["got"]) or "healthy"))
    print()
    print("labelled cases %d (exactly right %d) · unlabelled %d" % (t["cases"], t["exact"], t["unlabelled"]))
    print("precision %.3f (%d of %d reported causes expected or acceptable)" % (t["precision"], t["true_positive"], t["reported"]))
    print("recall    %.3f (%d of %d expected causes found)" % (t["recall"], t["expected_found"], t["expected"]))
    print("top-1     %.3f (%d faulty cases)" % (t["top1"], t["top1_n"]))
    print("healthy   %d of %d healthy cases with no critical/warning cause" % (t["healthy_ok"], t["healthy_n"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
