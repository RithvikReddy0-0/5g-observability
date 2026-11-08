#!/usr/bin/env python3
"""
correlate.py — line up metrics, logs and decision traces around a KPI breach (ADR-024).

Given a time window, or a trigger (a KPI breach such as URLLC p95 above its budget, a failed KPI
gate, a burst of radio-link failures), it pulls aligned evidence from three places:

  metrics   Prometheus query_range for a fixed catalogue of per-slice signals (SIGNALS below)
  logs      Loki when it is running, otherwise `docker logs` and the UEs' own log files, parsed by
            logparse.py with the same rules Alloy uses
  traces    the JSON-lines decision events of the orchestrators, scheduler and KPI runner (trace.py),
            and the network orchestrator's operation log

and produces a timeline and a ranked list of co-occurring anomalies.

HOW AN ANOMALY IS SCORED (deliberately simple and explainable)
  * Each series is compared with a BASELINE window just before the incident (default: as long as
    the incident window, ending where it starts). The baseline's spread is its median absolute
    deviation; the score is the robust ("modified") z-score of the incident window's level and of its
    worst value, (x - median) / (1.4826 * MAD). |z| >= 3.5 is the cut-off Iglewicz and Hoaglin
    recommend for the modified z-score (NIST/SEMATECH e-Handbook of Statistical Methods, §1.3.5.17).
  * When the series changed, WHEN it changed is the CUSUM change point: the k maximising
    |sum_{i<=k}(x_i - mean)| over baseline + window (Page, "Continuous inspection schemes",
    Biometrika 41(1/2):100-115, 1954, doi:10.1093/biomet/41.1-2.100).
  * Lead/lag: the Pearson correlation of the trigger series with each anomalous series shifted by
    -L..+L steps; a positive lag means the candidate moved FIRST, which is what a cause does.
  * Log events: counts per (event, slice) in the window against the baseline's rate; a fault event
    (logparse.FAULT_EVENTS) that is absent from the baseline is always reported.
A flat baseline (MAD = 0, e.g. a session count) gets a floor on its spread so that one session
lost is not an infinite z-score; the floor is per signal (SIGNALS[...]["floor"]).

    tools/observability/correlate.py --last 15m                          everything, last 15 minutes
    tools/observability/correlate.py --trigger urllc_owd_p95 --last 15m  ranked against URLLC latency
    tools/observability/correlate.py --gate verdict.json --last 10m      after a failed KPI gate
    tools/observability/correlate.py --series DIR --start T --end T      offline, recorded series

Standard library only. Read-only towards the stack: GET queries to Prometheus and Loki, `docker logs`.
"""

import argparse
import glob
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import logparse  # noqa: E402
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("obs_trace", os.path.join(HERE, "trace.py"))
obs_trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(obs_trace)

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://localhost:9091")
LOKI = os.environ.get("LOKI_URL", "http://localhost:3100")
TRACE_FILE = os.environ.get("OBS_TRACE_FILE", "/tmp/o5gs_traces.jsonl")
NETORCH_LOG = os.environ.get("NETORCH_LOG", "/tmp/o5gs_netorch_ops.jsonl")
Z_CUT = 3.5

SNSSAI = {"1-010203": "eMBB", "2-112233": "URLLC", "3-334455": "mMTC"}

# name -> PromQL, the label that names the series' slice/target, the direction that is BAD,
# a floor on the baseline spread (in the signal's unit), and the unit.
SIGNALS = {
    "urllc_owd_p95": dict(expr='max(kpi_owd_recent_ms{slice="URLLC",stat="p95"})', bad="up", floor=1, unit="ms"),
    "rtt_avg": dict(expr='max by (slice) (ue_probe_rtt_ms{stat="avg"})', by="slice", bad="up", floor=1.0, unit="ms"),
    "rtt_max": dict(expr='max by (slice) (ue_probe_rtt_ms{stat="max"})', by="slice", bad="up", floor=2.0, unit="ms"),
    "reachable": dict(expr="sum by (slice) (ue_probe_reachable)", by="slice", bad="down", floor=1.0, unit="UEs"),
    "probe_round_s": dict(expr="max(ue_probe_round_duration_seconds)", bad="up", floor=0.1, unit="s"),
    "upf_cpu": dict(expr='sum by (slice) (rate(process_cpu_seconds_total{nf="upf"}[30s]))', by="slice",
                    bad="up", floor=0.05, unit="cores"),
    "core_cpu": dict(expr='sum by (nf) (rate(process_cpu_seconds_total{job="open5gs",nf!="upf"}[30s]))', by="nf",
                     bad="up", floor=0.05, unit="cores"),
    "dl_mbps": dict(expr="sum by (slice) (rate(upf_slice_downlink_bytes_total[30s])) * 8 / 1e6", by="slice",
                    bad="any", floor=1.0, unit="Mbps"),
    "upf_drops": dict(expr="sum by (slice) (rate(upf_slice_dropped_packets_total[30s]))", by="slice",
                      bad="up", floor=1.0, unit="pps"),
    "sched_drops": dict(expr='sum by (slice) (rate(packet_scheduler_class_drops_total{slice!="cell",tier!="all"}[30s]))',
                        by="slice", bad="up", floor=1.0, unit="pps"),
    "sched_backlog": dict(expr='max by (slice) (packet_scheduler_class_backlog_bytes{slice!="cell",tier!="all"})',
                          by="slice", bad="up", floor=3000.0, unit="bytes"),
    "sched_fifo": dict(expr='max(packet_scheduler_info{mode="fifo"})', bad="up", floor=0.5, unit=""),
    "sched_applies": dict(expr="sum(increase(packet_scheduler_applies_total[1m]))", bad="up", floor=0.5, unit="/min"),
    "sessions": dict(expr="sum by (snssai) (fivegs_smffunction_sm_sessionnbr)", by="snssai", bad="down",
                     floor=1.0, unit="sessions"),
    "upf_sessions": dict(expr="sum by (slice) (fivegs_upffunction_upf_sessionnbr)", by="slice", bad="down",
                         floor=1.0, unit="sessions"),
    "pfcp_peers": dict(expr='sum(pfcp_peers_active{nf="smf"})', bad="down", floor=0.5, unit="peers"),
    "gnbs": dict(expr="sum(gnb)", bad="down", floor=0.5, unit="gNBs"),
    "ran_ue": dict(expr="sum(ran_ue)", bad="down", floor=1.0, unit="UEs"),
    "reg_fail": dict(expr="sum(increase(fivegs_amffunction_rm_reginitfail[1m]))", bad="up", floor=0.5, unit="/min"),
    "auth_fail": dict(expr="sum(increase(fivegs_amffunction_amf_authfail[1m]))", bad="up", floor=0.5, unit="/min"),
    "target_up": dict(expr="min by (job) (up)", by="job", bad="down", floor=0.5, unit=""),
    "admission_limit": dict(expr="max by (slice) (slice_admission_limit_mbps)", by="slice", bad="down",
                            floor=1.0, unit="Mbps"),
    "measured_mbps": dict(expr="max by (slice) (slice_measured_mbps)", by="slice", bad="any", floor=1.0, unit="Mbps"),
    "refused_capacity": dict(expr='sum by (slice) (increase(slice_rejected_total{reason="capacity"}[1m]))',
                             by="slice", bad="up", floor=0.5, unit="/min"),
    "control_cuts": dict(expr='sum by (slice) (increase(slice_control_actions_total{action="cut"}[1m]))',
                         by="slice", bad="up", floor=0.5, unit="/min"),
    "kpi_loss": dict(expr=("1 - sum by (slice) (increase(kpi_packets_received_total[1m])) "
                           "/ sum by (slice) (increase(kpi_packets_expected_total[1m]))"),
                     by="slice", bad="up", floor=0.001, unit="ratio"),
    "netorch_active": dict(expr="max by (slice) (netorch_slice_active)", by="slice", bad="down", floor=0.5, unit=""),
}


# ─────────────────────────── statistics ───────────────────────────


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def mad(xs):
    m = median(xs)
    return median([abs(x - m) for x in xs]) if xs else None


def robust_z(x, base, floor):
    """Modified z-score of x against the baseline sample: (x - median) / (1.4826 * MAD), with the
    spread floored so a flat baseline does not make every change infinite."""
    m = median(base)
    spread = max(1.4826 * (mad(base) or 0.0), floor)
    return (x - m) / spread


def cusum_change_point(values):
    """Index of the CUSUM change point (the first sample AFTER the change) and the mean shift."""
    n = len(values)
    if n < 4:
        return None, 0.0
    mean = sum(values) / n
    s, best, k = 0.0, 0.0, None
    for i, v in enumerate(values[:-1]):
        s += v - mean
        if abs(s) > best:
            best, k = abs(s), i
    if k is None:
        return None, 0.0
    before, after = values[:k + 1], values[k + 1:]
    return k + 1, sum(after) / len(after) - sum(before) / len(before)


def resample(points, grid):
    """Step-hold resampling of [(t, v)] onto grid times (last value at or before t)."""
    out, j, last = [], 0, None
    pts = sorted(points)
    for t in grid:
        while j < len(pts) and pts[j][0] <= t:
            last = pts[j][1]
            j += 1
        out.append(last)
    return out


def pearson(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 4:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in pairs) / (sx * sy)


def lead_lag(trigger, candidate, start, end, step, max_lag):
    """Best (lag_seconds, r) of corr(trigger(t), candidate(t - lag)); lag > 0: candidate leads."""
    grid = [start + i * step for i in range(int((end - start) / step) + 1)]
    tr = resample(trigger, grid)
    best = (0, None)
    for lag_steps in range(-max_lag, max_lag + 1):
        shifted = resample(candidate, [t - lag_steps * step for t in grid])
        r = pearson(tr, shifted)
        if r is not None and (best[1] is None or abs(r) > abs(best[1]) + 1e-9):
            best = (lag_steps * step, r)
    return best


# ─────────────────────────── sources ───────────────────────────


def prom_range(base, expr, start, end, step, timeout=15):
    q = urllib.parse.urlencode({"query": expr, "start": "%.3f" % start, "end": "%.3f" % end, "step": "%g" % step})
    with urllib.request.urlopen(base.rstrip("/") + "/api/v1/query_range?" + q, timeout=timeout) as r:
        doc = json.loads(r.read().decode())
    if doc.get("status") != "success":
        raise RuntimeError(doc.get("error", "query rejected"))
    return doc["data"]["result"]


def series_key(name, metric, by):
    if not by:
        return name
    v = metric.get(by, "")
    if by == "snssai":
        v = SNSSAI.get(v, v)
    return "%s{%s}" % (name, v)


def matrix_to_series(name, result, by=None):
    """Prometheus matrix result -> {key: [(t, v)]}."""
    out = {}
    for s in result:
        pts = []
        for t, v in s.get("values", []):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(fv) or math.isinf(fv):
                continue
            pts.append((float(t), fv))
        if pts:
            out[series_key(name, s.get("metric", {}), by)] = pts
    return out


def fetch_signals(base, start, end, step=15, names=None):
    """Every catalogue signal over [start, end]; a signal whose query fails is reported, not fatal."""
    series, errors = {}, {}
    for name, sig in SIGNALS.items():
        if names and name not in names:
            continue
        try:
            series.update(matrix_to_series(name, prom_range(base, sig["expr"], start, end, step), sig.get("by")))
        except urllib.error.HTTPError as e:      # this query was rejected: the others may still work
            errors[name] = str(e)[:200]
        except (urllib.error.URLError, OSError) as e:
            errors["prometheus"] = "unreachable: %s" % str(e)[:200]     # no point asking 26 more times
            break
        except Exception as e:
            errors[name] = str(e)[:200]
    return series, errors


# Recorded evidence stores raw metric names; these map them onto the catalogue's signals.
METRIC_TO_SIGNAL = {"slice_admission_limit_mbps": ("admission_limit", "slice"),
                    "slice_measured_mbps": ("measured_mbps", "slice"),
                    "ue_probe_reachable": ("reachable", "slice"),
                    "fivegs_smffunction_sm_sessionnbr": ("sessions", "snssai")}


def load_series_file(path, name=None, by=None):
    """A recorded query_range JSON (as stored under docs/evidence) -> {key: [(t, v)]}; {} if the file
    is not a Prometheus matrix (evidence folders also hold flow lists and control logs)."""
    doc = logparse.read_json(path)
    if not isinstance(doc, dict) or doc.get("data", {}).get("resultType") != "matrix":
        return {}
    out = {}
    for s in doc["data"]["result"]:
        m = s.get("metric", {})
        mname = m.get("__name__", "")
        if mname == "kpi_owd_recent_ms" and m.get("slice") == "URLLC" and m.get("stat") == "p95":
            sig, sby = "urllc_owd_p95", None
        elif mname in METRIC_TO_SIGNAL:
            sig, sby = METRIC_TO_SIGNAL[mname]
        else:
            sig = name or os.path.splitext(os.path.basename(path))[0]
            sby = by or ("slice" if "slice" in m and len(doc["data"]["result"]) > 1 else None)
        out.update(matrix_to_series(sig, [s], sby))
    return out


def gather_logs(start, end, loki=LOKI, use_docker=True):
    """Parsed log events in [start, end]: Loki if it answers, else docker (read-only)."""
    if loki and logparse.loki_ready(loki):
        try:
            return logparse.read_loki(loki, start, end), "loki"
        except Exception:
            pass
    if use_docker:
        return logparse.read_all_docker(start, end), "docker"
    return [], "none"


def gather_traces(start, end, trace_file=TRACE_FILE, netorch_log=NETORCH_LOG):
    spans = obs_trace.read_spans(trace_file, start, end) if trace_file else []
    if netorch_log and not any(s.get("service") == "network-orchestrator" for s in spans):
        spans += obs_trace.read_netorch_ops(netorch_log, start, end)
    spans.sort(key=lambda s: s.get("start", 0))
    return spans


# ─────────────────────────── analysis ───────────────────────────


def _signal_of(key):
    return key.split("{")[0]


def _label_of(key):
    m = re.search(r"\{(.*)\}$", key)
    return m.group(1) if m else None


def summarize(points, b0, b1, w0, w1, floor, bad):
    base = [v for t, v in points if b0 <= t < b1]
    win = [v for t, v in points if w0 <= t <= w1]
    if not win:
        return None
    s = {"window_median": median(win), "window_max": max(win), "window_min": min(win), "window_last": win[-1],
         "n_window": len(win), "n_baseline": len(base)}
    if bad == "down":
        s["worst"] = s["window_min"]
    elif bad == "up":
        s["worst"] = s["window_max"]
    else:
        s["worst"] = s["window_max"] if abs(s["window_max"] - (median(base) if base else 0)) >= \
            abs(s["window_min"] - (median(base) if base else 0)) else s["window_min"]
    if len(base) >= 3:
        s["baseline_median"] = median(base)
        s["z_level"] = robust_z(s["window_median"], base, floor)
        s["z_worst"] = robust_z(s["worst"], base, floor)
    else:
        s["baseline_median"] = median(base) if base else None
        s["z_level"] = s["z_worst"] = None
    seq = [(t, v) for t, v in points if b0 <= t <= w1]
    k, shift = cusum_change_point([v for _, v in seq])
    if k is not None and k < len(seq):
        s["change_at"], s["change_shift"] = seq[k][0], shift
    return s


def is_anomalous(s, bad):
    zl, zw = s.get("z_level"), s.get("z_worst")
    if zl is None:
        return False, 0.0
    sign = {"up": 1, "down": -1}.get(bad)
    cand = [zl, zw * 0.7]          # a single worst sample is noisier than the window's level
    if sign:
        score = max(sign * c for c in cand)
    else:
        score = max(abs(c) for c in cand)
    return score >= Z_CUT, score


def correlate(series, events, spans, start, end, baseline=None, trigger=None, step=15, max_lag_s=60):
    """The core analysis, pure (no I/O): series {key: [(t, v)]}, parsed log events, trace spans.

    trigger: a series key (or signal name) to rank the others against, or None.
    Returns the report dict (see the module docstring)."""
    b0, b1 = baseline or (start - (end - start), start)
    report = {"window": {"start": start, "end": end}, "baseline": {"start": b0, "end": b1},
              "trigger": trigger, "series": {}, "anomalies": [], "timeline": [], "logs": {}, "traces": []}
    trig_key = None
    if trigger:
        trig_key = trigger if trigger in series else next((k for k in series if _signal_of(k) == trigger), None)
        report["trigger_key"] = trig_key
    for key, pts in sorted(series.items()):
        sig = SIGNALS.get(_signal_of(key), {"bad": "any", "floor": 1.0, "unit": ""})
        s = summarize(pts, b0, b1, start, end, sig["floor"], sig["bad"])
        if s is None:
            continue
        s["unit"] = sig.get("unit", "")
        report["series"][key] = s
        anomalous, score = is_anomalous(s, sig["bad"])
        if not anomalous or key == trig_key:
            continue
        a = {"kind": "metric", "key": key, "signal": _signal_of(key), "label": _label_of(key),
             "z": round(score, 2), "baseline": s["baseline_median"], "window": s["window_median"],
             "worst": s["worst"], "unit": s["unit"], "change_at": s.get("change_at")}
        if trig_key:
            lag, r = lead_lag(series[trig_key], pts, b0, end, step, max(1, int(max_lag_s / step)))
            a["lag_s"], a["corr"] = lag, (round(r, 3) if r is not None else None)
        report["anomalies"].append(a)
        if a["change_at"] and start - (end - start) <= a["change_at"] <= end:
            report["timeline"].append({"t": a["change_at"], "source": "metric",
                                       "what": "%s changed: %s -> %s %s (z %.1f)" % (
                                           key, _fmt(s["baseline_median"]), _fmt(s["window_median"]), s["unit"], score)})
    # logs: counts per (event, slice), window against baseline rate
    win_len, base_len = max(1.0, end - start), max(1.0, b1 - b0)
    counts = {}
    for e in events:
        if not e.get("event") or e.get("ts") is None:
            continue
        k = (e["event"], e.get("slice") or "-")
        c = counts.setdefault(k, {"window": 0, "baseline": 0, "first": None, "last": None, "samples": []})
        if start <= e["ts"] <= end:
            c["window"] += 1
            c["first"] = e["ts"] if c["first"] is None else min(c["first"], e["ts"])
            c["last"] = e["ts"] if c["last"] is None else max(c["last"], e["ts"])
            if len(c["samples"]) < 3:
                c["samples"].append(e["msg"][:200])
        elif b0 <= e["ts"] < b1:
            c["baseline"] += 1
    for (ev, sl), c in sorted(counts.items()):
        report["logs"]["%s{%s}" % (ev, sl)] = c
        if not c["window"]:
            continue
        expected = c["baseline"] * win_len / base_len
        z = (c["window"] - expected) / math.sqrt(expected + 1.0)
        fault = ev in logparse.FAULT_EVENTS
        if (fault and c["baseline"] == 0) or (c["window"] >= 3 and z >= Z_CUT):
            report["anomalies"].append({"kind": "log", "key": "%s{%s}" % (ev, sl), "event": ev,
                                        "label": None if sl == "-" else sl, "count": c["window"],
                                        "baseline_count": c["baseline"], "z": round(max(z, Z_CUT if fault else z), 2),
                                        "first": c["first"], "last": c["last"], "samples": c["samples"]})
            report["timeline"].append({"t": c["first"], "source": "log",
                                       "what": "%s x%d on %s (first: %s)" % (ev, c["window"], sl, c["samples"][0])})
    # traces: every decision in the window is evidence, not an anomaly score
    for sp in spans:
        if sp.get("end", 0) < start or sp.get("start", 0) > end:
            continue
        rec = {"t": sp["start"], "service": sp.get("service"), "name": sp.get("name"),
               "attrs": sp.get("attrs", {}), "status": sp.get("status"), "duration_ms": sp.get("duration_ms")}
        report["traces"].append(rec)
        if sp.get("parent_id") is None or sp.get("status") == "error":
            report["timeline"].append({"t": sp["start"], "source": "trace", "what": "%s %s %s" % (
                sp.get("service"), sp.get("name"), _short(sp.get("attrs", {})))})
    # rank: magnitude, then whether it led the trigger, then how early it changed
    for a in report["anomalies"]:
        lead = 1.0
        if a.get("corr") is not None:
            lead += abs(a["corr"]) * (1.0 if (a.get("lag_s") or 0) >= 0 else 0.5)
        a["rank_score"] = round(min(a["z"], 50.0) * lead, 2)
    report["anomalies"].sort(key=lambda a: -a["rank_score"])
    report["timeline"].sort(key=lambda x: x["t"] or 0)
    return report


def _fmt(v):
    if v is None:
        return "?"
    return ("%.3g" % v) if isinstance(v, float) else str(v)


def _short(attrs):
    keep = ["slice", "action", "from", "to", "admitted", "result", "step", "mode", "reason", "why", "traffic_class"]
    return " ".join("%s=%s" % (k, attrs[k]) for k in keep if k in attrs and attrs[k] not in (None, ""))[:160]


def gate_trigger(verdict_path):
    """Signals worth ranking against, from a failed KPI gate verdict (kpi_gate.py --json)."""
    doc = logparse.read_json(verdict_path)
    failed = [r for r in doc.get("results", []) if r.get("status") in ("FAIL", "WARN")]
    hint = {"urllc": "urllc_owd_p95", "reachable": "reachable", "pfcp": "pfcp_peers", "gnb": "gnbs",
            "ues-on-ran": "ran_ue", "sessions": "sessions", "scrape": "target_up", "exporters": "target_up",
            "registration": "reg_fail", "signalling": "sched_drops"}
    for r in failed:
        for k, sig in hint.items():
            if k in r["id"]:
                return sig, failed
    return None, failed


def render(report, top=15):
    lines = []
    w = report["window"]
    lines.append("correlation window %s .. %s (baseline %s .. %s)" % (
        _ts(w["start"]), _ts(w["end"]), _ts(report["baseline"]["start"]), _ts(report["baseline"]["end"])))
    if report.get("trigger"):
        lines.append("trigger: %s" % (report.get("trigger_key") or report["trigger"]))
    lines.append("")
    lines.append("ranked anomalies:")
    if not report["anomalies"]:
        lines.append("  none above |z| %.1f" % Z_CUT)
    for a in report["anomalies"][:top]:
        if a["kind"] == "metric":
            extra = ""
            if a.get("corr") is not None:
                extra = "  corr %+.2f at lag %+ds" % (a["corr"], a["lag_s"])
            lines.append("  %6.1f  metric %-28s %s -> %s (worst %s) %s z %.1f%s" % (
                a["rank_score"], a["key"], _fmt(a["baseline"]), _fmt(a["window"]), _fmt(a["worst"]), a["unit"],
                a["z"], extra))
        else:
            lines.append("  %6.1f  log    %-28s x%d (baseline %d)  e.g. %s" % (
                a["rank_score"], a["key"], a["count"], a["baseline_count"], (a["samples"] or [""])[0][:80]))
    lines.append("")
    lines.append("timeline:")
    for e in report["timeline"][:60]:
        lines.append("  %s  %-6s %s" % (_ts(e["t"]), e["source"], e["what"][:150]))
    return "\n".join(lines)


def _ts(t):
    return time.strftime("%H:%M:%S", time.gmtime(t)) + "Z" if t else "?"


def _parse_dur(s):
    m = re.fullmatch(r"(\d+)([smh]?)", s)
    if not m:
        raise argparse.ArgumentTypeError("duration like 90s, 15m, 1h")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def run(start, end, baseline=None, trigger=None, prometheus=PROMETHEUS, loki=LOKI, use_docker=True,
        trace_file=TRACE_FILE, netorch_log=NETORCH_LOG, step=15):
    """Live correlation: fetch everything for [baseline start, end] and analyse."""
    b0 = (baseline or (start - (end - start), start))[0]
    series, errors = fetch_signals(prometheus, b0, end, step)
    events, log_source = gather_logs(b0, end, loki, use_docker)
    spans = gather_traces(b0, end, trace_file, netorch_log)
    rep = correlate(series, events, spans, start, end, baseline, trigger, step)
    rep["sources"] = {"prometheus": prometheus, "query_errors": errors, "logs": log_source,
                      "log_events": len(events), "trace_spans": len(spans)}
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--last", type=_parse_dur, help="window: the last N seconds/minutes (e.g. 15m)")
    ap.add_argument("--start", type=float, help="window start, Unix seconds")
    ap.add_argument("--end", type=float, help="window end, Unix seconds")
    ap.add_argument("--baseline", type=_parse_dur, help="baseline length before the window (default: window length)")
    ap.add_argument("--trigger", help="signal to rank against, e.g. urllc_owd_p95 or 'reachable{URLLC}'")
    ap.add_argument("--gate", help="a kpi_gate.py --json verdict: rank against what failed")
    ap.add_argument("--series", help="offline: a directory, or a glob, of recorded query_range JSON files")
    ap.add_argument("--prometheus", default=PROMETHEUS)
    ap.add_argument("--loki", default=LOKI)
    ap.add_argument("--no-docker", action="store_true", help="never read docker logs")
    ap.add_argument("--json", dest="json_out", help="write the report as JSON")
    a = ap.parse_args(argv)
    end = a.end or time.time()
    start = a.start or (end - (a.last or 900))
    baseline = (start - a.baseline, start) if a.baseline else None
    trigger = a.trigger
    if a.gate:
        trigger, failed = gate_trigger(a.gate)
        print("gate: %d failed/advisory KPI(s): %s" % (len(failed), ", ".join(r["id"] for r in failed)))
    if a.series:
        series = {}
        pattern = os.path.join(a.series, "*.json") if os.path.isdir(a.series) else a.series
        for f in sorted(glob.glob(pattern)):
            try:
                series.update(load_series_file(f))
            except (ValueError, KeyError):
                continue
        rep = correlate(series, [], [], start, end, baseline, trigger)
    else:
        rep = run(start, end, baseline, trigger, a.prometheus, a.loki, not a.no_docker)
    print(render(rep))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
