#!/usr/bin/env python3
"""
test_netorch.py — the network orchestrator's decisions, checked with no Docker and no stack.

  * inventory     slices, their network functions and UE counts come from slices.env + compose
  * closed loop   activate an on-demand slice when the plan needs it; deactivate only after
                  IDLE_SECONDS without demand and MIN_UP after its last change; never touch a
                  slice that is not on-demand
  * rollback      a failed activation stops exactly what it started and leaves the slice dormant;
                  UEs are parked again only if they had been brought up

Run: python3 tools/network-orchestrator/test_netorch.py        (CI runs it too)
"""

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load(env=None):
    env = env or {}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("netorch_%d" % id(env), os.path.join(HERE, "netorch.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    mod.orch = lambda *a, **k: {}          # no slice orchestrator
    mod.OPS_LOG = os.devnull
    return mod


def plan(**demand):
    return {"slices": [{"slice": n, "needed": demand.get(n, 0) > 0, "demand_mbps": demand.get(n, 0.0)}
                       for n in ("eMBB", "URLLC", "mMTC")]}


def states(**st):
    return {n: {"state": st.get(n, "ACTIVE"), "settled_state": st.get(n, "ACTIVE")}
            for n in ("eMBB", "URLLC", "mMTC")}


class Inventory(unittest.TestCase):
    def test_three_slices_with_their_functions(self):
        o = load()
        self.assertEqual([s["name"] for s in o.SLICES], ["eMBB", "URLLC", "mMTC"])
        m = o.BY_NAME["mMTC"]
        self.assertEqual((m["upf"], m["dn"], m["gnb"]), ("o5gs-upf-mmtc", "o5gs-dn-mmtc", "o5gs-gnb-mmtc"))
        self.assertEqual(m["ues"], 70)
        self.assertEqual(m["gateway"], "10.47.0.1")
        self.assertEqual(m["pool"], "10.47.")
        self.assertEqual(m["snssai_label"], "3-334455")


class ClosedLoop(unittest.TestCase):
    def setUp(self):
        self.o = load({"ON_DEMAND": "mMTC", "IDLE_SECONDS": "120", "MIN_UP": "60"})
        self.calls = []
        self.o.submit = lambda kind, name, reason="", **a: (self.calls.append((kind, name)) or object(), None)

    def test_dormant_slice_with_demand_is_activated(self):
        d = self.o.auto_tick(now=1000, plan=plan(mMTC=0.2), states=states(mMTC="DORMANT"))
        self.assertEqual([(k, n) for k, n, _ in d], [("activate", "mMTC")])

    def test_active_slice_is_kept_until_idle_long_enough(self):
        self.o.auto_tick(now=1000, plan=plan(mMTC=0.2), states=states())
        self.assertEqual(self.o.auto_tick(now=1100, plan=plan(), states=states()), [], "idle 100 s < 120 s")
        d = self.o.auto_tick(now=1121, plan=plan(), states=states())
        self.assertEqual([(k, n) for k, n, _ in d], [("deactivate", "mMTC")])

    def test_not_deactivated_soon_after_a_change(self):
        self.o.loop["last_need"]["mMTC"] = 0
        self.o.last_change["mMTC"] = 980
        self.assertEqual(self.o.auto_tick(now=1000, plan=plan(), states=states()), [], "changed 20 s ago")

    def test_slices_not_on_demand_are_never_switched(self):
        d = self.o.auto_tick(now=5000, plan=plan(), states=states(URLLC="DORMANT", eMBB="ACTIVE"))
        self.assertFalse(any(n in ("eMBB", "URLLC") for _, n, _ in d))

    def test_degraded_or_busy_slice_is_left_alone(self):
        self.assertEqual(self.o.auto_tick(now=1000, plan=plan(mMTC=1), states=states(mMTC="DEGRADED")), [])
        self.assertEqual(self.o.auto_tick(now=1000, plan=plan(mMTC=1), states=states(mMTC="ACTIVATING")), [])

    def test_no_plan_no_action(self):
        self.o.orch = lambda *a, **k: None
        self.assertEqual(self.o.auto_tick(now=1000, states=states(mMTC="DORMANT")), [])


class Rollback(unittest.TestCase):
    def setUp(self):
        self.o = load({"ACTIVATE_DEADLINE": "100"})
        self.cmds = []
        self.ue_ops = []
        o = self.o
        o.running = lambda c: False
        o.inspect = lambda c, fmt: "10.53.0.9" if "IPAddress" in fmt else "2026-10-08T00:00:00Z"

        def sh(args, timeout=30, stdin=None):
            self.cmds.append(" ".join(args))
            return 0, ""
        o.sh = sh
        o.ue_slice = lambda op, name="": self.ue_ops.append(op) or ["%s mMTC 70 70 5" % op]

    def run_op(self, fail_at):
        o = self.o

        def wait_for(check, timeout, what):
            if fail_at in what:
                raise o.Failure("%s not reached" % what)
        o.wait_for = wait_for
        op = o.Op("activate", "mMTC", "test")
        o.activate(op)
        return op

    def test_gnb_failure_stops_what_was_started_and_does_not_touch_ues(self):
        op = self.run_op("NG Setup")
        self.assertEqual(op.result, "rolled_back")
        stops = [c.split()[-1] for c in self.cmds if c.startswith("docker stop")]
        self.assertEqual(stops, ["o5gs-gnb-mmtc", "o5gs-dn-mmtc", "o5gs-upf-mmtc"], "all three, in reverse order")
        self.assertEqual(self.ue_ops, [], "UEs were never unparked, so nothing to park")

    def test_datapath_failure_parks_the_ues_again(self):
        self.o.datapath = lambda s: (_ for _ in ()).throw(self.o.Failure("no reply"))
        op = self.run_op("never")
        self.assertEqual(op.result, "rolled_back")
        self.assertEqual(self.ue_ops, ["unpark", "park"])

    def test_success_runs_every_step(self):
        self.o.datapath = lambda s: "ok"
        op = self.run_op("never")
        self.assertEqual(op.result, "done")
        self.assertEqual([s["step"] for s in op.steps], ["upf", "data network", "gnb", "ues", "data path", "admit"])

    def test_already_running_functions_are_not_stopped_on_rollback(self):
        self.o.running = lambda c: c == "o5gs-upf-mmtc"
        op = self.run_op("NG Setup")
        self.assertEqual(op.result, "rolled_back")
        stops = [c.split()[-1] for c in self.cmds if c.startswith("docker stop")]
        self.assertEqual(stops, ["o5gs-gnb-mmtc", "o5gs-dn-mmtc"], "the UPF was already running")


class Scale(unittest.TestCase):
    def setUp(self):
        self.o = load()
        self.quota = {}
        o = self.o

        def sh(args, timeout=30, stdin=None):
            if args[:2] == ["docker", "update"] and self.apply:
                self.quota[args[-1]] = float(args[3])
            return 0, ""
        o.sh = sh
        o.cpus_of = lambda c: self.quota.get(c, 0.0)
        self.apply = True

    def test_zero_means_every_host_core(self):
        op = self.o.Op("scale", "eMBB", "test", cpus=0)
        self.o.scale(op)
        self.assertEqual(op.result, "done")
        self.assertEqual(self.quota["o5gs-gnb-embb"], float(self.o.HOST_CPUS))

    def test_a_quota_that_did_not_take_is_a_failure(self):
        # `docker update --cpus 0` was accepted and changed nothing, while the op said "done".
        self.apply = False
        op = self.o.Op("scale", "eMBB", "test", cpus=1)
        self.o.scale(op)
        self.assertEqual(op.result, "failed")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=2).result.wasSuccessful() else 1)
