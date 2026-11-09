#!/usr/bin/env python3
"""Tests for kpi_collector.py's run tally: bounded, cheap to replace, counts exact past the cap."""

import array
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kpi_collector as K  # noqa: E402


class RunTally(unittest.TestCase):
    def test_samples_stop_at_the_cap_but_counts_and_loss_stay_exact(self):
        t = K.Tally(max_samples=5)
        for seq in range(10):
            if seq != 7:                       # one packet lost
                t.add(21, 1, seq, 2.0 + seq)
        st = t.stats()
        self.assertEqual(len(t.owd), 5)
        self.assertTrue(st["samples_truncated"])
        self.assertEqual((st["received"], st["expected"]), (9, 10))
        self.assertAlmostEqual(st["loss_ratio"], 0.1)
        self.assertEqual(st["owd_ms"]["n"], 5)

    def test_under_the_cap_nothing_is_truncated(self):
        t = K.Tally(max_samples=100)
        for seq in range(10):
            t.add(21, 1, seq, 1.0)
        self.assertFalse(t.stats()["samples_truncated"])
        self.assertEqual(len(t.owd), 10)
        self.assertEqual(len(t.jitter), 9)

    def test_samples_are_flat_arrays(self):
        # 8 bytes a sample, freed in one step: a list held a float object per sample (~32 bytes),
        # and freeing 19 million of them under the lock stalled the receive loop for ~9 s.
        t = K.Tally(K.MAX_RUN_SAMPLES)
        self.assertIsInstance(t.owd, array.array)
        self.assertIsInstance(t.jitter, array.array)

    def test_a_full_run_tally_is_freed_quickly(self):
        t = K.Tally(K.MAX_RUN_SAMPLES)
        t.owd = array.array("d", [1.5]) * K.MAX_RUN_SAMPLES
        t.jitter = array.array("d", [0.5]) * K.MAX_RUN_SAMPLES
        start = time.perf_counter()
        del t
        self.assertLess(time.perf_counter() - start, 0.5)

    def test_socket_drops_read_from_proc_net_udp(self):
        # Lines as /proc/net/udp printed them in o5gs-dn-urllc on 2026-11-09 (port 0x1518 = 5400):
        # the last column is the datagrams dropped at that socket, 4280 = that run's lost packets.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".udp", delete=False) as f:
            f.write("   sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n"
                    "  544: 0B00007F:CD7E 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 16734977 2 0000000000000000 0\n"
                    " 2490: 00000000:1518 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 16735031 2 0000000000000000 4280\n")
        try:
            self.assertEqual(K.socket_drops(5400, f.name), 4280)
            self.assertEqual(K.socket_drops(52606, f.name), 0)
            self.assertIsNone(K.socket_drops(9999, f.name))
        finally:
            os.unlink(f.name)
        self.assertIsNone(K.socket_drops(5400, "/no/such/file"))

    def test_the_process_wide_run_tally_is_capped(self):
        self.assertEqual(K.run.max_samples, K.MAX_RUN_SAMPLES)
        self.assertGreaterEqual(K.MAX_RUN_SAMPLES, 660 * 500)   # a 660 s reliability run fits


if __name__ == "__main__":
    unittest.main(verbosity=1)
