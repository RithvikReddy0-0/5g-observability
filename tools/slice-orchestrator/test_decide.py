#!/usr/bin/env python3
"""
test_decide.py — the orchestrator's placement rules, checked with no core, no database, no traffic.

Every rule below came from a real failure, so each is pinned by a test:
  * cheapest adequate slice — video must not be packed onto URLLC while eMBB has room
  * no spill-over — with eMBB full, video must be REFUSED, not admitted onto URLLC
    (observed on real traffic: video took 16 of URLLC's 20 Mbps)
  * measured load counts — capacity used by traffic the orchestrator never admitted still counts
  * permission — a subscriber is only placed on slices it is provisioned for

Run: python3 tools/slice-orchestrator/test_decide.py        (CI runs it too)
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


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
                               "imsi-208930000000099": ["2/112233"]}
    mod.measured_mbps = lambda: {}
    mod.flows.clear()
    return mod


def fill(mod, slice_name, mbps):
    mod.flows.append({"id": "x", "supi": "fill", "cls": "file", "mbps": mbps,
                      "snssai": {"eMBB": "1/010203", "URLLC": "2/112233"}[slice_name],
                      "slice": slice_name, "expires": 9e18})


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
        fill(self.o, "eMBB", 199.0)
        r = self.o.decide("imsi-208930000000001", "video")
        self.assertFalse(r["admitted"])
        self.assertIn("eMBB", r["reason"])
        self.assertEqual(self.o.allocated("2/112233"), 0.0, "URLLC capacity must stay untouched")

    def test_spillover_only_when_explicitly_enabled(self):
        o = load({"CORE": "free5gc", "EXECUTE": "0", "SPILLOVER": "1", "PROMETHEUS_URL": ""})
        fill(o, "eMBB", 199.0)
        r = o.decide("imsi-208930000000001", "video")
        self.assertTrue(r["admitted"])
        self.assertEqual(r["slice"]["name"], "URLLC")

    def test_measured_load_blocks_admission(self):
        self.o.measured_mbps = lambda: {"eMBB": 483.2}
        r = self.o.decide("imsi-208930000000001", "video")
        self.assertFalse(r["admitted"])
        self.assertIn("measured 483.2", r["reason"])

    def test_urllc_capacity_is_enforced(self):
        fill(self.o, "URLLC", 19.5)
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
        self.assertEqual(embb["policy_capacity_mbps"], 200.0)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=2).result.wasSuccessful() else 1)
