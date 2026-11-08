#!/usr/bin/env python3
"""
diagnose.py — map correlated symptoms to likely causes, with the evidence and what to do (ADR-024).

Rules, not a model: each rule below encodes ONE failure this project has actually met on this stack,
reads only the facts it names, scores how strongly they are present (0..1), and cites them. Rules are
independent; a post-pass links consequences to causes (radio-link failures CAUSED BY a FIFO cell
dropping heartbeats; registration failures CAUSED BY a gNB without N2).

  cpu-saturation           a gNB/UPF or the whole host is out of CPU; URLLC's tail is CPU contention
  cell-congestion          the emulated cell's queues drop/back up (scheduler class drops; FIFO cell)
  rlf-no-data-path         radio-link failures leave UEs registered but without a data path
  pfcp-association-loss    the SMF lost N4 to a slice's UPF (heartbeat lost, restoration)
  gnb-n2-down              a slice's gNB has no N2 to the AMF: the whole slice has no radio access
  registration-failure     UEs fail to register (rejects, auth failures, NF de-registered at the NRF)
  orchestrator-throttling  the feedback controller cut a slice's admission limit; demands refused
  target-down              a scrape target is down (core exporter: critical; optional host process: advisory)
  observability-artifact   a measurement disagrees with ground truth (e.g. 240 UE logs for 100 UEs)

Facts come from a correlation report (correlate.py: Prometheus + logs + traces, live) or from an
evidence adapter (incidents.py: the incidents recorded under docs/evidence, offline).

    tools/observability/diagnose.py --last 15m                live: correlate, then diagnose
    tools/observability/diagnose.py --report corr.json        diagnose a saved correlation report
    tools/observability/diagnose.py --validate                the recorded incidents: precision/recall
    tools/observability/diagnose.py --serve                   :9115  /diagnosis /correlation /metrics

Standard library only.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import correlate  # noqa: E402
import logparse  # noqa: E402

REPORT_AT = 0.5                 # causes scoring at least this are reported
OPTIONAL_JOBS = ("slice-orchestrator", "network-orchestrator", "packet-scheduler", "fault-diagnosis")
START_SCRIPT = {"slice-orchestrator": "scripts/open5gs/start_orchestrator.sh",
                "network-orchestrator": "scripts/open5gs/start_netorch.sh",
                "packet-scheduler": "scripts/open5gs/start_scheduler.sh",
                "fault-diagnosis": "scripts/open5gs/start_diagnosis.sh"}


def _budgets():
    env = logparse._read_env(logparse.SLICES_ENV)
    out = {}
    for letter, name, *_ in logparse.SliceMap.DEFAULT:
        n = env.get("SLICE_%s_NAME" % letter, name)
        try:
            out[n] = float(env.get("SLICE_%s_MAX_LATENCY_MS" % letter, ""))
        except ValueError:
            pass
    return out or {"eMBB": 300.0, "URLLC": 10.0, "mMTC": 1000.0}


def _planned_ues():
    env = logparse._read_env(logparse.SLICES_ENV)
    return sum(int(p.partition("=")[2]) for p in env.get("O5GS_UES", "A=20 B=10 C=70").split()
               if p.partition("=")[2].isdigit())


BUDGET_MS = _budgets()
PLANNED_UES = _planned_ues()


# ─────────────────────────── facts ───────────────────────────


class Facts:
    """What the rules may read. metrics: {"signal{label}": summary} (correlate.summarize's shape);
    logs: {"event{slice}": {"window": n, "baseline": n, "first", "last", "samples"}}; traces: span
    records; info: point facts that are not time series (host load, scheduler mode, gate results…)."""

    def __init__(self, metrics=None, logs=None, traces=None, info=None, anomalies=None, source=""):
        self.metrics = metrics or {}
        self.logs = logs or {}
        self.traces = traces or []
        self.info = info or {}
        self.anomalies = anomalies or []
        self.source = source

    @classmethod
    def from_report(cls, rep, info=None):
        return cls(rep.get("series", {}), rep.get("logs", {}), rep.get("traces", []), info or {},
                   rep.get("anomalies", []), "correlation")

    def m(self, signal, label=None):
        if label is None:
            return {k: v for k, v in self.metrics.items() if k.split("{")[0] == signal}
        return self.metrics.get("%s{%s}" % (signal, label)) or (self.metrics.get(signal) if label == "" else None)

    def worst(self, signal, label=None):
        s = self.metrics.get(signal if label is None else "%s{%s}" % (signal, label))
        return None if s is None else s.get("worst")

    def z(self, signal, label=None):
        s = self.metrics.get(signal if label is None else "%s{%s}" % (signal, label))
        return None if s is None else s.get("z_level")

    def log(self, event, slice_=None):
        tot = 0
        for k, v in self.logs.items():
            ev, _, rest = k.partition("{")
            sl = rest.rstrip("}")
            if ev == event and (slice_ is None or sl == slice_):
                tot += v.get("window", 0)
        return tot

    def log_by_slice(self, event):
        out = {}
        for k, v in self.logs.items():
            ev, _, rest = k.partition("{")
            if ev == event and v.get("window"):
                out[rest.rstrip("}")] = out.get(rest.rstrip("}"), 0) + v["window"]
        return out

    def log_sample(self, event):
        for k, v in self.logs.items():
            if k.partition("{")[0] == event and v.get("samples"):
                return v["samples"][0]
        return None

    def trace_actions(self, prefix, action=None):
        out = []
        for t in self.traces:
            if not str(t.get("name", "")).startswith(prefix):
                continue
            if action and t.get("attrs", {}).get("action") != action:
                continue
            out.append(t)
        return out


def ev(source, ref, detail):
    return {"source": source, "ref": ref, "detail": detail}


def _fmt(v, nd=1):
    if v is None:
        return "?"
    if isinstance(v, float):
        return ("%." + str(nd) + "f") % v
    return str(v)


# ─────────────────────────── the rules ───────────────────────────


def latency_breaches(f):
    """[(slice, what, value_ms, budget)] — latency above the slice's budget, from any source."""
    out = []
    for sl, budget in BUDGET_MS.items():
        cands = []
        w = f.worst("rtt_max", sl)
        if w is not None:
            cands.append(("probe RTT worst", w, 'max(ue_probe_rtt_ms{slice="%s",stat="max"})' % sl))
        s = f.m("rtt_avg", sl)
        if s and s.get("window_median") is not None:
            cands.append(("probe RTT average", s["window_median"], 'ue_probe_rtt_ms{slice="%s",stat="avg"}' % sl))
        if sl == "URLLC":
            s = f.metrics.get("urllc_owd_p95")
            if s and s.get("window_median") is not None:
                cands.append(("one-way p95 (median over the window)", s["window_median"],
                              'kpi_owd_recent_ms{slice="URLLC",stat="p95"}'))
        for k in ("p99", "p95"):
            v = f.info.get("%s_%s_ms" % (sl.lower(), k))
            if v is not None:
                cands.append((k, v, f.info.get("%s_%s_ref" % (sl.lower(), k), "evidence")))
        for what, v, ref in cands:
            # The worst single probe of a window is noisy (the gate keeps URLLC's tail advisory for that
            # reason); a breach on it alone needs twice the budget.
            limit = budget * (2 if what == "probe RTT worst" else 1)
            if v is not None and v > limit:
                out.append((sl, what, v, budget, ref))
    return out


def rule_cpu_saturation(f):
    evidence, strength, direct = [], 0.0, False
    load, ncpu = f.info.get("host_load1"), f.info.get("ncpu")
    if load is not None and ncpu:
        ratio = load / float(ncpu)
        if ratio >= 0.75:
            strength = max(strength, min(0.95, 0.6 + 0.3 * (ratio - 0.75)))
            direct = True
            evidence.append(ev("snapshot", f.info.get("host_load_ref", "/proc/loadavg"),
                               "host load %.1f on %d CPUs (%.0f%% of the host)" % (load, ncpu, 100 * ratio)))
    for key, s in f.m("upf_cpu").items():
        if s.get("worst") is not None and s["worst"] >= 0.85:
            strength, direct = max(strength, 0.8), True
            evidence.append(ev("metric", 'rate(process_cpu_seconds_total{nf="upf"}[30s]) %s' % key,
                               "%s at %.2f cores: the Open5GS UPF forwards on one thread" % (key, s["worst"])))
    for c, pct in sorted((f.info.get("container_cpu") or {}).items()):
        quota = (f.info.get("cpu_quota") or {}).get(c)
        if c.startswith("upf-") and pct >= 85:
            strength, direct = max(strength, 0.8), True
            evidence.append(ev("snapshot", "docker stats %s" % c, "%s at %.0f%% CPU: one core, its forwarding thread" % (c, pct)))
        elif quota and pct >= 90 * quota:
            strength, direct = max(strength, 0.85), True
            evidence.append(ev("snapshot", "docker stats %s" % c, "%s at %.0f%% CPU against a quota of %g CPUs" % (c, pct, quota)))
    zr = f.z("probe_round_s")
    if zr is not None and zr >= correlate.Z_CUT:
        strength = max(strength, 0.6)
        evidence.append(ev("metric", "ue_probe_round_duration_seconds",
                           "UE probe rounds slowed (z %.1f): the UE container is starved" % zr))
    breaches = latency_breaches(f)
    short = f.info.get("embb_flows_met")
    symptom = [b for b in breaches if b[0] != "eMBB"] or ([("eMBB", "flows short", short, None, "flows")]
                                                          if short is not None and short < 0.9 else [])
    if not symptom:
        return None
    for sl, what, v, budget, ref in symptom[:3]:
        if budget:
            evidence.append(ev("metric" if ref.startswith(("max", "ue_", "kpi")) else "snapshot", ref,
                               "%s %s %.1f ms > budget %g ms" % (sl, what, v, budget)))
        else:
            evidence.append(ev("snapshot", ref, "only %.0f%% of eMBB flows got their rate" % (100 * v)))
    if not direct:
        # Inferred: the latency is not explained by the slice's own queue, and another slice is loaded.
        own_drops = sum((f.info.get("class_drops") or {}).get(sl, 0) for sl, *_ in symptom)
        own_loss = max([f.info.get("%s_loss_pct" % sl.lower(), 0) or 0 for sl, *_ in symptom] + [0])
        loaded = max([v for v in (f.info.get("offered_mbps") or {}).values()] +
                     [s.get("worst") or 0 for s in f.m("measured_mbps").values()] +
                     [s.get("worst") or 0 for s in f.m("dl_mbps").values()] + [0])
        mode = f.info.get("scheduler_mode")
        if own_drops == 0 and own_loss < 1 and loaded >= 100:
            strength = 0.55
            evidence.append(ev("snapshot", "offered load / scheduler classes",
                               "%.0f Mbps offered on another slice while the affected slice lost nothing in its own "
                               "queue%s: shared-CPU contention (ADR-016 §4), inferred - no CPU measurement in this "
                               "evidence" % (loaded, " (no cell: scheduler off)" if mode == "off" else "")))
        else:
            return None
    slices = sorted({b[0] for b in symptom})
    return {"id": "cpu-saturation", "title": "CPU saturation: latency is CPU contention, not queueing",
            "score": round(strength, 2), "severity": "critical" if "URLLC" in slices else "warning",
            "slices": slices, "evidence": evidence,
            "actions": ["Cap the load that costs the CPU: run the slice orchestrator with the feedback controller "
                        "(CONTROL=1, default policy pi) so eMBB's admission limit follows URLLC latency (ADR-016/018).",
                        "Give eMBB's gNB and UPF a CPU quota: tools/network-orchestrator/netorch.py scale eMBB 1 "
                        "(366 -> 121 Mbps, URLLC p99 11.0 -> 10.4 ms measured, ADR-017).",
                        "Check what is burning CPU: docker stats --no-stream; never generate test traffic inside a UPF "
                        "(ADR-012).",
                        "A queue cannot fix this (ADR-019 finding 5): changing scheduler shares will not help."]}


def rule_cell_congestion(f):
    evidence, score = [], 0.0
    mode = f.info.get("scheduler_mode")
    if mode is None:
        s = f.metrics.get("sched_fifo")
        if s and s.get("worst") == 1:
            mode = "fifo"
    drops = dict(f.info.get("class_drops") or {})
    drop_unit = "packets"
    for key, s in f.m("sched_drops").items():
        sl = key[key.find("{") + 1:-1]
        if (s.get("worst") or 0) >= 0.5:            # a rate: at least one packet every two seconds
            drops[sl] = max(drops.get(sl, 0), s["worst"])
            drop_unit = "packets/s at worst"
    if mode is None and drops.get("all"):
        mode = "fifo"                               # the FIFO cell is one class labelled "all"
    backlog = {key[key.find("{") + 1:-1]: s.get("worst") for key, s in f.m("sched_backlog").items()
               if (s.get("z_level") or 0) >= correlate.Z_CUT}
    cell = f.info.get("cell_dl_mbps")
    offered = sum((f.info.get("offered_mbps") or {}).values()) if f.info.get("offered_mbps") else None
    over = cell is not None and offered is not None and mode != "off" and offered > cell
    losses = {sl: f.info.get("%s_loss_pct" % sl.lower()) for sl in BUDGET_MS
              if (f.info.get("%s_loss_pct" % sl.lower()) or 0) > 0}
    for key, s in f.m("kpi_loss").items():
        if (s.get("worst") or 0) > 0.001:
            losses[key[key.find("{") + 1:-1]] = 100 * s["worst"]
    if not drops and not backlog and not over and not (mode == "fifo" and losses):
        return None
    if over:
        evidence.append(ev("snapshot", "offered load vs cell", "%.0f Mbps offered into a %.0f Mbps cell" % (offered, cell)))
    for sl, n in sorted(drops.items()):
        if n:
            evidence.append(ev("metric" if f.m("sched_drops") else "snapshot",
                               'packet_scheduler_class_drops_total{slice="%s"}' % sl, "%s class dropped %s %s" % (sl, _fmt(n, 0), drop_unit)))
    for sl, b in sorted(backlog.items()):
        evidence.append(ev("metric", 'packet_scheduler_class_backlog_bytes{slice="%s"}' % sl, "%s backlog %s bytes" % (sl, _fmt(b, 0))))
    for sl, p in sorted(losses.items()):
        evidence.append(ev("snapshot" if f.info.get("%s_loss_pct" % sl.lower()) else "metric",
                           f.info.get("loss_ref", "kpi/probe loss"), "%s lost %.2f%%" % (sl, p)))
    signalling = drops.get("signalling", 0)
    victims = [sl for sl in losses if sl not in (f.info.get("offered_mbps") or {})]
    if signalling:
        score, severity = 0.95, "critical"
    elif mode == "fifo" and (over or drops or losses):
        score, severity = 0.9 if (victims or len(losses) > 1) else 0.75, "critical"
    else:
        score, severity = 0.65 if (drops or over) else 0.55, "warning"
    if mode:
        evidence.append(ev("snapshot", "packet_scheduler_info", "scheduler mode %s" % mode))
    acts = []
    if mode == "fifo":
        acts.append("The cell is one FIFO queue: switch the scheduler to sliced "
                    "(curl -XPOST localhost:9114/config -d '{\"mode\":\"sliced\"}'); a FIFO cell under overload made "
                    "93-99 of 100 UEs lose their radio link (ADR-019).")
    if signalling:
        acts.append("Signalling is being dropped: raise SCHED_SIGNALLING_SHARE; radio links are at risk.")
    acts += ["Reduce the offered load on the congested slice: admission control in the slice orchestrator, or a lower "
             "eMBB admission limit.",
             "If the guaranteed shares are wrong for the demand, change them at run time "
             "(POST :9114/config {\"shares\":{...}}), or raise SCHED_CELL_DL_MBPS if the cell is set below what the host carries."]
    sl = sorted(set(drops) | set(backlog) | set(losses) | set(f.info.get("offered_mbps") or {}))
    return {"id": "cell-congestion", "title": "Cell congestion: the shared cell's queues are dropping or backing up",
            "score": score, "severity": severity, "slices": sl, "evidence": evidence, "actions": acts}


def _periodic(times, tol=0.25):
    """Median gap when bursts recur at a near-constant period (>= 3 gaps), else None."""
    ts = sorted(times)
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 2]
    if len(gaps) < 3:
        return None
    med = correlate.median(gaps)
    return med if all(abs(g - med) <= tol * med for g in gaps) else None


def rule_rlf_no_data_path(f):
    evidence = []
    rlf = f.log("radio_link_failure") + (f.info.get("rlf_count") or 0)
    restarts = f.log("ue_restart")
    not_found = f.log("pdu_session_not_found")
    broken = f.info.get("ues_without_data_path") or 0
    reach_drop = {}
    for key, s in f.m("reachable").items():
        if (s.get("z_level") or 0) <= -correlate.Z_CUT:
            reach_drop[key[key.find("{") + 1:-1]] = (s.get("baseline_median"), s.get("window_min"))
    sessions_held = all((s.get("z_level") or 0) > -correlate.Z_CUT for s in f.m("sessions").values())
    if not (rlf or restarts or not_found or broken or reach_drop):
        return None
    if rlf:
        evidence.append(ev("log", "UE logs: 'Radio link failure detected'", "%d radio-link failure(s)" % rlf))
    if not_found:
        evidence.append(ev("log", "gNB: 'Uplink data failure, PDU session not found'", "%d uplink packets with no session at the gNB" % not_found))
    if restarts:
        evidence.append(ev("log", "ue-entrypoint: unhealthy ... restarting this UE only",
                           "%d UE restart(s) by the supervisor, per slice %s" % (restarts, f.log_by_slice("ue_restart"))))
    if broken:
        evidence.append(ev("snapshot", f.info.get("broken_ref", "UE data-path check"), "%d UEs left without a data path" % broken))
    for sl, (b, w) in sorted(reach_drop.items()):
        evidence.append(ev("metric", 'ue_probe_reachable{slice="%s"}' % sl, "%s reachable UEs %s -> %s" % (sl, _fmt(b, 0), _fmt(w, 0))))
    if reach_drop and sessions_held and f.m("sessions"):
        evidence.append(ev("metric", "fivegs_smffunction_sm_sessionnbr", "the core still counts every session: the loss is below the core"))
    affected = max(rlf, broken, restarts, sum(b - w for b, w in reach_drop.values() if b is not None and w is not None))
    score = 0.55 + 0.4 * min(1.0, affected / max(1.0, 0.1 * PLANNED_UES))
    if not (rlf or broken or not_found) and not reach_drop:
        score = min(score, 0.6)          # supervisor restarts alone: a few UEs, routinely recovered
        if restarts < 3:
            return None
    acts = ["Each affected UE is restarted by the UE supervisor within ~30 s (ran/ue-entrypoint.sh); if many are "
            "left broken, re-attach: scripts/open5gs/start_ues.sh."]
    period = _periodic(f.info.get("rlf_times") or [])
    hint = None
    if period:
        hint = "failures recur every %.0f s: look for wall-clock steps (docs/evidence/open5gs-clock)" % period
        evidence.append(ev("log", "radio_link_failure timestamps", hint))
    acts.append("Find what stopped the gNB heartbeats (2 s, UERANSIM): a FIFO cell dropping signalling, a starved UE "
                "container, or wall-clock steps; cell-congestion and cpu-saturation findings say which.")
    return {"id": "rlf-no-data-path", "title": "Radio-link failures: UEs registered but without a data path",
            "score": round(min(score, 0.95), 2), "severity": "critical", "evidence": evidence, "actions": acts,
            "slices": sorted(set(f.log_by_slice("ue_restart")) | set(reach_drop) | set(f.info.get("rlf_slices") or [])),
            "hint": hint}


def rule_pfcp_loss(f):
    evidence, score = [], 0.0
    expected = f.info.get("expected_upfs", 3)
    peers = f.worst("pfcp_peers")
    if peers is not None and peers < expected:
        score = 0.85
        evidence.append(ev("metric", 'sum(pfcp_peers_active{nf="smf"})', "SMF has %s of %d UPF associations" % (_fmt(peers, 0), expected)))
    slices = set()
    for evname, weight, what in (("pfcp_heartbeat_lost", 0.85, "heartbeat lost"), ("pfcp_deassociated", 0.7, "de-associated"),
                                 ("pfcp_restoration", 0.7, "restoration"), ("pfcp_no_response", 0.6, "requests unanswered"),
                                 ("pfcp_retry_failed", 0.6, "re-association failed"), ("pfcp_node_unknown", 0.75, "unknown PFCP node")):
        n = f.log(evname)
        if n:
            score = max(score, weight)
            by = f.log_by_slice(evname)
            slices |= {s for s in by if s != "-"}
            evidence.append(ev("log", evname, "%s x%d %s; e.g. %s" % (what, n, by, (f.log_sample(evname) or "")[:120])))
    for key, s in f.m("upf_sessions").items():
        if (s.get("z_level") or 0) <= -correlate.Z_CUT:
            score = max(score, 0.6)
            slices.add(key[key.find("{") + 1:-1])
            evidence.append(ev("metric", "fivegs_upffunction_upf_sessionnbr %s" % key,
                               "sessions at the UPF %s -> %s" % (_fmt(s.get("baseline_median"), 0), _fmt(s.get("window_min"), 0))))
    if score < REPORT_AT:
        return None
    if f.log("pfcp_heartbeat_lost") and f.log("pfcp_restoration"):
        score = max(score, 0.9)
    return {"id": "pfcp-association-loss", "title": "N4/PFCP association between the SMF and a slice's UPF was lost",
            "score": score, "severity": "critical", "slices": sorted(slices), "evidence": evidence,
            "actions": ["Check the slice's UPF container: docker ps --filter name=o5gs-upf; a restart shows as "
                        "'Remote PFCP restarted' at the SMF.",
                        "After a UPF restart confirm sessions were restored: sum by (snssai)(fivegs_smffunction_sm_sessionnbr) "
                        "and the slice's ue_probe_reachable.",
                        "If the SMF was restarted, stop the UPFs first, restart the SMF, then start the UPFs - a UPF older "
                        "than the SMF keeps stale PFCP state (docs/open5gs.md operating notes; deploy.sh does this)."]}


def rule_gnb_n2_down(f):
    evidence, score, slices = [], 0.0, set()
    expected = f.info.get("expected_gnbs", 3)
    g = f.worst("gnbs")
    if g is not None and g < expected:
        score = 0.75
        evidence.append(ev("metric", "sum(gnb)", "%s of %d gNBs connected over N2" % (_fmt(g, 0), expected)))
    for evname, what in (("n2_connect_failed", "SCTP to the AMF refused"), ("ng_setup_failed", "NG Setup failed"),
                         ("gnb_removed", "AMF removed a gNB"), ("n2_association_shutdown", "SCTP association shut down")):
        n = f.log(evname)
        if n:
            score = max(score, 0.9 if evname in ("n2_connect_failed", "ng_setup_failed") else 0.7)
            by = f.log_by_slice(evname)
            slices |= {s for s in by if s != "-"}
            evidence.append(ev("log", evname, "%s x%d %s; e.g. %s" % (what, n, by, (f.log_sample(evname) or "")[:120])))
    for gid in f.info.get("gate_failed", []):
        if "gnb" in gid:
            score = max(score, 0.8)
            evidence.append(ev("gate", gid, "KPI gate failed: %s" % gid))
    if score < REPORT_AT:
        return None
    return {"id": "gnb-n2-down", "title": "A slice's gNB has no N2 connection to the AMF",
            "score": score, "severity": "critical", "slices": sorted(slices) or f.info.get("gnb_down_slices", []),
            "evidence": evidence,
            "actions": ["Check the gNB's AMF address and port (ran/gnb-<slice>.yaml, amfConfigs: 10.53.0.5:38412).",
                        "The gNBs are supervised and retry (ran/gnb-entrypoint.sh): docker logs o5gs-gnb-<slice> shows each attempt.",
                        "If this came with a deploy, scripts/open5gs/deploy.sh rolls back on the gate failure."]}


def rule_registration_failure(f):
    evidence, score = [], 0.0
    rf = f.worst("reg_fail")
    if rf:
        score = max(score, 0.7)
        evidence.append(ev("metric", "increase(fivegs_amffunction_rm_reginitfail[1m])", "%s registration failures/min" % _fmt(rf, 0)))
    af = f.worst("auth_fail")
    if af:
        score = max(score, 0.6)
        evidence.append(ev("metric", "increase(fivegs_amffunction_amf_authfail[1m])", "%s authentication failures/min" % _fmt(af, 0)))
    for evname, weight, what in (("registration_reject", 0.8, "registration rejected"), ("auth_failure", 0.55, "authentication/NAS security failure"),
                                 ("nf_deregistered", 0.6, "NF de-registered at the NRF"), ("access_barred", 0.5, "UE access attempts barred"),
                                 ("cell_selection_failure", 0.5, "UE found no cell")):
        n = f.log(evname)
        if not n:
            continue
        if evname in ("auth_failure", "access_barred", "cell_selection_failure") and n < 3:
            # One NAS MAC failure, or one barred attempt that the UE retries (T3580), is routine on a
            # re-attach: both were seen on the healthy stack (case healthy-attach-20261108).
            continue
        if evname == "nf_deregistered" and f.log("nf_registered") >= n:
            continue        # de-registered and registered again: a restart, not an outage
        score = max(score, weight)
        evidence.append(ev("log", evname, "%s x%d %s" % (what, n, f.log_by_slice(evname))))
    ru = f.m("ran_ue", "") or f.metrics.get("ran_ue")
    if ru and (ru.get("z_level") or 0) <= -correlate.Z_CUT:
        score = max(score, 0.6)
        evidence.append(ev("metric", "sum(ran_ue)", "UEs on the RAN %s -> %s" % (_fmt(ru.get("baseline_median"), 0), _fmt(ru.get("window_min"), 0))))
    for gid in f.info.get("gate_failed", []):
        if gid in ("ues-on-ran",) or gid.startswith("sessions-") or gid.startswith("ues-reachable"):
            score = max(score, 0.6)
            evidence.append(ev("gate", gid, "KPI gate failed: %s" % gid))
    if score < REPORT_AT:
        return None
    return {"id": "registration-failure", "title": "UEs fail to register or attach",
            "score": score, "severity": "critical", "slices": sorted(set(f.log_by_slice("registration_reject")) - {"-"}),
            "evidence": evidence,
            "actions": ["Look at the AMF's reasons: docker logs o5gs-amf | grep -E 'reject|failure'.",
                        "Check every NF is registered at the NRF (docker logs o5gs-nrf | grep 'NF registered').",
                        "Expected for a few minutes after a Docker engine restart; UEs recover on retry (gate advisory)."]}


def rule_orchestrator_throttling(f):
    cuts = {}
    for t in f.trace_actions("controller.", "cut"):
        sl = t.get("attrs", {}).get("slice")
        cuts.setdefault(sl, []).append(t)
    evidence, slices, score = [], [], 0.0
    capacity = f.info.get("capacity_mbps") or {}
    for key, s in f.m("admission_limit").items():
        sl = key[key.find("{") + 1:-1]
        cap = capacity.get(sl)
        low = s.get("window_min")
        if low is not None and cap and low < 0.6 * cap:
            cuts.setdefault(sl, [])
            evidence.append(ev("metric", 'slice_admission_limit_mbps{slice="%s"}' % sl,
                               "%s admission limit down to %.0f of %g Mbps" % (sl, low, cap)))
    for sl, items in sorted(cuts.items(), key=lambda kv: str(kv[0])):
        if items:
            first = items[0].get("attrs", {})
            evidence.append(ev("trace", "controller.cut", "%d cut(s) of %s's limit, first %s -> %s (%s)" % (
                len(items), sl, first.get("from"), first.get("to"), first.get("why"))))
        slices.append(sl)
    if not slices:
        return None
    score = 0.6
    refused = {k[k.find("{") + 1:-1]: s.get("window_max") for k, s in f.m("refused_capacity").items() if (s.get("window_max") or 0) > 0}
    refused.update(f.info.get("refused") or {})
    for sl, n in sorted(refused.items()):
        if sl in slices and n:
            score = max(score, 0.75)
            evidence.append(ev("metric", 'slice_rejected_total{reason="capacity",slice="%s"}' % sl, "%s demands refused: %s" % (sl, _fmt(n, 0))))
    protected = f.info.get("protected_slice", "URLLC")
    self_throttle = protected in slices
    acts = ["Intended behaviour when it is eMBB: the controller trades eMBB admissions for URLLC latency "
            "(ADR-016/018). Refusals and a low limit are its cost, not a fault in the network."]
    if self_throttle:
        score = max(score, 0.8)
        evidence.append(ev("trace", "controller.cut slice=%s" % protected, "the controller is cutting %s, the slice it protects" % protected))
        acts.insert(0, "The controller is throttling %s itself (CONTROL_THROTTLE includes it). Measured: %s's own flows do "
                       "not inflate its latency; cutting its limit left 18%% of its flows short for nothing "
                       "(evidence controller-throttling-urllc-too). Set CONTROL_THROTTLE=eMBB." % (protected, protected))
    met = f.info.get("flows_met") or {}
    for sl in slices:
        if sl in met and met[sl] < 0.95:
            evidence.append(ev("snapshot", "flows", "only %.0f%% of %s flows got their rate" % (100 * met[sl], sl)))
    return {"id": "orchestrator-throttling", "title": "The slice orchestrator's controller is throttling a slice",
            "score": round(score, 2), "severity": "warning" if self_throttle else "info", "slices": sorted(map(str, slices)),
            "evidence": evidence, "actions": acts}


def rule_target_down(f):
    down = dict(f.info.get("jobs_down") or {})
    for key, s in f.m("target_up").items():
        if s.get("window_min") == 0:
            down[key[key.find("{") + 1:-1]] = s.get("window_last")
    if not down:
        return None
    core = [j for j in down if j not in OPTIONAL_JOBS]
    evidence = [ev("metric", 'up{job="%s"}' % j, "%s %s" % (j, "is down" if not v else "was down during the window"))
                for j, v in sorted(down.items())]
    acts = []
    for j in sorted(down):
        if j in START_SCRIPT:
            acts.append("%s is a host process: start it with %s. It does not come back by itself after a WSL or Docker "
                        "engine restart (network orchestrator, 2026-11-08)." % (j, START_SCRIPT[j]))
        else:
            acts.append("%s is part of the network's observability: docker ps / docker logs for its container; the gate "
                        "fails while it is down." % j)
    return {"id": "target-down", "title": "A scrape target is down",
            "score": 0.95 if core else 0.8, "severity": "critical" if core else "advisory",
            "slices": [], "evidence": evidence, "actions": acts, "jobs": sorted(down)}


def rule_observability_artifact(f):
    evidence = []
    n_logs, truth = f.info.get("ue_log_count"), f.info.get("ue_truth") or f.worst("ran_ue")
    planned = f.info.get("planned_ues", PLANNED_UES)
    if n_logs is not None and n_logs > max(truth or 0, planned):
        evidence.append(ev("snapshot", "ls /tmp/ue-*.log in o5gs-ue", "%d UE log files for %d planned UEs (%s on the RAN)" % (n_logs, planned, _fmt(truth, 0))))
    for key, s in f.m("sessions").items():
        if f.info.get("slice_dormant") and key[key.find("{") + 1:-1] in f.info["slice_dormant"] and (s.get("window_last") or 0) > 0:
            evidence.append(ev("metric", "fivegs_smffunction_sm_sessionnbr %s" % key,
                               "the core counts %s sessions on a dormant slice: stale contexts (ADR-017 finding 4)" % _fmt(s["window_last"], 0)))
    if not evidence:
        return None
    return {"id": "observability-artifact", "title": "A measurement disagrees with ground truth",
            "score": 0.8, "severity": "warning", "slices": [], "evidence": evidence,
            "actions": ["Count only what belongs to the current plan: logs of planned IMSIs, lines --since the container's "
                        "start (docs/open5gs.md operating notes; tests/acceptance-open5gs.sh scopes by plan).",
                        "Trust the gauges audited against ground truth (docs/open5gs.md 'Which metrics can be trusted')."]}


RULES = [rule_cpu_saturation, rule_cell_congestion, rule_rlf_no_data_path, rule_pfcp_loss, rule_gnb_n2_down,
         rule_registration_failure, rule_orchestrator_throttling, rule_target_down, rule_observability_artifact]

# (consequence, cause): when both are found, the consequence is explained by the cause.
CAUSAL = [("rlf-no-data-path", "cell-congestion"), ("rlf-no-data-path", "cpu-saturation"),
          ("registration-failure", "gnb-n2-down"), ("registration-failure", "pfcp-association-loss"),
          ("orchestrator-throttling", "cpu-saturation"), ("rlf-no-data-path", "pfcp-association-loss")]


def diagnose(f, report_at=REPORT_AT):
    found = []
    for rule in RULES:
        try:
            r = rule(f)
        except Exception as e:          # a rule bug must not hide the other findings
            r = {"id": rule.__name__, "title": "rule failed", "score": 0, "error": repr(e), "evidence": [], "actions": []}
        if r and r.get("score", 0) >= report_at:
            found.append(r)
    ids = {r["id"] for r in found}
    for r in found:
        r["explained_by"] = [c for cons, c in CAUSAL if cons == r["id"] and c in ids]
    # Root causes first: a finding explained by another one ranks below it.
    found.sort(key=lambda r: (bool(r["explained_by"]), -r["score"]))
    unverified = f.info.get("prometheus_error")
    # No metrics is not "no fault": the verdict is then unverified, as an unreachable Prometheus makes
    # the KPI gate exit 2 rather than pass.
    healthy = None if unverified else not any(r["severity"] in ("critical", "warning") for r in found)
    return {"healthy": healthy, "verified": not unverified, "unverified_reason": unverified,
            "causes": found, "facts_source": f.source}


def render(result):
    lines = []
    if not result["causes"]:
        return "no fault found (every rule below %.2f)" % REPORT_AT
    for i, c in enumerate(result["causes"], 1):
        lines.append("%d. [%s %.2f] %s%s%s" % (i, c["severity"], c["score"], c["title"],
                                                 (" — slices " + ", ".join(c["slices"])) if c.get("slices") else "",
                                                 (" — explained by " + ", ".join(c["explained_by"])) if c.get("explained_by") else ""))
        for e in c["evidence"]:
            lines.append("     evidence (%s) %s: %s" % (e["source"], e["ref"], e["detail"]))
        for a in c["actions"]:
            lines.append("     -> %s" % a)
    return "\n".join(lines)


# ─────────────────────────── live facts beyond Prometheus ───────────────────────────


def host_info(use_docker=False):
    info = {"ncpu": os.cpu_count(), "planned_ues": PLANNED_UES}
    try:
        info["host_load1"] = float(logparse.read_text("/proc/loadavg").split()[0])
        info["host_load_ref"] = "/proc/loadavg"
    except (OSError, ValueError, IndexError):
        pass
    if use_docker:
        import subprocess
        try:
            out = subprocess.run([logparse.DOCKER, "exec", "o5gs-ue", "sh", "-c", "ls /tmp/ue-[0-9]*.log | wc -l"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            info["ue_log_count"] = int(out)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    env = logparse._read_env(logparse.SLICES_ENV)
    info["capacity_mbps"] = {}
    for letter, name, *_ in logparse.SliceMap.DEFAULT:
        try:
            info["capacity_mbps"][env.get("SLICE_%s_NAME" % letter, name)] = float(env.get("SLICE_%s_CAPACITY_MBPS" % letter))
        except (TypeError, ValueError):
            pass
    return info


def live(start, end, prometheus=correlate.PROMETHEUS, loki=correlate.LOKI, use_docker=True, trigger=None):
    rep = correlate.run(start, end, None, trigger, prometheus, loki, use_docker)
    info = host_info(use_docker)
    info["prometheus_error"] = rep.get("sources", {}).get("query_errors", {}).get("prometheus")
    return diagnose(Facts.from_report(rep, info)), rep


# ─────────────────────────── service: /diagnosis /metrics ───────────────────────────


class Service:
    def __init__(self, interval, window, prometheus, loki, use_docker):
        self.interval, self.window = interval, window
        self.prometheus, self.loki, self.use_docker = prometheus, loki, use_docker
        self.lock = threading.Lock()
        self.result, self.report = None, None
        self.runs = self.errors = 0
        self.last_run = self.last_duration = 0.0
        self.last_error = ""

    def run_once(self):
        t0 = time.time()
        try:
            result, rep = live(t0 - self.window, t0, self.prometheus, self.loki, self.use_docker)
            with self.lock:
                self.result, self.report = result, rep
                self.runs += 1
                self.last_error = ""
        except Exception as e:
            with self.lock:
                self.errors += 1
                self.last_error = repr(e)[:300]
        with self.lock:
            self.last_run, self.last_duration = time.time(), time.time() - t0

    def loop(self):
        while True:
            self.run_once()
            time.sleep(self.interval)

    def metrics(self):
        with self.lock:
            res, rep = self.result, self.report
            lines = ["# HELP fault_diagnosis_up The fault-diagnosis service is running.",
                     "# TYPE fault_diagnosis_up gauge", "fault_diagnosis_up 1",
                     "# HELP fault_diagnosis_runs_total Diagnosis runs completed.",
                     "# TYPE fault_diagnosis_runs_total counter", "fault_diagnosis_runs_total %d" % self.runs,
                     "# HELP fault_diagnosis_run_errors_total Diagnosis runs that failed.",
                     "# TYPE fault_diagnosis_run_errors_total counter", "fault_diagnosis_run_errors_total %d" % self.errors,
                     "# TYPE fault_diagnosis_last_run_timestamp_seconds gauge",
                     "fault_diagnosis_last_run_timestamp_seconds %.3f" % self.last_run,
                     "# TYPE fault_diagnosis_last_run_duration_seconds gauge",
                     "fault_diagnosis_last_run_duration_seconds %.3f" % self.last_duration,
                     "# HELP fault_diagnosis_cause_score Score (0-1) of each cause found in the last run; absent when not found.",
                     "# TYPE fault_diagnosis_cause_score gauge"]
            causes = (res or {}).get("causes", [])
            for c in causes:
                for sl in (c.get("slices") or ["all"]):
                    lines.append('fault_diagnosis_cause_score{cause="%s",severity="%s",slice="%s"} %.2f'
                                 % (c["id"], c["severity"], sl, c["score"]))
            lines += ["# HELP fault_diagnosis_causes Causes found in the last run.", "# TYPE fault_diagnosis_causes gauge",
                      "fault_diagnosis_causes %d" % len(causes),
                      "# HELP fault_diagnosis_healthy 1 when the last run found no critical or warning cause.",
                      "# TYPE fault_diagnosis_healthy gauge",
                      "fault_diagnosis_healthy %d" % (1 if res and res.get("healthy") else 0),
                      "# HELP fault_diagnosis_verified 1 when the last run could read Prometheus.",
                      "# TYPE fault_diagnosis_verified gauge",
                      "fault_diagnosis_verified %d" % (1 if res and res.get("verified") else 0),
                      "# HELP fault_diagnosis_anomalies Anomalies in the last correlation, by kind.",
                      "# TYPE fault_diagnosis_anomalies gauge"]
            kinds = {}
            for a in (rep or {}).get("anomalies", []):
                kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
            for k in ("metric", "log"):
                lines.append('fault_diagnosis_anomalies{kind="%s"} %d' % (k, kinds.get(k, 0)))
        return "\n".join(lines) + "\n"


def serve(port, svc):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/metrics":
                self._send(200, svc.metrics(), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/diagnosis":
                with svc.lock:
                    body = {"result": svc.result, "last_run": svc.last_run, "last_error": svc.last_error,
                            "window_s": svc.window}
                self._send(200, json.dumps(body, indent=2, default=str))
            elif path == "/correlation":
                with svc.lock:
                    self._send(200, json.dumps(svc.report, indent=2, default=str))
            elif path == "/":
                self._send(200, json.dumps({"service": "fault-diagnosis", "endpoints": ["/diagnosis", "/correlation", "/metrics"],
                                            "interval_s": svc.interval, "window_s": svc.window}))
            else:
                self._send(404, '{"error":"not found"}')

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--last", type=correlate._parse_dur, default=None, help="live: diagnose the last N (e.g. 15m)")
    ap.add_argument("--report", help="diagnose a saved correlation report (correlate.py --json)")
    ap.add_argument("--validate", action="store_true", help="run every recorded incident; print precision/recall")
    ap.add_argument("--serve", action="store_true", help="HTTP service with /diagnosis and /metrics")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "9115")))
    ap.add_argument("--interval", type=float, default=float(os.environ.get("DIAG_INTERVAL", "120")))
    ap.add_argument("--window", type=correlate._parse_dur, default=None)
    ap.add_argument("--prometheus", default=correlate.PROMETHEUS)
    ap.add_argument("--loki", default=correlate.LOKI)
    ap.add_argument("--no-docker", action="store_true")
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args(argv)
    if a.validate:
        import incidents
        return incidents.main()
    if a.serve:
        svc = Service(a.interval, a.window or int(os.environ.get("DIAG_WINDOW", "600")), a.prometheus, a.loki,
                      not a.no_docker and os.environ.get("DIAG_DOCKER", "1") == "1")
        threading.Thread(target=svc.loop, daemon=True).start()
        print("fault-diagnosis on :%d — every %gs over the last %ds; prometheus %s, loki %s, docker logs %s"
              % (a.port, svc.interval, svc.window, a.prometheus, a.loki, svc.use_docker), flush=True)
        serve(a.port, svc)
        return 0
    if a.report:
        rep = logparse.read_json(a.report)
        info = host_info(False)
        info["prometheus_error"] = rep.get("sources", {}).get("query_errors", {}).get("prometheus")
        result = diagnose(Facts.from_report(rep, info))
    else:
        end = time.time()
        result, rep = live(end - (a.last or 900), end, a.prometheus, a.loki, not a.no_docker)
        print(correlate.render(rep))
        print()
    print(render(result))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
    if not result["verified"]:
        print("UNVERIFIED: %s" % result["unverified_reason"])
        return 2
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
