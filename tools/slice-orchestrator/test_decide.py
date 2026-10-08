#!/usr/bin/env python3
"""
test_decide.py — the orchestrator's placement rules, checked with no core, no database, no traffic.

Every rule below came from a real failure, so each is pinned by a test:
  * cheapest adequate slice — video must not be packed onto URLLC while eMBB has room
  * no spill-over — with eMBB full, video must be REFUSED, not admitted onto URLLC
    (observed on real traffic: video took 16 of URLLC's 20 Mbps)
  * measured load counts — capacity used by traffic the orchestrator never admitted still counts
  * permission — a subscriber is only placed on slices it is provisioned for
Phase 2b (ADR-016):
  * priority inside a slice — a critical ARP may pre-empt lower-priority, pre-emptable flows; never
    the reverse, never an equal, never a protected one
  * feedback controller — cuts the eMBB admission limit from the operating point when URLLC latency
    or eMBB delivery says so, holds while a cut takes effect, raises only when healthy AND pressed
  * planning — which slices a demand set needs

Run: python3 tools/slice-orchestrator/test_decide.py        (CI runs it too)
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

CRITICAL, STANDARD, IOT = "imsi-208930000000021", "imsi-208930000000024", "imsi-208930000000031"
ARP_CRITICAL = {"priority": 1, "may_preempt": True, "preemptable": False}
ARP_STANDARD = {"priority": 2, "may_preempt": False, "preemptable": True}
SN = {"eMBB": "1/010203", "URLLC": "2/112233", "mMTC": "3/334455"}


def load(env):
    """Import a fresh orchestrator module under a given environment."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("orch_%d" % id(env), os.path.join(HERE, "orchestrator.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    both = ["1/010203", "2/112233"]
    mod.subscribers = lambda: {"imsi-208930000000001": both, "imsi-208930000000011": both,
                               "imsi-208930000000099": ["2/112233"],
                               CRITICAL: both, STANDARD: both, IOT: ["3/334455"]}
    mod._arp.clear()
    mod._arp.update({CRITICAL: {"2/112233": ARP_CRITICAL}, STANDARD: {"2/112233": ARP_STANDARD}})
    mod.measured_mbps = lambda: {}
    mod.flows.clear()
    return mod


def fill(mod, slice_name, mbps, arp=None, fid="x", age=0.0):
    mod.flows.append({"id": fid, "supi": "fill", "cls": "file", "mbps": mbps,
                      "snssai": SN[slice_name], "slice": slice_name,
                      "arp": arp or {"priority": 8, "may_preempt": False, "preemptable": True},
                      "admitted_at": 1000.0 - age, "expires": 9e18})


class Placement(unittest.TestCase):
    def setUp(self):
        self.o = load({"CORE": "free5gc", "EXECUTE": "0", "SPILLOVER": "0", "PROMETHEUS_URL": ""})

    def test_video_goes_to_embb_not_urllc(self):
        r = self.o.decide("imsi-208930000000001", "video")
        self.assertTrue(r["admitted"])
        self.assertEqual(r["slice"]["name"], "eMBB")

    def test_control_goes_to_urllc(self):
        r = self.o.decide("imsi-208930000000001", "control")
        self.assertTrue(r["admitted"])
        self.assertEqual(r["slice"]["name"], "URLLC")

    def test_full_embb_refuses_video_instead_of_spilling(self):
        fill(self.o, "eMBB", 499.0)
        r = self.o.decide("imsi-208930000000001", "video")
        self.assertFalse(r["admitted"])
        self.assertIn("eMBB", r["reason"])
        self.assertEqual(self.o.allocated("2/112233"), 0.0, "URLLC capacity must stay untouched")

    def test_spillover_only_when_explicitly_enabled(self):
        o = load({"CORE": "free5gc", "EXECUTE": "0", "SPILLOVER": "1", "PROMETHEUS_URL": ""})
        fill(o, "eMBB", 499.0)
        r = o.decide("imsi-208930000000001", "video")
        self.assertTrue(r["admitted"])
        self.assertEqual(r["slice"]["name"], "URLLC")

    def test_measured_load_blocks_admission(self):
        self.o.measured_mbps = lambda: {"eMBB": 543.2}
        r = self.o.decide("imsi-208930000000001", "video")
        self.assertFalse(r["admitted"])
        self.assertIn("measured 543.2", r["reason"])

    def test_urllc_capacity_is_enforced(self):
        fill(self.o, "URLLC", 19.5, ARP_STANDARD)      # an equal priority: no pre-emption
        r = self.o.decide("imsi-208930000000001", "control")
        self.assertFalse(r["admitted"])

    def test_latency_need_that_only_urllc_meets_is_refused_for_embb_only_subscriber(self):
        o = self.o
        o.subscribers = lambda: {"imsi-208930000000042": ["1/010203"]}
        r = o.decide("imsi-208930000000042", "control")
        self.assertFalse(r["admitted"])
        self.assertIn("needs <=10ms", r["reason"])

    def test_unknown_subscriber_refused(self):
        r = self.o.decide("imsi-208930000000123", "video")
        self.assertFalse(r["admitted"])

    def test_capacity_override_lowers_capacity(self):
        o = load({"CORE": "free5gc", "EXECUTE": "0", "CAPACITY_OVERRIDE": "eMBB=50", "PROMETHEUS_URL": ""})
        embb = next(s for s in o.SLICES if s["name"] == "eMBB")
        self.assertEqual(embb["capacity_mbps"], 50.0)
        self.assertEqual(embb["policy_capacity_mbps"], 500.0)

    def test_flow_ports_avoid_the_kpi_collectors(self):
        # iperf3 runs its UDP test on its control port; the collectors own UDP 5400 and 5401.
        ports = range(self.o.FLOW_PORT_FIRST, self.o.FLOW_PORT_FIRST + 300)
        self.assertNotIn(5400, ports)
        self.assertNotIn(5401, ports)

    def test_sensor_goes_to_mmtc(self):
        r = self.o.decide(IOT, "sensor")
        self.assertTrue(r["admitted"])
        self.assertEqual(r["slice"]["name"], "mMTC")

    def test_admission_limit_below_capacity_is_enforced(self):
        self.o.limits["eMBB"] = 100.0
        fill(self.o, "eMBB", 95.0)
        r = self.o.decide("imsi-208930000000001", "video")
        self.assertFalse(r["admitted"])
        self.assertIn("95.0/100.0", r["reason"])


class Priority(unittest.TestCase):
    """Priority inside the URLLC slice: ARP tiers, TS 23.501 5.7.2.2."""

    def setUp(self):
        self.o = load({"CORE": "open5gs", "EXECUTE": "0", "SPILLOVER": "0", "PROMETHEUS_URL": ""})

    def test_critical_preempts_standard_when_full(self):
        for i in range(20):
            fill(self.o, "URLLC", 1.0, ARP_STANDARD, fid="s%d" % i, age=i)
        r = self.o.decide(CRITICAL, "control")
        self.assertTrue(r["admitted"])
        self.assertEqual(r["slice"]["name"], "URLLC")
        self.assertEqual(len(r["preempted"]), 1, "stop only as many flows as needed")
        self.assertEqual(r["preempted"][0]["id"], "s0", "newest flow of the lowest priority goes first")
        self.assertAlmostEqual(self.o.allocated("2/112233"), 20.0)

    def test_lowest_priority_goes_before_newer_higher_priority(self):
        fill(self.o, "URLLC", 10.0, {"priority": 5, "may_preempt": False, "preemptable": True}, fid="old-low", age=50)
        fill(self.o, "URLLC", 10.0, ARP_STANDARD, fid="new-std", age=0)
        r = self.o.decide(CRITICAL, "control")
        self.assertEqual([v["id"] for v in r["preempted"]], ["old-low"])

    def test_standard_cannot_preempt_an_equal(self):
        for i in range(20):
            fill(self.o, "URLLC", 1.0, ARP_STANDARD, fid="s%d" % i)
        r = self.o.decide(STANDARD, "control")
        self.assertFalse(r["admitted"])

    def test_critical_cannot_preempt_a_protected_flow(self):
        for i in range(20):
            fill(self.o, "URLLC", 1.0, ARP_CRITICAL, fid="c%d" % i)
        r = self.o.decide(CRITICAL, "control")
        self.assertFalse(r["admitted"])

    def test_preemption_refused_when_victims_would_not_free_enough(self):
        fill(self.o, "URLLC", 19.5, ARP_CRITICAL, fid="big")
        fill(self.o, "URLLC", 0.4, ARP_STANDARD, fid="small")
        r = self.o.decide(CRITICAL, "control")
        self.assertFalse(r["admitted"])
        self.assertEqual(len(self.o.flows), 2, "nothing may be stopped for a demand that is refused anyway")

    def test_priority_counters(self):
        for i in range(20):
            fill(self.o, "URLLC", 1.0, ARP_STANDARD, fid="s%d" % i)
        self.o.decide(CRITICAL, "control")
        self.o.decide(STANDARD, "control")
        self.assertEqual(self.o.by_priority[("URLLC", 1, "admitted")], 1)
        self.assertEqual(self.o.by_priority[("URLLC", 2, "preempted")], 1)
        self.assertEqual(self.o.by_priority[("URLLC", 2, "refused")], 1)


class Controller(unittest.TestCase):
    """The feedback controller moves admission limits (AIMD), eMBB by default; URLLC budget 10 ms."""

    def setUp(self):
        self.o = load({"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "CONTROL": "1",
                       "CONTROL_HOLD": "15"})
        self.o.flow_results.clear()
        self.o.limits.clear()

    def embb_limit(self):
        return self.o.limit(next(s for s in self.o.SLICES if s["name"] == "eMBB"))

    def test_hot_latency_cuts_from_the_operating_point(self):
        self.o.measured_mbps = lambda: {"eMBB": 300.0}
        acts = self.o.control_step(9.0, now=1000.0)
        self.assertEqual(acts[0]["action"], "cut")
        self.assertAlmostEqual(self.embb_limit(), 300.0 * 0.7)

    def test_holds_while_a_cut_takes_effect(self):
        self.o.measured_mbps = lambda: {"eMBB": 300.0}
        self.o.control_step(9.0, now=1000.0)
        self.assertEqual(self.o.control_step(9.0, now=1005.0), [])
        self.assertAlmostEqual(self.embb_limit(), 210.0)
        self.o.control_step(9.0, now=1016.0)
        self.assertLess(self.embb_limit(), 210.0)

    def test_never_below_the_floor(self):
        self.o.measured_mbps = lambda: {"eMBB": 10.0}
        self.o.control_step(9.5, now=1000.0)
        self.assertAlmostEqual(self.embb_limit(), 50.0)       # 10 % of the 500 Mbps ceiling

    def test_raises_only_when_healthy_and_pressed(self):
        self.o.limits["eMBB"] = 200.0
        self.o.measured_mbps = lambda: {"eMBB": 50.0}
        self.assertEqual(self.o.control_step(3.0, now=1000.0), [], "no demand pressing: hold")
        self.o.measured_mbps = lambda: {"eMBB": 190.0}
        self.assertEqual(self.o.control_step(3.0, now=1005.0)[0]["action"], "raise")
        self.assertAlmostEqual(self.embb_limit(), 225.0)       # + 5 % of the ceiling

    def test_holds_between_the_thresholds(self):
        self.o.limits["eMBB"] = 200.0
        self.o.measured_mbps = lambda: {"eMBB": 199.0}
        self.assertEqual(self.o.control_step(6.5, now=1000.0), [])

    def test_no_raise_without_a_latency_signal(self):
        self.o.limits["eMBB"] = 200.0
        self.o.measured_mbps = lambda: {"eMBB": 199.0}
        self.assertEqual(self.o.control_step(None, now=1000.0), [])

    def test_short_flows_cut_even_with_good_latency(self):
        self.o.measured_mbps = lambda: {"eMBB": 400.0}
        for i in range(5):
            self.o.flow_results.append({"slice": "eMBB", "ended": 995.0,
                                        "outcome": "short" if i < 3 else "met"})
        acts = self.o.control_step(3.0, now=1000.0)
        self.assertEqual(acts[0]["action"], "cut")
        self.assertIn("short", acts[0]["why"])

    def test_preempted_flows_are_not_counted_as_short(self):
        self.o.measured_mbps = lambda: {"eMBB": 100.0}
        for i in range(5):
            self.o.flow_results.append({"slice": "eMBB", "ended": 995.0, "outcome": "preempted"})
        self.assertEqual(self.o.control_step(6.0, now=1000.0), [])

    def test_by_default_only_embb_moves(self):
        # Measured clean: eMBB load inflates URLLC's tail, URLLC's own flows do not.
        self.o.measured_mbps = lambda: {"eMBB": 300.0, "URLLC": 20.0}
        self.o.control_step(9.0, now=1000.0)
        self.assertNotIn("URLLC", self.o.limits)
        self.assertNotIn("mMTC", self.o.limits)

    def test_throttle_list_is_configurable(self):
        o = load({"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "CONTROL": "1",
                  "CONTROL_THROTTLE": "eMBB URLLC"})
        o.measured_mbps = lambda: {"eMBB": 300.0, "URLLC": 20.0}
        o.control_step(9.0, now=1000.0)
        self.assertAlmostEqual(o.limits["URLLC"], 14.0)
        self.assertNotIn("mMTC", o.limits)

    def test_critical_urllc_still_preempts_under_a_cut_limit(self):
        self.o.limits["URLLC"] = 5.0
        for i in range(5):
            fill(self.o, "URLLC", 1.0, ARP_STANDARD, fid="s%d" % i, age=i)
        self.o.subscribers = lambda: {CRITICAL: ["1/010203", "2/112233"]}
        r = self.o.decide(CRITICAL, "control")
        self.assertTrue(r["admitted"])
        self.assertEqual(len(r["preempted"]), 1)

    def test_hold_defaults_to_the_flow_duration(self):
        o = load({"CORE": "open5gs", "EXECUTE": "1", "FLOW_SECONDS": "20", "PROMETHEUS_URL": "", "CONTROL": "1"})
        self.assertEqual(o.CONTROL_HOLD, 20.0)


class Planning(unittest.TestCase):
    def setUp(self):
        self.o = load({"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": ""})

    def needed(self, result):
        return {s["slice"] for s in result["slices"] if s["needed"]}

    def test_phase2b_mix_needs_three_slices(self):
        r = self.o.plan([{"class": "control", "count": 10}, {"class": "video", "count": 20},
                         {"class": "sensor", "count": 70}])
        self.assertEqual(self.needed(r), {"URLLC", "eMBB", "mMTC"})
        embb = next(s for s in r["slices"] if s["slice"] == "eMBB")
        self.assertAlmostEqual(embb["required_mbps"], 20 * 8 * 1.25)

    def test_delay_tolerant_demand_needs_one_slice(self):
        r = self.o.plan([{"class": "video", "count": 5}, {"class": "file", "count": 2}])
        self.assertEqual(r["slices_needed"], 1)
        self.assertEqual(self.needed(r), {"eMBB"})

    def test_reports_what_does_not_fit(self):
        r = self.o.plan([{"class": "control", "mbps": 30}])
        urllc = next(s for s in r["slices"] if s["slice"] == "URLLC")
        self.assertFalse(urllc["fits_policy"])

    def test_unknown_class_is_unservable(self):
        r = self.o.plan([{"class": "hologram", "count": 1}])
        self.assertEqual(r["slices_needed"], 0)
        self.assertEqual(r["unservable"][0]["class"], "hologram")

    def test_observed_demand_uses_littles_law(self):
        o = load({"CORE": "open5gs", "EXECUTE": "1", "FLOW_SECONDS": "20", "PROMETHEUS_URL": ""})
        o.demand_log[:] = [(1000.0 - i, "video", 8.0) for i in range(30)]   # 30 demands in 300 s
        d = {x["class"]: x["mbps"] for x in o.observed_demands(window=300, now=1000.0)}
        self.assertAlmostEqual(d["video"], 30 * 8.0 * 20 / 300)            # 16 Mbps on average


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=2).result.wasSuccessful() else 1)
