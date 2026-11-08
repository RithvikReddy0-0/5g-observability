#!/usr/bin/env python3
"""
stub_open5gs.py — a fake Prometheus holding the series of a healthy 3-slice Open5GS stack.

The free5GC stub (stub_prometheus.py) answers by looking the gate's expression up in a table.
That proves the gate's decision logic, but a table keyed by the gate's own expressions can only
confirm that the gate agrees with itself (see check_metric_names.py). This stub instead stores
SERIES — metric names and labels as the real stack exports them — and evaluates each gate
expression against them. A gate with a wrong label value, job name or metric name gets no data
here exactly as it would on the live stack.

Where the series come from
    targets and target labels   deployments/open5gs/observability/prometheus.yml
    AMF / SMF / UPF / PCF       Open5GS metrics.c names; label sets as recorded in
                                docs/evidence/open5gs-20260914-060008Z/05-metrics-audit.txt
    upf_slice_*                 deployments/open5gs/observability/upf_slice_exporter.py
    ue_probe_*                  deployments/open5gs/observability/ue_probe_exporter.py
    packet_scheduler_*          tools/packet-scheduler/scheduler.py, classes as recorded in
                                docs/evidence/open5gs-kpi/20261108-130550Z/scheduler-state.json
    session counts              docs/evidence/open5gs-multi-session/20261108-092615Z/1-sessions.txt
                                (SMF: 1-010203 30, 2-112233 30, 3-334455 70)
    probe RTTs                  docs/evidence/open5gs-k8s/k8s-20260925-053701Z-gate.json

The healthy stack: 100 UEs (eMBB 20, URLLC 10, mMTC 70) on 3 gNBs, 130 PDU sessions (eMBB 30,
URLLC 30, mMTC 70), 3 UPFs each associated with the SMF over PFCP, every target up, no signalling
dropped by the cell scheduler. SCENARIOS lists faults applied to it and the verdict the gate must
reach on each; tools/kpi-gate/test_open5gs_gate.py runs them.

The PromQL evaluator understands only what the gate file uses: selectors with = != =~ !~
matchers, sum/count/max/min/avg without grouping, a comparison with a number, `or`,
vector(N), and max/min/avg_over_time and increase over a range. Anything else is answered with
the error a real Prometheus returns for a bad query, which the gate reports as a FAIL — so a new
gate shape cannot silently pass here.
"""

import json
import math
import re
from http.server import BaseHTTPRequestHandler

STEP = 5            # scrape_interval in deployments/open5gs/observability/prometheus.yml
HISTORY = 600       # seconds of samples held; the longest gate window is 5m
LOOKBACK = 300      # Prometheus' default staleness lookback for instant selectors

PLMN = "20893"
# name, S-NSSAI, sst, sd, DNN, TUN device, UPF N4 address, devices, PDU sessions
SLICES = [
    ("eMBB",  "1-010203", "1", "010203", "internet", "ogstun",  "10.53.0.7", 20, 30),
    ("URLLC", "2-112233", "2", "112233", "urllc",    "ogstun2", "10.53.0.8", 10, 30),
    ("mMTC",  "3-334455", "3", "334455", "iot",      "ogstun3", "10.53.0.9", 70, 70),
]
# Probe round-trip times (ms) per slice: (min, avg, max). From a recorded 3-slice gate run.
RTT = {"eMBB": (1.9, 6.2, 24.8), "URLLC": (2.1, 3.97, 15.2), "mMTC": (2.4, 5.1, 11.5)}
# Cell scheduler classes in sliced mode (classid, slice, tier).
SCHED_CLASSES = [("1:1", "cell", ""), ("1:10", "signalling", ""), ("1:99", "other", ""),
                 ("1:20", "eMBB", ""), ("1:30", "URLLC", "all"), ("1:31", "URLLC", "arp1"),
                 ("1:32", "URLLC", "arp2"), ("1:40", "mMTC", "")]


def const(v):
    return lambda age: v


def counter(rate, base=1000.0):
    """A counter that has grown by `rate` per second; `age` is seconds before now."""
    return lambda age: base + rate * (HISTORY - age)


def wobble(lo, hi):
    """A gauge cycling deterministically between lo and hi, one sample per step."""
    return lambda age: lo + (hi - lo) * ((age // STEP) % 4) / 3.0


class Target:
    def __init__(self, job, instance, labels=None):
        self.job, self.instance, self.labels = job, instance, dict(labels or {})
        self.up = True
        self.series = []                     # (name, labels, fn(age) -> value)

    def add(self, name, fn, **labels):
        self.series.append((name, labels, fn))

    def drop(self, name, **match):
        self.series = [s for s in self.series
                       if not (s[0] == name and all(s[1].get(k) == v for k, v in match.items()))]

    def set(self, name, fn, **match):
        for i, (n, lab, _) in enumerate(self.series):
            if n == name and all(lab.get(k) == v for k, v in match.items()):
                self.series[i] = (n, lab, fn)


def healthy():
    """Targets of a healthy stack, keyed by a short name the scenarios refer to."""
    t = {}

    amf = t["amf"] = Target("open5gs", "10.53.0.5:9090", {"nf": "amf"})
    amf.add("gnb", const(3))
    amf.add("ran_ue", const(100))
    amf.add("fivegs_amffunction_rm_reginitreq", counter(0, 100))
    amf.add("fivegs_amffunction_rm_reginitsucc", counter(0, 100))
    # fivegs_amffunction_rm_reginitfail{cause} has no series until the first failure.

    smf = t["smf"] = Target("open5gs", "10.53.0.4:9090", {"nf": "smf"})
    smf.add("pfcp_peers_active", const(3))
    smf.add("ues_active", const(100))
    smf.add("pfcp_sessions_active", const(130))
    smf.add("fivegs_smffunction_sm_pdusessioncreationreq", counter(0, 130), plmnid="", snssai="")
    for name, snssai, *_rest, sessions in SLICES:
        smf.add("fivegs_smffunction_sm_sessionnbr", const(sessions), plmnid=PLMN, snssai=snssai)
        smf.add("fivegs_smffunction_sm_pdusessioncreationreq", counter(0, sessions),
                plmnid=PLMN, snssai=snssai)

    pcf = t["pcf"] = Target("open5gs", "10.53.0.13:9090", {"nf": "pcf"})
    for name, snssai, *_rest, sessions in SLICES:
        pcf.add("fivegs_pcffunction_pa_sessionnbr", const(sessions), plmnid=PLMN, snssai=snssai)

    for name, snssai, sst, sd, dnn, dev, addr, devices, sessions in SLICES:
        upf = t["upf-" + name] = Target("open5gs", addr + ":9090", {"nf": "upf", "slice": name})
        upf.add("pfcp_peers_active", const(1))
        upf.add("fivegs_upffunction_upf_sessionnbr", const(sessions))
        upf.add("fivegs_upffunction_upf_qosflows", const(sessions), dnn=dnn)
        upf.add("fivegs_ep_n3_gtp_indatavolumeqosleveln3upf", counter(2e5), qfi="1")

        tr = t["traffic-" + name] = Target("upf-slice-traffic", addr + ":9120",
                                           {"component": "upf-slice-exporter"})
        lab = dict(sst=sst, sd=sd, slice=name, device=dev)
        tr.add("upf_slice_up", const(1), **lab)
        tr.add("upf_slice_uplink_packets_total", counter(devices * 0.2 + 5), **lab)
        tr.add("upf_slice_downlink_packets_total", counter(devices * 0.2 + 5), **lab)
        tr.add("upf_slice_uplink_bytes_total", counter(devices * 20.0 + 500), **lab)
        tr.add("upf_slice_downlink_bytes_total", counter(devices * 20.0 + 500), **lab)
        tr.add("upf_slice_dropped_packets_total", const(0), **lab)

    probe = t["ue-probe"] = Target("ue-probe", "10.53.0.31:9121",
                                   {"component": "ue-probe-exporter"})
    for name, *_rest, devices, _sessions in SLICES:
        probe.add("ue_probe_interfaces", const(devices), slice=name)
        probe.add("ue_probe_reachable", const(devices), slice=name)
        lo, avg, hi = RTT[name]
        probe.add("ue_probe_rtt_ms", const(lo), slice=name, stat="min")
        probe.add("ue_probe_rtt_ms", wobble(avg * 0.85, avg), slice=name, stat="avg")
        probe.add("ue_probe_rtt_ms", wobble(hi * 0.6, hi), slice=name, stat="max")

    for i, inst in enumerate(("10.53.0.52:9130", "10.53.0.53:9130")):
        t["kpi-collector-%d" % i] = Target("kpi-traffic", inst, {"component": "kpi-collector"})
    t["traffic-agent"] = Target("kpi-traffic", "10.53.0.31:9122", {"component": "traffic-agent"})
    t["slice-orchestrator"] = Target("slice-orchestrator", "10.53.0.1:9111",
                                     {"component": "slice-orchestrator"})
    t["network-orchestrator"] = Target("network-orchestrator", "10.53.0.1:9113",
                                       {"component": "network-orchestrator"})

    sched = t["packet-scheduler"] = Target("packet-scheduler", "10.53.0.1:9114",
                                           {"component": "packet-scheduler"})
    sched.add("packet_scheduler_info", const(1), mode="sliced")
    for direction in ("downlink", "uplink"):
        for classid, sl, tier in SCHED_CLASSES:
            lab = dict(direction=direction, classid=classid, slice=sl, tier=tier)
            sched.add("packet_scheduler_class_packets_total", counter(50), **lab)
            # eMBB saturating its share drops packets: normal, and must not fail the gate.
            drops = counter(40, 5000) if sl == "eMBB" else const(0)
            sched.add("packet_scheduler_class_drops_total", drops, **lab)

    # ADR-024 fault-diagnosis service (prometheus.yml job fault-diagnosis); optional, advisory only.
    t["fault-diagnosis"] = Target("fault-diagnosis", "10.53.0.1:9115", {"component": "fault-diagnosis"})

    t["prometheus"] = Target("prometheus", "localhost:9090")
    return t


# --------------------------------------------------------------------------- faults

def pcf_down(t):
    """The PCF container is stopped. No user-facing KPI notices; only the scrape does."""
    t["pcf"].up = False


def upf_down(t):
    """The mMTC UPF container is stopped: its own exporter and the slice-traffic exporter that
    runs inside it vanish, the SMF loses one PFCP peer and no mMTC UE answers its probe.
    The SMF's session gauge is deliberately left unchanged — the gate must catch this from the
    user plane even if the control plane has not noticed."""
    t["upf-mMTC"].up = False
    t["traffic-mMTC"].up = False
    t["smf"].set("pfcp_peers_active", const(2))
    t["ue-probe"].set("ue_probe_reachable", const(0), slice="mMTC")
    t["ue-probe"].drop("ue_probe_rtt_ms", slice="mMTC")   # emitted only for answered probes


def gnb_down(t):
    """The eMBB gNB is stopped: N2 is lost and its 20 UEs drop off the RAN."""
    t["amf"].set("gnb", const(2))
    t["amf"].set("ran_ue", const(80))
    t["ue-probe"].set("ue_probe_reachable", const(0), slice="eMBB")
    t["ue-probe"].drop("ue_probe_rtt_ms", slice="eMBB")


def slice_series_missing(t):
    """The SMF stops reporting mMTC altogether: no series, not a zero. Everything else intact,
    so only the per-slice session gates stand between this and a green verdict."""
    t["smf"].drop("fivegs_smffunction_sm_sessionnbr", snssai="3-334455")


def sessions_lost(t):
    """One eMBB device loses both its PDU sessions (home on eMBB, second on URLLC). It stays
    registered, so the AMF still counts it; SMF and UPF count one session fewer per slice, and
    its home interface stops answering probes."""
    t["smf"].set("fivegs_smffunction_sm_sessionnbr", const(29), snssai="1-010203")
    t["smf"].set("fivegs_smffunction_sm_sessionnbr", const(29), snssai="2-112233")
    for sl in ("eMBB", "URLLC"):
        t["upf-" + sl].set("fivegs_upffunction_upf_sessionnbr", const(29))
    t["ue-probe"].set("ue_probe_reachable", const(19), slice="eMBB")


def ue_unreachable(t):
    """One URLLC UE's user plane is broken while every core gauge still counts it."""
    t["ue-probe"].set("ue_probe_reachable", const(9), slice="URLLC")


def latency_breach(t):
    """Every slice over its round-trip budget for the whole window."""
    t["ue-probe"].set("ue_probe_rtt_ms", const(14.0), slice="URLLC", stat="avg")
    t["ue-probe"].set("ue_probe_rtt_ms", const(22.0), slice="URLLC", stat="max")
    t["ue-probe"].set("ue_probe_rtt_ms", const(420.0), slice="eMBB", stat="max")
    t["ue-probe"].set("ue_probe_rtt_ms", const(1500.0), slice="mMTC", stat="max")


def signalling_drops(t):
    """The cell scheduler drops uplink signalling (3 packets in the last 5 minutes)."""
    t["packet-scheduler"].set(
        "packet_scheduler_class_drops_total",
        lambda age: 0.0 if age > 120 else 3.0, direction="uplink", slice="signalling")


def advisories_only(t):
    """Optional processes stopped (both orchestrators, the cell scheduler, the fault-diagnosis
    service) and a few registration failures: advisories must WARN, and the deployment must
    still PASS. A stopped orchestrator once failed this gate on a healthy
    stack; the stopped scheduler's drop counters vanish and must read as 0, not as no data."""
    for name in ("slice-orchestrator", "network-orchestrator", "packet-scheduler", "fault-diagnosis"):
        t[name].up = False
    t["amf"].add("fivegs_amffunction_rm_reginitfail",
                 lambda age: 4.0 if age > 60 else 6.0, cause="111")


# scenario -> (fault, expected exit code, gates that must FAIL, advisories that must WARN)
# urllc-tail-latency WARNs on a healthy stack: the recorded worst probe is over 10 ms at idle.
TAIL = {"urllc-tail-latency"}
SCENARIOS = {
    "good": (None, 0, set(), TAIL),
    "pcf-down": (pcf_down, 1, {"exporters-up", "no-scrape-failures"}, TAIL),
    "upf-down": (upf_down, 1,
                 {"exporters-up", "no-scrape-failures", "pfcp-associated", "pdu-sessions-at-upf",
                  "upf-sessions-per-slice", "user-plane-flows-per-slice", "slice-traffic-exporter",
                  "ues-reachable-mmtc", "mmtc-latency-budget"},
                 TAIL | {"slices-carrying-traffic"}),
    "gnb-down": (gnb_down, 1,
                 {"gnb-connected", "ues-on-ran", "ues-reachable-embb", "embb-latency-budget"}, TAIL),
    "slice-series-missing": (slice_series_missing, 1,
                             {"sessions-mmtc", "every-slice-has-sessions"}, TAIL),
    "sessions-lost": (sessions_lost, 1,
                      {"sessions-embb", "sessions-urllc", "pdu-sessions-at-upf",
                       "ues-reachable-embb"}, TAIL),
    "ue-unreachable": (ue_unreachable, 1, {"ues-reachable-urllc"}, TAIL | {"urllc-no-probe-loss"}),
    "latency-breach": (latency_breach, 1,
                       {"urllc-latency-budget", "embb-latency-budget", "mmtc-latency-budget"}, TAIL),
    "signalling-drops": (signalling_drops, 1, {"scheduler-no-signalling-drops"}, TAIL),
    "advisories-only": (advisories_only, 0, set(),
                        TAIL | {"orchestrator-reachable", "network-orchestrator-reachable",
                                "packet-scheduler-reachable", "fault-diagnosis-reachable",
                                "no-registration-failures"}),
}


def tsdb(scenario):
    """Flatten the stack into (labels, fn) series, the way Prometheus stores a scrape."""
    targets = healthy()
    fault = SCENARIOS[scenario][0]
    if fault:
        fault(targets)
    out = []
    for tg in targets.values():
        base = dict(tg.labels, job=tg.job, instance=tg.instance)
        out.append((dict(base, __name__="up"), const(1 if tg.up else 0)))
        if not tg.up:
            continue                        # a target that is down has only `up`
        for name, labels, fn in tg.series:
            clash = set(labels) & set(base)
            assert not clash, "%s: exporter label(s) %s clash with target labels" % (name, clash)
            out.append((dict(labels, __name__=name, **base), fn))
    return out


# --------------------------------------------------------------------------- PromQL subset

class BadQuery(Exception):
    pass


TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<dur>\d+[smh])(?![\w.])'
                   r'|(?P<num>\d+(?:\.\d+)?)|(?P<id>[A-Za-z_:][A-Za-z0-9_:]*)'
                   r'|(?P<op>==|!=|>=|<=|=~|!~|[(){}\[\],=<>]))')
AGGS = {"sum": sum, "count": len, "max": max, "min": min, "avg": lambda v: sum(v) / len(v)}
RANGE_FUNCS = {"max_over_time", "min_over_time", "avg_over_time", "increase"}
CMPS = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
        "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}
DUR = {"s": 1, "m": 60, "h": 3600}


def tokenize(expr):
    toks, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise BadQuery("unexpected character at position %d" % pos)
        kind = m.lastgroup
        toks.append((kind, m.group(kind)))
        pos = m.end()
    return toks


class Parser:
    def __init__(self, expr):
        self.toks, self.i = tokenize(expr), 0

    def peek(self, k=0):
        return self.toks[self.i + k] if self.i + k < len(self.toks) else (None, None)

    def take(self, kind=None, value=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind) or (value and tok[1] != value):
            raise BadQuery("expected %s, found %r" % (value or kind, tok[1]))
        self.i += 1
        return tok[1]

    def parse(self):
        node = self.expr()
        if self.peek()[0] is not None:
            raise BadQuery("unexpected %r" % self.peek()[1])
        return node

    def expr(self):
        node = self.cmp()
        while self.peek() == ("id", "or"):
            self.take()
            node = ("or", node, self.cmp())
        return node

    def cmp(self):
        node = self.atom()
        if self.peek()[0] == "op" and self.peek()[1] in CMPS:
            op = self.take()
            node = ("filter", op, node, float(self.take("num")))
        return node

    def atom(self):
        kind, val = self.peek()
        if kind != "id":
            raise BadQuery("unsupported expression at %r" % val)
        if self.peek(1) == ("op", "("):
            self.take()
            self.take("op", "(")
            if val in AGGS:
                node = ("agg", val, self.expr())
            elif val == "vector":
                node = ("vector", float(self.take("num")))
            elif val in RANGE_FUNCS:
                sel = self.selector()
                self.take("op", "[")
                d = self.take("dur")
                self.take("op", "]")
                node = ("range", val, sel, int(d[:-1]) * DUR[d[-1]])
            else:
                raise BadQuery("function %s() is not supported by the stub" % val)
            self.take("op", ")")
            return node
        return ("select", self.selector())

    def selector(self):
        matchers = [("__name__", "=", self.take("id"))]
        if self.peek() == ("op", "{"):
            self.take()
            while self.peek() != ("op", "}"):
                label = self.take("id")
                op = self.take("op")
                if op not in ("=", "!=", "=~", "!~"):
                    raise BadQuery("bad matcher %r" % op)
                matchers.append((label, op, json.loads(self.take("str"))))
                if self.peek() == ("op", ","):
                    self.take()
            self.take("op", "}")
        return matchers


def matches(labels, matchers):
    for label, op, want in matchers:
        have = labels.get(label, "")
        ok = {"=": have == want, "!=": have != want,
              "=~": re.fullmatch(want, have) is not None,
              "!~": re.fullmatch(want, have) is None}[op]
        if not ok:
            return False
    return True


def without_name(labels):
    return {k: v for k, v in labels.items() if k != "__name__"}


def evaluate(node, db):
    """Instant-vector result: a list of (labels, value)."""
    kind = node[0]
    if kind == "select":
        return [(lab, fn(0)) for lab, fn in db if matches(lab, node[1])]
    if kind == "vector":
        return [({}, node[1])]
    if kind == "filter":
        _, op, inner, n = node
        return [(without_name(lab), v) for lab, v in evaluate(inner, db) if CMPS[op](v, n)]
    if kind == "agg":
        values = [v for _, v in evaluate(node[2], db)]
        return [({}, AGGS[node[1]](values))] if values else []
    if kind == "or":
        left = evaluate(node[1], db)
        seen = [lab for lab, _ in left]
        return left + [(lab, v) for lab, v in evaluate(node[2], db) if lab not in seen]
    if kind == "range":
        _, fn_name, sel, window = node
        out = []
        for lab, fn in db:
            if not matches(lab, sel):
                continue
            samples = [fn(age) for age in range(0, min(window, HISTORY), STEP)][::-1]
            if fn_name == "increase":
                v = samples[-1] - samples[0] if len(samples) > 1 else None   # no extrapolation
            else:
                v = {"max_over_time": max, "min_over_time": min,
                     "avg_over_time": lambda s: sum(s) / len(s)}[fn_name](samples)
            if v is not None:
                out.append((without_name(lab), v))
        return out
    raise BadQuery("unknown node %r" % (kind,))


def query(expr, scenario):
    """Answer one /api/v1/query the way Prometheus does: (HTTP status, JSON document)."""
    try:
        result = evaluate(Parser(expr).parse(), tsdb(scenario))
    except BadQuery as e:
        return 400, {"status": "error", "errorType": "bad_data", "error": str(e)}

    def fmt(v):
        if math.isnan(v):
            return "NaN"
        return str(int(v)) if float(v).is_integer() else repr(float(v))

    return 200, {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": lab, "value": [0, fmt(v)]} for lab, v in result]}}


def make_handler(scenario):
    if scenario not in SCENARIOS:
        raise ValueError("unknown Open5GS scenario %r" % scenario)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            expr = parse_qs(urlparse(self.path).query).get("query", [""])[0]
            code, doc = query(expr, scenario)
            body = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler
