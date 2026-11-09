#!/usr/bin/env python3
"""Tests for campaign.py's offline parts: the interleaved order and the arm parsing."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campaign as C  # noqa: E402


class Order(unittest.TestCase):
    def test_rounds_alternate_so_each_arm_runs_first_equally_often(self):
        seq = C.order(["a", "b"], 4)
        self.assertEqual(seq, [(1, "a"), (1, "b"), (2, "b"), (2, "a"), (3, "a"), (3, "b"), (4, "b"), (4, "a")])
        firsts = [arm for i, (_, arm) in enumerate(seq) if i % 2 == 0]
        self.assertEqual(firsts.count("a"), firsts.count("b"))

    def test_added_rounds_continue_the_alternation(self):
        self.assertEqual(C.order(["a", "b"], 2, first=5), [(5, "a"), (5, "b"), (6, "b"), (6, "a")])

    def test_every_arm_once_per_round(self):
        for r, arms in ((1, ["x"]), (3, ["a", "b", "c"])):
            seq = C.order(arms, r)
            for k in range(1, r + 1):
                self.assertEqual(sorted(a for rr, a in seq if rr == k), sorted(arms))


class Arms(unittest.TestCase):
    def test_parse(self):
        arms = C.parse_arms(['cell250={"mode":"sliced","cell_dl_mbps":250}', 'off={"mode":"off"}'])
        self.assertEqual(list(arms), ["cell250", "off"])
        self.assertEqual(arms["cell250"]["cell_dl_mbps"], 250)

    def test_malformed_arm_is_refused(self):
        with self.assertRaises(ValueError):
            C.parse_arms(["cell250"])

    def test_applied_needs_mode_cell_and_no_new_error(self):
        cfg = {"mode": "sliced", "cell_dl_mbps": 600, "cell_ul_mbps": 100}
        st = {"mode": "sliced", "cell_mbps": {"downlink": 600.0, "uplink": 100.0}, "errors": 3}
        self.assertTrue(C.applied(cfg, st, 3))
        self.assertFalse(C.applied(cfg, dict(st, errors=4), 3))
        self.assertFalse(C.applied(cfg, dict(st, cell_mbps={"downlink": 250.0, "uplink": 100.0}), 3))
        self.assertFalse(C.applied(cfg, dict(st, mode="fifo"), 3))


if __name__ == "__main__":
    unittest.main(verbosity=1)
