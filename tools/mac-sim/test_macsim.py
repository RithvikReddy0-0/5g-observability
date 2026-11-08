#!/usr/bin/env python3
"""Unit tests for the MAC scheduler SIMULATOR (ADR-022). Deterministic, no network, a few seconds:

    python3 tools/mac-sim/test_macsim.py

Each scheduler is checked for its defining property on a small hand-built cell (fixed channels,
full-buffer or hand-placed packets), so a failure points at one rule, not at a statistic.
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import macsim as M  # noqa: E402

FLAT = lambda c: [c] * M.N_RBG  # noqa: E731


def mk_cell(inter, specs, seed=1):
    """specs: list of dicts name, weight, kind, cqis (one CQI list or callable per UE), and optionally
    full (default True), budget_ms, tiers, share, prio."""
    slices, uid = [], 0
    for sp in specs:
        users = []
        for k, cq in enumerate(sp["cqis"]):
            u = M.UE(uid, sp["name"], M.FixedChannel(cq), full=sp.get("full", True),
                     tier=(sp.get("tiers") or [0] * len(sp["cqis"]))[k], budget_ms=sp.get("budget_ms"))
            users.append(u)
            uid += 1
        slices.append(M.Slice(sp["name"], users, M.Intra(users, sp["kind"], tiered=(inter == "priority")),
                              weight=sp["weight"], share=sp.get("share", sp["weight"]),
                              prio=sp.get("prio", len(slices))))
    return M.Cell(slices, inter, seed=seed)


def run(cell, n, arrivals=()):
    return M.simulate(cell, sorted(arrivals), n, 0)


class Carrier(unittest.TestCase):
    def test_cqi_table_is_ts38214_table_5_2_2_1_2(self):
        self.assertEqual(len(M.CQI_TABLE), 16)
        self.assertEqual([r[2] for r in M.CQI_TABLE[1:]],
                         [78, 120, 193, 308, 449, 602, 378, 490, 616, 466, 567, 666, 772, 873, 948])
        for idx, mod, rate, se in M.CQI_TABLE[1:]:
            qm = {"QPSK": 2, "16QAM": 4, "64QAM": 6}[mod]
            self.assertAlmostEqual(se, qm * rate / 1024.0, places=3)   # efficiency = Qm x R
        self.assertEqual(M.cqi_from_snr(1e-3), 0)
        self.assertEqual(M.cqi_from_snr(1e4), 15)
        cq = [M.cqi_from_snr(10 ** (db / 10)) for db in range(-15, 30)]
        self.assertEqual(cq, sorted(cq))

    def test_rbg_layout(self):
        self.assertEqual(sum(M.RBG_PRBS), 106)
        self.assertEqual(M.N_RBG, 14)
        self.assertEqual(M.RBG_PRBS[-1], 2)

    def test_jakes_correlation(self):
        self.assertAlmostEqual(M.bessel_j0(0.0), 1.0)
        self.assertAlmostEqual(M.bessel_j0(2.404825557695773), 0.0, places=6)   # first zero of J0


class IntraSlice(unittest.TestCase):
    def test_round_robin_gives_equal_turns_whatever_the_channel(self):
        cell = mk_cell("static", [dict(name="s", weight=1, kind="rr",
                                       cqis=[FLAT(15), FLAT(3), FLAT(9), FLAT(1)])])
        run(cell, 101)
        turns = [u.rbgs for u in cell.users]
        self.assertLessEqual(max(turns) - min(turns), 1, turns)

    def test_max_ci_picks_the_best_cqi_on_every_rbg(self):
        pats = [[(3 * b + 5 * k) % 15 + 1 for b in range(M.N_RBG)] for k in range(4)]
        cell = mk_cell("static", [dict(name="s", weight=1, kind="maxci", cqis=pats)])
        run(cell, 1)
        for b in range(M.N_RBG):
            self.assertEqual(cell.assigned[b].cq[b], max(p[b] for p in pats), "RBG %d" % b)

    def test_proportional_fair_converges_to_equal_time_shares(self):
        # two UEs, flat channels CQI 15 and CQI 5: PF maximises sum log(throughput), which on
        # constant channels means equal time, so throughputs in the ratio of their rates;
        # Max-C/I starves the weaker UE.
        cell = mk_cell("static", [dict(name="s", weight=1, kind="pf", cqis=[FLAT(15), FLAT(5)])])
        raw = run(cell, 4000)
        a, b = cell.users
        share = a.rbgs / (a.rbgs + b.rbgs)
        self.assertAlmostEqual(share, 0.5, delta=0.02)
        ratio = raw["bits"][a.id] / raw["bits"][b.id]
        self.assertAlmostEqual(ratio, M.SE[15] / M.SE[5], delta=0.3)
        cell = mk_cell("static", [dict(name="s", weight=1, kind="maxci", cqis=[FLAT(15), FLAT(5)])])
        run(cell, 400)
        self.assertEqual(cell.users[1].rbgs, 0)

    def test_proportional_fair_rides_the_peaks_of_a_fading_channel(self):
        # Each UE alternates between CQI 15 and CQI 7, out of phase. PF serves each on its peaks,
        # RR ignores them: PF carries more with the same time shares.
        a = lambda p: FLAT(15 if p % 2 == 0 else 7)  # noqa: E731
        b = lambda p: FLAT(7 if p % 2 == 0 else 15)  # noqa: E731
        res = {}
        for kind in ("pf", "rr"):
            cell = mk_cell("static", [dict(name="s", weight=1, kind=kind, cqis=[a, b])])
            raw = run(cell, 2000)
            res[kind] = sum(raw["bits"].values())
        self.assertGreater(res["pf"], 1.3 * res["rr"])

    def test_m_lwdf_serves_the_older_head_of_line_packet_first(self):
        # both UEs on the same channel; each packet needs every RBG of the slot, so only one goes
        big = M.BITS[1][0] * M.N_RBG
        for kind, older_first in (("mlwdf", True), ("exppf", True)):
            cell = mk_cell("static", [dict(name="u", weight=1, kind=kind, cqis=[FLAT(1), FLAT(1)],
                                           full=False, budget_ms=10.0)])
            young, old = cell.users
            run(cell, 1, arrivals=[(0.0, young.id, big), (-8.0, old.id, big)])
            self.assertGreater(old.rbgs, young.rbgs, kind)
            self.assertEqual(young.rbgs, 0, kind)

    def test_m_lwdf_weighs_delay_against_channel(self):
        # the older packet sits on the worse channel: M-LWDF still serves it once it has waited
        # long enough, Max-C/I never does while the better UE has data
        big = M.BITS[15][0] * M.N_RBG
        for kind, expect_old in (("mlwdf", True), ("maxci", False)):
            cell = mk_cell("static", [dict(name="u", weight=1, kind=kind, cqis=[FLAT(15), FLAT(9)],
                                           full=False, budget_ms=10.0)])
            good, old = cell.users
            run(cell, 1, arrivals=[(0.0, good.id, big), (-9.0, old.id, big)])
            self.assertEqual(old.rbgs > 0, expect_old, kind)


class InterSlice(unittest.TestCase):
    def three(self, inter, **kw):
        return mk_cell(inter, [
            dict(name="eMBB", weight=0.60, kind="pf", cqis=[FLAT(12)] * 3, prio=2),
            dict(name="URLLC", weight=0.10, kind="pf", cqis=[FLAT(12)] * 2, prio=1),
            dict(name="mMTC", weight=0.05, kind="pf", cqis=[FLAT(12)] * 2, prio=3)], **kw)

    def shares(self, cell):
        tot = {}
        for u in cell.users:
            tot[u.slice] = tot.get(u.slice, 0) + u.rbgs
        n = sum(tot.values())
        return {k: v / n for k, v in tot.items()}

    def test_static_split_respects_the_shares(self):
        cell = self.three("static")
        run(cell, 3000)
        sh = self.shares(cell)
        for name, w in (("eMBB", 0.60), ("URLLC", 0.10), ("mMTC", 0.05)):
            self.assertAlmostEqual(sh[name], w / 0.75, delta=0.005, msg=name)

    def test_static_split_leaves_an_idle_slices_rbgs_unused(self):
        cell = mk_cell("static", [dict(name="a", weight=0.5, kind="pf", cqis=[FLAT(12)]),
                                  dict(name="b", weight=0.5, kind="pf", cqis=[FLAT(12)], full=False)])
        raw = run(cell, 200)
        self.assertLess(raw["prb_used"] / (200 * M.N_PRB), 0.6)
        cell = mk_cell("radiosaber", [dict(name="a", weight=0.5, kind="pf", cqis=[FLAT(12)]),
                                      dict(name="b", weight=0.5, kind="pf", cqis=[FLAT(12)], full=False)])
        raw = run(cell, 200)
        self.assertEqual(raw["prb_used"], 200 * M.N_PRB)

    def test_nvs_shares_slots_by_weight(self):
        cell = self.three("nvs")
        run(cell, 4000)
        sh = self.shares(cell)
        for name, w in (("eMBB", 0.60), ("URLLC", 0.10), ("mMTC", 0.05)):
            self.assertAlmostEqual(sh[name], w / 0.75, delta=0.02, msg=name)

    def test_radiosaber_quotas_follow_the_weights(self):
        cell = mk_cell("radiosaber", [
            dict(name="eMBB", weight=0.60, kind="pf", cqis=[[(b * 7 + k) % 15 + 1 for b in range(M.N_RBG)] for k in range(3)]),
            dict(name="URLLC", weight=0.10, kind="pf", cqis=[[(b * 5 + k) % 15 + 1 for b in range(M.N_RBG)] for k in range(2)]),
            dict(name="mMTC", weight=0.05, kind="pf", cqis=[[(b * 3 + k) % 15 + 1 for b in range(M.N_RBG)] for k in range(2)])])
        run(cell, 3000)
        sh = self.shares(cell)
        for name, w in (("eMBB", 0.60), ("URLLC", 0.10), ("mMTC", 0.05)):
            self.assertAlmostEqual(sh[name], w / 0.75, delta=0.005, msg=name)

    def test_radiosaber_beats_static_split_on_a_channel_diverse_cell(self):
        # slice a's UE is good on even RBGs, slice b's on odd ones; equal weights. Static gives
        # each slice a contiguous half (half good, half bad); RadioSaber gives each its good RBGs.
        ga = [15 if b % 2 == 0 else 3 for b in range(M.N_RBG)]
        gb = [3 if b % 2 == 0 else 15 for b in range(M.N_RBG)]
        res, sh = {}, {}
        for inter in ("static", "quota-seq", "radiosaber"):
            cell = mk_cell(inter, [dict(name="a", weight=0.5, kind="pf", cqis=[ga]),
                                   dict(name="b", weight=0.5, kind="pf", cqis=[gb])])
            raw = run(cell, 400)
            res[inter] = sum(raw["bits"].values())
            sh[inter] = self.shares(cell)["a"]
        self.assertGreaterEqual(res["radiosaber"], res["static"])
        self.assertGreater(res["radiosaber"], 1.4 * res["static"])
        self.assertGreater(res["radiosaber"], 1.4 * res["quota-seq"])
        self.assertAlmostEqual(sh["radiosaber"], 0.5, delta=0.01)        # and still fair

    def test_radiosaber_asks_the_slice_again_after_a_grant(self):
        # slice a has one UE with a small backlog, best on every RBG. Once its backlog is covered
        # its scheduler has nobody left, and the remaining RBGs must go to slice b.
        small = M.BITS[15][0]
        cell = mk_cell("radiosaber", [dict(name="a", weight=0.5, kind="pf", cqis=[FLAT(15)], full=False),
                                      dict(name="b", weight=0.5, kind="pf", cqis=[FLAT(4)])])
        run(cell, 1, arrivals=[(0.0, 0, small)])
        self.assertEqual(cell.users[0].rbgs, 1)
        self.assertEqual(cell.users[1].rbgs, M.N_RBG - 1)

    def test_priority_serves_urllc_first_and_critical_devices_before_standard(self):
        # HTB semantics: URLLC (prio 1) takes its guarantee (floor(0.10 x 14) = 1 RBG), eMBB its own
        # (floor(0.60 x 14) = 8), then URLLC borrows all that is left before eMBB may. Inside URLLC
        # the critical device (tier 1) is served before the standard one.
        need = M.BITS[2][0] * 6
        cell = mk_cell("priority", [
            dict(name="eMBB", weight=0.60, kind="pf", cqis=[FLAT(15)], prio=2),
            dict(name="URLLC", weight=0.10, kind="pf", cqis=[FLAT(2), FLAT(2)], full=False,
                 budget_ms=10.0, tiers=[0, 1], prio=1)])
        embb, std, crit = cell.users
        run(cell, 1, arrivals=[(0.0, std.id, need), (0.0, crit.id, need)])
        self.assertEqual(crit.rbgs, 6)
        self.assertEqual(std.rbgs, 0)
        self.assertEqual(embb.rbgs, 8)
        self.assertIs(cell.assigned[0], crit)

    def test_priority_guarantees_the_lowest_slice_its_share(self):
        cell = mk_cell("priority", [
            dict(name="URLLC", weight=0.10, share=0.10, kind="pf", cqis=[FLAT(12)], prio=1),
            dict(name="mMTC", weight=0.05, share=0.20, kind="pf", cqis=[FLAT(12)], prio=3)])
        run(cell, 1000)
        sh = self.shares(cell)
        self.assertAlmostEqual(sh["mMTC"], 0.20, delta=0.01)


class Study(unittest.TestCase):
    def test_runs_are_deterministic(self):
        a = M.run_one(("urllc-flood", "radiosaber", "default", 3, 0.3, 0.1))
        b = M.run_one(("urllc-flood", "radiosaber", "default", 3, 0.3, 0.1))
        a.pop("wall_s")
        b.pop("wall_s")
        self.assertEqual(a, b)

    def test_every_scheduler_sees_the_same_radio_and_packets(self):
        def fingerprint(inter, scenario="overload"):
            cell, arr = M.build(scenario, inter, "default", 5, 0.2)
            return [round(u.ch.snr, 9) for u in cell.users], [u.ch.at(7) for u in cell.users[:5]], len(arr)
        ref = fingerprint("static")
        for inter in M.INTER_KINDS:
            self.assertEqual(fingerprint(inter), ref, inter)
        # normal and overload share the radio (only eMBB's traffic differs)
        self.assertEqual(fingerprint("static", "normal")[:2], ref[:2])

    def test_confidence_interval(self):
        mu, hw = M.ci95([1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(mu, 3.0)
        self.assertAlmostEqual(hw, 2.776 * math.sqrt(2.5) / math.sqrt(5), places=6)
        self.assertAlmostEqual(M.jain([1, 1, 1, 1]), 1.0)
        self.assertAlmostEqual(M.jain([1, 0, 0, 0]), 0.25)
        self.assertAlmostEqual(M.quantile([1, 2, 3, 4, 5], 0.5), 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
