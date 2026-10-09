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

    def test_inactive_slice_admits_nothing_but_demand_is_logged(self):
        mmtc = next(s for s in self.o.SLICES if s["name"] == "mMTC")
        mmtc["active"] = False
        r = self.o.decide(IOT, "sensor")
        self.assertFalse(r["admitted"])
        self.assertIn("not active", r["reason"])
        self.assertEqual(self.o.demand_log[-1][1], "sensor", "planning must still see refused demand")
        plan = self.o.plan(self.o.observed_demands(window=60))
        self.assertIn("mMTC", {s["slice"] for s in plan["slices"] if s["needed"]})

    def test_inactive_slice_is_skipped_when_another_permitted_one_fits(self):
        urllc = next(s for s in self.o.SLICES if s["name"] == "URLLC")
        urllc["active"] = False
        r = self.o.decide("imsi-208930000000001", "control")      # needs 10 ms: only URLLC fits
        self.assertFalse(r["admitted"])
        r = self.o.decide("imsi-208930000000001", "video")        # eMBB still active
        self.assertTrue(r["admitted"])

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
        self.o = load({"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "CONTROL": "1", "CONTROL_POLICY": "aimd",
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
        o = load({"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "CONTROL": "1", "CONTROL_POLICY": "aimd",
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


class AdmissionPolicies(unittest.TestCase):
    def mod(self, policy, **extra):
        env = {"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "ADMISSION_POLICY": policy}
        env.update(extra)
        return load(env)

    def test_reservation_keeps_room_for_exempt_classes(self):
        o = self.mod("reservation", RESERVE="URLLC=4", RESERVE_EXEMPT="control")
        fill(o, "URLLC", 15.0, ARP_STANDARD)
        r = o.decide("imsi-208930000000001", "blog")          # would leave 4 free: allowed
        self.assertTrue(r["admitted"])
        r = o.decide("imsi-208930000000001", "blog")          # would leave 3 free: held back
        self.assertFalse(r["admitted"])
        self.assertIn("reservation", r["reason"])
        r = o.decide("imsi-208930000000001", "control")       # exempt: uses the reserve
        self.assertTrue(r["admitted"])

    def test_pricing_admits_everything_when_empty_and_only_value_when_full(self):
        o = self.mod("pricing", CLASS_VALUE="control:10 blog:2 video:1 file:1 sensor:5")
        self.assertTrue(o.decide("imsi-208930000000001", "blog")["admitted"])
        fill(o, "URLLC", 17.0, ARP_STANDARD)                  # utilisation 0.9: price near the top
        self.assertFalse(o.decide("imsi-208930000000001", "blog")["admitted"])
        self.assertTrue(o.decide("imsi-208930000000001", "control")["admitted"])


class Policies(unittest.TestCase):
    """The alternative control laws (ADR-018): PIE-style PI, one-step MPC with an online model, UCB1."""

    def mod(self, policy, **extra):
        env = {"CORE": "open5gs", "EXECUTE": "0", "PROMETHEUS_URL": "", "CONTROL": "1", "CONTROL_POLICY": policy}
        env.update(extra)
        o = load(env)
        o.flow_results.clear()
        o.limits.clear()
        return o

    def embb(self, o):
        return o.limit(next(s for s in o.SLICES if s["name"] == "eMBB"))

    def test_pi_cuts_above_target_and_is_bounded(self):
        o = self.mod("pi")
        o.measured_mbps = lambda: {"eMBB": 500.0}
        o.control_step(17.0, now=1000)          # target 6.5 ms; first step has no derivative term
        self.assertLess(self.embb(o), 500.0)
        self.assertGreaterEqual(self.embb(o), 500.0 - 0.3 * 500, "at most 30 % of the ceiling per step")

    def test_pi_raises_only_with_demand_pressing(self):
        o = self.mod("pi")
        o.limits["eMBB"] = 200.0
        o.measured_mbps = lambda: {"eMBB": 50.0}
        o.control_step(3.0, now=1000)
        self.assertEqual(self.embb(o), 200.0, "below target but nobody needs more")
        o.measured_mbps = lambda: {"eMBB": 195.0}
        o.control_step(3.0, now=1005)
        self.assertGreater(self.embb(o), 200.0)

    def test_mpc_learns_the_model_and_meets_the_target(self):
        o = self.mod("mpc")
        o.limits["eMBB"] = 500.0
        # True plant: p95 = 2 ms + 10 ms per unit of load share. Target 6.5 ms -> load share 0.45.
        for i, share in enumerate([0.1, 0.3, 0.5, 0.7, 0.9, 0.6, 0.4, 0.8, 0.5, 0.45, 0.45, 0.45]):
            o.measured_mbps = (lambda m: (lambda: {"eMBB": m}))(share * 500)
            o.control_step(2.0 + 10.0 * share, now=1000 + 5 * i)
        a, b = o.control["mpc"]["eMBB"]["theta"]
        self.assertAlmostEqual(a, 2.0, delta=0.3)
        self.assertAlmostEqual(b, 10.0, delta=1.0)
        self.assertAlmostEqual(self.embb(o), 0.45 * 500, delta=30)

    def test_ucb_explores_low_arms_first_then_exploits_the_best(self):
        o = self.mod("ucb", UCB_DWELL="10", UCB_ARMS="0.2 0.5 1.0")
        o.measured_mbps = lambda: {"eMBB": 500.0}
        o.control_step(3.0, now=0)
        self.assertEqual(self.embb(o), 100.0, "first arm, 0.2 of the ceiling")
        t = 0
        # latency is fine up to arm 0.5, violated at 1.0
        for _ in range(40):
            t += 5
            lat = 12.0 if self.embb(o) > 300 else 3.0
            o.control_step(lat, now=t)
        st = o.control["ucb"]["eMBB"]
        self.assertTrue(all(n > 0 for n in st["n"]), "every arm tried")
        best = max(range(3), key=lambda i: st["sum"][i] / st["n"][i])
        self.assertEqual(o.UCB_ARMS[best], 0.5, "highest throughput without violating latency")

    def test_short_flows_guard_applies_to_new_policies(self):
        o = self.mod("pi")
        o.measured_mbps = lambda: {"eMBB": 300.0}
        for i in range(5):
            o.flow_results.append({"slice": "eMBB", "ended": 995.0, "outcome": "short"})
        o.control_step(3.0, now=1000)
        self.assertLessEqual(self.embb(o), 0.85 * 300 + 0.01)


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
        self.assertAlmostEqual(embb["mean_headroom_mbps"], 20 * 8 * 1.25)

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

    def test_kaufman_roberts_reduces_to_erlang_b(self):
        # One class of 1-unit demands: Erlang B. B(10 Erlangs, 15 servers) = 0.0365 (standard tables).
        b = self.o.kaufman_roberts([(10.0, 1)], 15)[0]
        self.assertAlmostEqual(b, 0.0365, places=3)

    def test_kr_sizing_meets_the_blocking_target_and_wider_classes_block_more(self):
        mbps, unit, block = self.o.kr_size([(10.0, 8.0), (5.0, 15.0)], target=0.01)
        self.assertLessEqual(max(block), 0.01)
        self.assertGreater(block[1], block[0], "a 15 Mbps demand is blocked more than an 8 Mbps one")
        mean = 10 * 8 + 5 * 15
        self.assertGreater(mbps, 1.25 * mean, "mean x 1.25 is not enough for 1 % blocking here")

    def test_plan_reports_kr_sizing(self):
        r = self.o.plan([{"class": "video", "count": 20}])
        embb = next(s for s in r["slices"] if s["slice"] == "eMBB")
        self.assertGreater(embb["kr_required_mbps"], embb["demand_mbps"])

    def test_observed_demand_uses_littles_law(self):
        o = load({"CORE": "open5gs", "EXECUTE": "1", "FLOW_SECONDS": "20", "PROMETHEUS_URL": ""})
        o.demand_log[:] = [(1000.0 - i, "video", 8.0) for i in range(30)]   # 30 demands in 300 s
        d = {x["class"]: x["mbps"] for x in o.observed_demands(window=300, now=1000.0)}
        self.assertAlmostEqual(d["video"], 30 * 8.0 * 20 / 300)            # 16 Mbps on average


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=2).result.wasSuccessful() else 1)
