#!/usr/bin/env python3
"""
test_observability.py — logs, traces, correlation and diagnosis (ADR-024), with no stack.

  * log parsing     real lines captured from the running stack (tests/fixtures/observability): every
                    event, level and slice attribution; the generated Alloy pipeline is current
  * traces          off by default; JSON lines with nesting and errors; OTLP/JSON to a local stub collector;
                    the orchestrators' own decisions and lifecycle steps become spans
  * correlation     robust z-score, CUSUM change point, lead/lag on synthetic and recorded series
  * diagnosis       each rule fires on its symptom and stays silent without it; causes link to
                    consequences; the recorded incidents keep their scores (incidents.py)

Run: python3 tools/observability/test_observability.py        (CI runs it too)
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
FIX = os.path.join(REPO, "tests", "fixtures", "observability")
sys.path.insert(0, HERE)
import correlate  # noqa: E402
import diagnose  # noqa: E402
import incidents  # noqa: E402
import logparse  # noqa: E402

_spec = importlib.util.spec_from_file_location("obs_trace_test", os.path.join(HERE, "trace.py"))
obs_trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(obs_trace)


def parse_fixture(name, container, component=None):
    imsi = re.search(r"ue-(\d+)\.log$", name)
    lines = logparse.read_text(os.path.join(FIX, name)).splitlines()
    return [logparse.parse_line(l, container=container, component=component, year=2026,
                                imsi=("imsi-" + imsi.group(1)) if imsi else None, date=(2026, 11, 8)) for l in lines]


def events(parsed, name):
    return [e for e in parsed if e and e["event"] == name]


class SliceMapping(unittest.TestCase):
    def setUp(self):
        self.s = logparse.SliceMap()

    def test_snssai_dnn_and_pools_come_from_the_repository(self):
        self.assertEqual(self.s.of_snssai(2, "112233"), "URLLC")
        self.assertEqual(self.s.of_snssai("1", "0x010203"), "eMBB")
        self.assertEqual(self.s.of_dnn("iot"), "mMTC")           # from ran/ue-slice-c.yaml
        self.assertEqual(self.s.of_ip("10.46.3.170"), "URLLC")
        self.assertEqual(self.s.of_ip("10.53.0.9"), "mMTC")      # the mMTC UPF's N4 address

    def test_home_slice_of_an_imsi_follows_O5GS_UES(self):
        self.assertEqual(self.s.of_imsi("imsi-208930000000020"), "eMBB")
        self.assertEqual(self.s.of_imsi("imsi-208930000000021"), "URLLC")
        self.assertEqual(self.s.of_imsi("imsi-208930000000031"), "mMTC")
        self.assertEqual(self.s.of_imsi("imsi-208930000000100"), "mMTC")
        self.assertIsNone(self.s.of_imsi("imsi-208930000000101"))

    def test_container_names(self):
        self.assertEqual(self.s.of_container("/o5gs-gnb-urllc"), "URLLC")
        self.assertIsNone(self.s.of_container("o5gs-amf"))
        self.assertEqual(logparse.component_of("o5gs-upf-mmtc"), "upf")
        self.assertEqual(logparse.component_of("/o5gs-ue"), "ue-supervisor")


class LogParsing(unittest.TestCase):
    def test_every_fixture_line_parses_and_ansi_is_stripped(self):
        p = parse_fixture("o5gs-amf.log", "o5gs-amf")
        self.assertTrue(all(e is not None for e in p))
        self.assertTrue("\x1b" in logparse.read_text(os.path.join(FIX, "o5gs-amf.log")))
        self.assertFalse(any("\x1b" in e["msg"] for e in p))
        self.assertEqual({e["level"] for e in p} - {"info", "warning", "error"}, set())

    def test_amf_session_requests_carry_their_slice_from_the_snssai(self):
        reqs = events(parse_fixture("o5gs-amf.log", "o5gs-amf"), "pdu_session_request")
        self.assertEqual({e["slice"] for e in reqs}, {"eMBB", "URLLC", "mMTC"})
        self.assertTrue(all(e["slice_from"] == "snssai" for e in reqs))

    def test_a_second_session_is_attributed_by_dnn_not_the_home_slice(self):
        # imsi-…022 is a URLLC device (home) whose second session is on eMBB (ADR-020).
        e = [x for x in events(parse_fixture("o5gs-smf.log", "o5gs-smf"), "pdu_session_established")
             if x["imsi"] == "imsi-208930000000022"][0]
        self.assertEqual((e["slice"], e["slice_from"]), ("eMBB", "dnn"))

    def test_pfcp_incident_lines(self):
        p = parse_fixture("o5gs-smf.log", "o5gs-smf")
        lost = events(p, "pfcp_heartbeat_lost")
        self.assertEqual([(e["fields"]["peer"], e["slice"], e["level"]) for e in lost],
                         [("10.53.0.8", "URLLC", "warning"), ("10.53.0.9", "mMTC", "warning")])
        self.assertEqual(len(events(p, "pfcp_restoration")), 2)
        self.assertEqual(events(p, "pfcp_no_response")[0]["slice"], "URLLC")
        self.assertEqual(events(p, "pdu_session_release")[0]["fields"]["psi"], "1")

    def test_upf_lines_take_the_slice_of_their_container(self):
        p = parse_fixture("o5gs-upf-mmtc.log", "o5gs-upf-mmtc")
        self.assertEqual([e["slice"] for e in events(p, "ip_pool_exhausted")], ["mMTC", "mMTC"])
        u = parse_fixture("o5gs-upf-urllc.log", "o5gs-upf-urllc")
        self.assertTrue(events(u, "upf_session_added") and events(u, "upf_session_removed"))
        self.assertEqual({e["slice"] for e in u if e["event"]}, {"URLLC"})

    def test_nrf_lifecycle(self):
        p = parse_fixture("o5gs-nrf.log", "o5gs-nrf")
        self.assertEqual(len(events(p, "nf_registered")), 2)
        self.assertEqual(len(events(p, "nf_deregistered")), 2)
        self.assertEqual(len(events(p, "nf_terminating")), 2)
        self.assertEqual(len(events(p, "nf_started")), 1)

    def test_gnb_that_cannot_reach_the_amf(self):
        p = parse_fixture("o5gs-gnb-urllc-n2-refused.log", "o5gs-gnb-urllc")
        e = events(p, "n2_connect_failed")[0]
        self.assertEqual((e["slice"], e["level"], e["fields"]["port"]), ("URLLC", "error", "38413"))
        self.assertEqual(len(events(p, "ue_signal_lost")), 1)

    def test_ue_log_file(self):
        p = [e for e in parse_fixture("ue-208930000000001.log", "o5gs-ue", "ue") if e]
        self.assertTrue(all(e["imsi"] == "imsi-208930000000001" for e in p))
        tun = events(p, "tun_up")
        self.assertEqual([(t["fields"]["ip"], t["slice"]) for t in tun],
                         [("10.45.3.135", "eMBB"), ("10.46.3.170", "URLLC")])
        self.assertEqual(len(events(p, "registration_complete")), 1)
        self.assertEqual(len(events(p, "access_barred")), 1)
        self.assertEqual(events(p, "registration_complete")[0]["slice_from"], "imsi")

    def test_radio_link_failure_line_from_the_pinned_source(self):
        # Not in the captured logs; the text is UERANSIM src/ue/nas/mm/radio.cpp:260, logged by "nas".
        e = logparse.parse_line("[2026-11-08 13:41:02.118] [nas] [error] Radio link failure detected",
                                component="ue", imsi="imsi-208930000000025")
        self.assertEqual((e["event"], e["slice"], e["level"]), ("radio_link_failure", "URLLC", "error"))
        g = logparse.parse_line("[2026-11-08 13:41:02.200] [gtp] [error] Uplink data failure, PDU session not found. UE[7] PSI[1]",
                                container="o5gs-gnb-embb")
        self.assertEqual((g["event"], g["slice"]), ("pdu_session_not_found", "eMBB"))

    def test_ue_supervisor(self):
        p = [e for e in parse_fixture("o5gs-ue-supervisor.log", "o5gs-ue") if e]
        r = events(p, "ue_restart")
        self.assertEqual({e["slice"] for e in r}, {"eMBB", "URLLC", "mMTC"})
        self.assertEqual(events(p, "ues_up")[0]["fields"], {"up": "99", "total": "100"})
        self.assertEqual(len(events(p, "ue_start_timeout")), 1)
        self.assertIsNotNone(r[0]["ts"])

    def test_scheduler_reapplies(self):
        p = [e for e in parse_fixture("o5gs-scheduler.log", "o5gs-scheduler") if e]
        self.assertEqual([e["fields"]["mode"] for e in events(p, "scheduler_apply")], ["sliced", "off", "fifo"])

    def test_docker_timestamp_wins_and_sets_the_year(self):
        e = logparse.parse_line("2025-12-31T23:59:59.250000000Z 12/31 23:59:59.000: [smf] INFO: PFCP associated "
                                "[10.53.0.7]:8805 (../src/smf/pfcp-sm.c:189)", container="o5gs-smf")
        self.assertAlmostEqual(e["ts"], 1767225599.25, places=2)
        self.assertEqual((e["event"], e["slice"]), ("pfcp_associated", "eMBB"))

    def test_ue_grep_output(self):
        lines = ["/tmp/ue-208930000000031.log:[2026-11-08 13:41:02.118] [nas] [error] Radio link failure detected",
                 "/tmp/ue-208930000000002.log:[2026-11-08 13:41:03.000] [nas] [info] Initial Registration is successful"]
        p = logparse.parse_ue_grep(lines)
        self.assertEqual([(e["event"], e["slice"]) for e in p], [("radio_link_failure", "mMTC"), ("registration_complete", "eMBB")])

    def test_rules_are_re2_compatible_and_fault_events_exist(self):
        names = {r[0] for r in logparse.RULES}
        self.assertLessEqual(logparse.FAULT_EVENTS, names)
        for ev, comps, pat, lvl in logparse.RULES:
            re.compile(pat)
            self.assertNotRegex(pat, r"\(\?[=!<]|\\[1-9]", "%s: look-around/back-references are not RE2" % ev)
            self.assertNotIn("`", pat)

    def test_generated_alloy_pipeline_is_current(self):
        committed = logparse.read_text(os.path.join(
            REPO, "deployments", "open5gs", "observability", "alloy", "config.alloy")).replace("\r\n", "\n")
        self.assertEqual(committed, logparse.alloy_config(),
                         "regenerate: python3 tools/observability/logparse.py --alloy deployments/open5gs/observability/alloy/config.alloy")
        for ev, *_ in logparse.RULES:
            self.assertIn('event = "%s"' % ev, committed)
        self.assertNotRegex(committed, r"imsi\s*=", "an IMSI must never become a Loki label")

    def test_every_match_selector_is_valid_logql_quoting(self):
        # `alloy fmt` checks only Alloy's own syntax; the selector inside is parsed when the pipeline
        # starts. LogQL reads a double-quoted value as a Go string, so an escape Go does not know
        # (re.escape's "\-") stops Alloy with "invalid char escape", and Alloy's match stage takes no
        # backtick strings at all. Both happened on the first deployment (2026-11-09).
        go_escape = re.compile(r'\\(?:[abfnrtv\\"\']|[0-7]{3}|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})')
        sels = [json.loads(m) for m in re.findall(r'^\s*selector = ("(?:[^"\\]|\\.)*")$',
                                                  logparse.alloy_config(), re.M)]
        self.assertGreater(len(sels), len(logparse.RULES))
        matcher = re.compile(r'\s*(\w+)\s*(=~|!~|!=|=)\s*("(?:[^"\\]|\\.)*")\s*(?:,|$)')
        for sel in sels:
            m = re.match(r"\{(.*?)\}\s*(.*)$", sel)
            self.assertIsNotNone(m, sel)
            stream, pipeline = m.groups()
            # Matcher values must be double-quoted: a backtick there is "expecting STRING" in Alloy.
            pos = 0
            while pos < len(stream):
                mm = matcher.match(stream, pos)
                self.assertIsNotNone(mm, "matcher values must be double-quoted strings: %r" % sel)
                rest = go_escape.sub("", mm.group(3)[1:-1])
                self.assertNotIn("\\", rest, "invalid Go escape in %r of %r" % (mm.group(3), sel))
                if mm.group(2) in ("=~", "!~"):
                    re.compile(json.loads(mm.group(3)))       # the unescaped value is the regex
                pos = mm.end()
            # The line filter is one double-quoted Go string as well (Alloy's match stage rejects a
            # backtick raw string: "unexpected IDENTIFIER, expecting STRING"), holding a valid regex.
            lf = re.match(r'^\|~ ("(?:[^"\\]|\\.)*")$', pipeline)
            self.assertIsNotNone(lf, "line filter must be |~ \"...\": %r" % sel)
            self.assertNotIn("\\", go_escape.sub("", lf.group(1)[1:-1]), "invalid Go escape in %r" % sel)
            re.compile(json.loads(lf.group(1)))


class Traces(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.jsonl")

    def test_off_by_default(self):
        t = obs_trace.Tracer("x", path="", otlp_endpoint="")
        with t.span("a") as sp:
            sp.set(k=1)
        self.assertIsNone(t.event("b"))
        self.assertFalse(os.path.exists(self.path))

    def test_spans_nest_and_errors_are_recorded(self):
        t = obs_trace.Tracer("svc", path=self.path, otlp_endpoint="")
        with t.span("outer", slice="URLLC") as o:
            t.event("decision", action="cut")
            with self.assertRaises(ValueError):
                with t.span("inner"):
                    raise ValueError("boom")
        recs = obs_trace.read_spans(self.path)
        by = {r["name"]: r for r in recs}
        self.assertEqual(by["decision"]["type"], "event")
        self.assertEqual(by["decision"]["parent_id"], o.span_id)
        self.assertEqual(by["inner"]["parent_id"], o.span_id)
        self.assertEqual(by["inner"]["status"], "error")
        self.assertIn("boom", by["inner"]["error"])
        self.assertEqual(len({r["trace_id"] for r in recs}), 1)
        self.assertIsNone(by["outer"]["parent_id"])

    def test_per_writer_files_are_read_with_the_main_one(self):
        # The scheduler (root, in a container) cannot append to the orchestrators' file in /tmp
        # (fs.protected_regular=2), so it writes t-scheduler.jsonl; readers must merge both, in
        # time order, and must not pick up an unrelated file that merely shares the directory.
        a = obs_trace.Tracer("slice-orchestrator", path=self.path, otlp_endpoint="")
        b = obs_trace.Tracer("packet-scheduler", path=os.path.join(self.dir, "t-scheduler.jsonl"),
                             otlp_endpoint="")
        with a.span("admission.decide"):
            pass
        with b.span("scheduler.apply"):
            pass
        with a.span("controller.cut"):
            pass
        with open(os.path.join(self.dir, "other.jsonl"), "w") as fh:
            fh.write('{"name": "unrelated", "start": 0, "end": 0}\n')
        names = [r["name"] for r in obs_trace.read_spans(self.path)]
        self.assertEqual(names, ["admission.decide", "scheduler.apply", "controller.cut"])

    def test_unwritable_file_never_reaches_the_caller(self):
        t = obs_trace.Tracer("svc", path=os.path.join(self.dir, "no", "such", "dir.jsonl"), otlp_endpoint="")
        with t.span("a"):
            pass
        self.assertEqual(t.dropped, 1)

    def test_otlp_json_to_a_stub_collector(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, self.headers.get("Content-Type"),
                            json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            t = obs_trace.Tracer("network-orchestrator", path="", otlp_endpoint="http://127.0.0.1:%d" % srv.server_port)
            with t.span("netorch.activate", slice="mMTC", cpus=0.5):
                t.event("netorch.step", step="upf")
            for _ in range(50):
                if got:
                    break
                threading.Event().wait(0.1)
        finally:
            srv.shutdown()
        path, ctype, body = got[0]
        self.assertEqual((path, ctype), ("/v1/traces", "application/json"))
        rs = body["resourceSpans"][0]
        self.assertEqual(rs["resource"]["attributes"][0], {"key": "service.name", "value": {"stringValue": "network-orchestrator"}})
        spans = rs["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        for sp in spans:
            self.assertRegex(sp["traceId"], r"^[0-9a-f]{32}$")
            self.assertRegex(sp["spanId"], r"^[0-9a-f]{16}$")
            self.assertRegex(sp["startTimeUnixNano"], r"^\d{19}$")
            self.assertEqual(sp["kind"], 1)
        outer = [s for s in spans if s["name"] == "netorch.activate"][0]
        self.assertIn({"key": "cpus", "value": {"doubleValue": 0.5}}, outer["attributes"])

    def test_netorch_operation_log_becomes_spans(self):
        op = {"op": "activate", "slice": "mMTC", "result": "done", "t0": 1000.0, "seconds": 12.2, "reason": "manual",
              "steps": [{"step": "upf", "seconds": 2.7, "detail": "associated"}, {"step": "gnb", "seconds": 1.5, "detail": "NG Setup done"}]}
        with open(self.path, "w") as f:
            f.write(json.dumps(op) + "\n")
        spans = obs_trace.read_netorch_ops(self.path)
        self.assertEqual([s["name"] for s in spans], ["netorch.activate", "netorch.step", "netorch.step"])
        self.assertAlmostEqual(spans[2]["start"], 1002.7)


def _load(path, name, env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


class InstrumentedComponents(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.jsonl")

    def test_slice_orchestrator_traces_admission_preemption_and_the_controller(self):
        o = _load(os.path.join(REPO, "tools", "slice-orchestrator", "orchestrator.py"), "orch_traced",
                  {"CORE": "free5gc", "EXECUTE": "0", "PROMETHEUS_URL": "", "CONTROL_POLICY": "aimd",
                   "OBS_TRACE_FILE": self.path})
        crit, std = "imsi-208930000000021", "imsi-208930000000024"
        o.subscribers = lambda: {crit: ["2/112233"], std: ["2/112233"], "imsi-208930000000001": ["1/010203"]}
        o._arp.clear()
        o._arp.update({crit: {"2/112233": {"priority": 1, "may_preempt": True, "preemptable": False}},
                       std: {"2/112233": {"priority": 2, "may_preempt": False, "preemptable": True}}})
        o.measured_mbps = lambda: {}
        o.flows.clear()
        for _ in range(20):
            self.assertTrue(o.decide_traced(std, "control")["admitted"])
        r = o.decide_traced(crit, "control")
        self.assertTrue(r["admitted"])
        self.assertFalse(o.decide_traced("imsi-208930000000001", "control")["admitted"])
        o.flows.append({"id": "f", "supi": "x", "cls": "file", "mbps": 300.0, "snssai": "1/010203", "slice": "eMBB",
                        "arp": {"priority": 8, "may_preempt": False, "preemptable": True}, "admitted_at": 0, "expires": 9e18})
        o.control_step(9.5, now=1000.0)
        recs = obs_trace.read_spans(self.path)
        decides = [r for r in recs if r["name"] == "admission.decide"]
        self.assertEqual(len(decides), 22)
        pre = [r for r in recs if r["name"] == "admission.preempt"][0]
        self.assertEqual(pre["parent_id"], decides[20]["span_id"], "a pre-emption is a child of the decision")
        self.assertEqual(decides[21]["attrs"]["admitted"], False)
        cut = [r for r in recs if r["name"] == "controller.cut"][0]
        self.assertEqual((cut["attrs"]["slice"], cut["attrs"]["from"]), ("eMBB", 500.0))

    def test_network_orchestrator_steps_are_child_spans_with_durations(self):
        n = _load(os.path.join(REPO, "tools", "network-orchestrator", "netorch.py"), "netorch_traced",
                  {"OBS_TRACE_FILE": self.path})
        op = n.Op("activate", "mMTC", "test")
        with n.TRACE.span("netorch.activate", slice="mMTC") as root:
            op.step("upf", lambda: "associated")
            with self.assertRaises(n.Failure):
                op.step("gnb", lambda: (_ for _ in ()).throw(n.Failure("no NG Setup")))
        recs = obs_trace.read_spans(self.path)
        steps = [r for r in recs if r["name"] == "netorch.step"]
        self.assertEqual([s["attrs"]["step"] for s in steps], ["upf", "gnb"])
        self.assertEqual([s["status"] for s in steps], ["ok", "error"])
        self.assertTrue(all(s["parent_id"] == root.span_id for s in steps))
        self.assertIn("t0", op.as_dict())


class Correlation(unittest.TestCase):
    def test_robust_statistics(self):
        self.assertEqual(correlate.median([3, 1, 2]), 2)
        self.assertEqual(correlate.mad([1, 2, 3, 4, 100]), 1)
        self.assertAlmostEqual(correlate.robust_z(10, [1, 2, 3, 4, 100], 0.1), (10 - 3) / 1.4826, places=3)
        self.assertEqual(correlate.robust_z(5, [3, 3, 3], 1.0), 2.0, "flat baseline: the floor, not infinity")

    def test_cusum_finds_the_step(self):
        k, shift = correlate.cusum_change_point([2, 2.1, 1.9, 2, 2.2, 9, 9.1, 8.8, 9.2])
        self.assertEqual(k, 5)
        self.assertAlmostEqual(shift, 7.0, delta=0.3)

    def test_lead_lag_recovers_a_known_lead(self):
        cause = [(t, 1.0 if 100 <= t < 160 else 0.0) for t in range(0, 300, 5)]
        effect = [(t, 1.0 if 130 <= t < 190 else 0.0) for t in range(0, 300, 5)]
        lag, r = correlate.lead_lag(effect, cause, 0, 295, 5, 10)
        self.assertEqual(lag, 30)
        self.assertGreater(r, 0.95)

    def test_recorded_controller_run(self):
        d = os.path.join(REPO, "docs", "evidence", "open5gs-orchestrator-dynamic", "controller-throttling-urllc-too")
        series = {}
        for f in ("2-series-controlled-slice_admission_limit_mbps.json", "2-series-controlled-urllc_p95.json",
                  "2-series-controlled-slice_measured_mbps.json"):
            series.update(correlate.load_series_file(os.path.join(d, f)))
        self.assertIn("urllc_owd_p95", series)
        rep = correlate.correlate(series, [], [], 1791468665, 1791468857, baseline=(1791468645, 1791468665),
                                  trigger="urllc_owd_p95")
        keys = [a["key"] for a in rep["anomalies"]]
        self.assertIn("admission_limit{eMBB}", keys)
        self.assertIn("measured_mbps{eMBB}", keys)
        lim = [a for a in rep["anomalies"] if a["key"] == "admission_limit{eMBB}"][0]
        self.assertLess(lim["worst"], 100)
        self.assertLessEqual(lim["lag_s"], 0, "the limit moves after URLLC latency, not before")
        self.assertEqual(correlate.load_series_file(os.path.join(d, "2-control-log-controlled.json")), {})

    def test_log_events_against_their_baseline(self):
        evs = [e for e in parse_fixture("o5gs-smf.log", "o5gs-smf") if e]
        start = incidents._utc("2026-11-08 09:09:00")
        rep = correlate.correlate({}, evs, [], start, start + 1900, baseline=(start - 3600, start))
        keys = {a["key"] for a in rep["anomalies"]}
        self.assertIn("pfcp_heartbeat_lost{URLLC}", keys)
        self.assertTrue(any(t["source"] == "log" for t in rep["timeline"]))

    def test_gate_verdict_trigger(self):
        p = os.path.join(tempfile.mkdtemp(), "v.json")
        with open(p, "w") as fh:
            json.dump({"results": [{"id": "pfcp-associated", "status": "FAIL"}, {"id": "exporters-up", "status": "PASS"}]}, fh)
        sig, failed = correlate.gate_trigger(p)
        self.assertEqual((sig, len(failed)), ("pfcp_peers", 1))

    def test_snssai_label_becomes_the_slice(self):
        s = correlate.matrix_to_series("sessions", [{"metric": {"snssai": "2-112233"}, "values": [[1, "30"]]}], "snssai")
        self.assertEqual(list(s), ["sessions{URLLC}"])


class Diagnosis(unittest.TestCase):
    F = diagnose.Facts

    def ids(self, facts):
        return [c["id"] for c in diagnose.diagnose(facts)["causes"]]

    def test_nothing_wrong_is_healthy(self):
        r = diagnose.diagnose(self.F(info={"urllc_p99_ms": 4.0, "host_load1": 2.0, "ncpu": 12}))
        self.assertTrue(r["healthy"])
        self.assertEqual(r["causes"], [])

    def test_no_metrics_is_unverified_not_healthy(self):
        r = diagnose.diagnose(self.F(info={"prometheus_error": "unreachable: connection refused"}))
        self.assertEqual((r["healthy"], r["verified"]), (None, False))
        svc = diagnose.Service(120, 600, "http://127.0.0.1:1", "", False)
        svc.result = r
        self.assertIn("fault_diagnosis_healthy 0", svc.metrics())
        self.assertIn("fault_diagnosis_verified 0", svc.metrics())

    def test_unreachable_prometheus_is_asked_once(self):
        series, errors = correlate.fetch_signals("http://127.0.0.1:1", 0, 60)
        self.assertEqual((series, list(errors)), ({}, ["prometheus"]))

    def test_cpu_needs_a_symptom(self):
        self.assertEqual(self.ids(self.F(info={"host_load1": 30.0, "ncpu": 12})), [])
        self.assertEqual(self.ids(self.F(info={"host_load1": 30.0, "ncpu": 12, "urllc_p99_ms": 25.0})), ["cpu-saturation"])

    def test_upf_at_one_core_is_direct_cpu_evidence(self):
        f = self.F(info={"urllc_p99_ms": 14.0, "container_cpu": {"upf-embb": 92.0}})
        c = diagnose.diagnose(f)["causes"][0]
        self.assertEqual((c["id"], c["score"]), ("cpu-saturation", 0.8))
        self.assertTrue(any("upf-embb" in e["detail"] for e in c["evidence"]))

    def test_fifo_cell_explains_the_radio_link_failures(self):
        f = self.F(info={"scheduler_mode": "fifo", "cell_dl_mbps": 100.0, "offered_mbps": {"eMBB": 150.0},
                         "urllc_loss_pct": 60.0, "rlf_count": 97, "ues_without_data_path": 97, "class_drops": {"cell": 5000}})
        r = diagnose.diagnose(f)
        self.assertEqual([c["id"] for c in r["causes"]], ["cell-congestion", "rlf-no-data-path"])
        self.assertEqual(r["causes"][1]["explained_by"], ["cell-congestion"])
        self.assertTrue(any("sliced" in a for a in r["causes"][0]["actions"]))

    def test_signalling_drops_are_critical(self):
        c = diagnose.diagnose(self.F(info={"scheduler_mode": "sliced", "class_drops": {"signalling": 3}}))["causes"][0]
        self.assertEqual((c["id"], c["score"], c["severity"]), ("cell-congestion", 0.95, "critical"))

    def test_periodic_rlf_points_at_clock_steps(self):
        c = diagnose.diagnose(self.F(info={"rlf_count": 5, "rlf_times": [0, 31, 62, 94, 125]}))["causes"][0]
        self.assertIn("wall-clock", c["hint"])

    def test_pfcp_from_logs(self):
        f = self.F(logs={"pfcp_heartbeat_lost{URLLC}": {"window": 1, "samples": ["No Heartbeat from UPF [10.53.0.8]"]},
                         "pfcp_restoration{-}": {"window": 1, "samples": ["PFCP restoration"]}})
        c = diagnose.diagnose(f)["causes"][0]
        self.assertEqual((c["id"], c["score"], c["slices"]), ("pfcp-association-loss", 0.9, ["URLLC"]))

    def test_gnb_without_n2_explains_registration_failures(self):
        f = self.F(logs={"n2_connect_failed{URLLC}": {"window": 1}}, info={"gate_failed": ["ues-on-ran"]})
        r = diagnose.diagnose(f)
        self.assertEqual([c["id"] for c in r["causes"]], ["gnb-n2-down", "registration-failure"])
        self.assertEqual(r["causes"][1]["explained_by"], ["gnb-n2-down"])

    def test_nf_restarting_is_not_an_outage(self):
        self.assertEqual(self.ids(self.F(logs={"nf_deregistered{-}": {"window": 2}, "nf_registered{-}": {"window": 2}})), [])
        self.assertEqual(self.ids(self.F(logs={"nf_deregistered{-}": {"window": 2}})), ["registration-failure"])

    def test_controller_throttling_the_protected_slice_is_flagged(self):
        cut = {"name": "controller.cut", "attrs": {"slice": "URLLC", "action": "cut", "from": 20, "to": 4.2, "why": "p95"}}
        c = diagnose.diagnose(self.F(traces=[cut]))["causes"][0]
        self.assertEqual((c["id"], c["severity"]), ("orchestrator-throttling", "warning"))
        self.assertIn("CONTROL_THROTTLE=eMBB", c["actions"][0])
        cut["attrs"]["slice"] = "eMBB"
        self.assertEqual(diagnose.diagnose(self.F(traces=[cut]))["causes"][0]["severity"], "info")

    def test_optional_target_is_advisory_core_target_is_critical(self):
        c = diagnose.diagnose(self.F(info={"jobs_down": {"network-orchestrator": 0}}))["causes"][0]
        self.assertEqual((c["severity"], c["score"]), ("advisory", 0.8))
        self.assertTrue(any("start_netorch.sh" in a for a in c["actions"]))
        f = self.F(metrics={"target_up{open5gs}": {"window_min": 0, "window_last": 0, "worst": 0}})
        self.assertEqual(diagnose.diagnose(f)["causes"][0]["severity"], "critical")

    def test_stale_logs(self):
        self.assertEqual(self.ids(self.F(info={"ue_log_count": 240, "ue_truth": 100, "planned_ues": 100})),
                         ["observability-artifact"])
        self.assertEqual(self.ids(self.F(info={"ue_log_count": 100, "ue_truth": 100, "planned_ues": 100})), [])

    def test_service_metrics(self):
        svc = diagnose.Service(120, 600, "http://127.0.0.1:1", "", False)
        svc.result = diagnose.diagnose(self.F(info={"jobs_down": {"packet-scheduler": 0}}))
        text = svc.metrics()
        self.assertIn('fault_diagnosis_cause_score{cause="target-down",severity="advisory",slice="all"} 0.80', text)
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertTrue(line.startswith("fault_diagnosis_"), line)

    def test_recorded_incidents_keep_their_scores(self):
        rows, unlabelled, t = incidents.evaluate(incidents.all_cases())
        self.assertGreaterEqual(t["cases"], 45)
        self.assertGreaterEqual(t["precision"], 0.95, [r for r in rows if r.get("false_positives")])
        self.assertGreaterEqual(t["recall"], 0.95, [r for r in rows if r.get("missed")])
        self.assertEqual(t["healthy_ok"], t["healthy_n"])
        by = {r["id"]: r for r in rows}
        self.assertEqual(by["pfcp-urllc-upf-20261108"]["got"][0], "pfcp-association-loss")
        self.assertEqual(by["deploy-gnb-port-typo-20260914"]["got"][0], "gnb-n2-down")
        self.assertEqual(by["dyn-urllc-too-controlled"]["got"][0], "orchestrator-throttling")


if __name__ == "__main__":
    unittest.main(verbosity=1)
