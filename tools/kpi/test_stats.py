#!/usr/bin/env python3
"""Unit tests for tools/kpi/stats.py (ADR-023), against published reference values.

Each reference value names its source. Where a source gives a closed form rather than a printed
number, the test checks the closed form, or the defining property of the quantity.
"""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stats as S  # noqa: E402

# R. Shier, "Statistics: 2.3 The Mann-Whitney U Test", Mathematics Learning Support Centre, 2004,
# as used in the Examples of scipy.stats.mannwhitneyu (SciPy documentation): U1 = 17, U2 = 3,
# exact two-sided p = 0.1111111111111111, normal approximation p = 0.11134688653314041 with the
# continuity correction and 0.0864107329737 without it.
MALES = [19, 22, 16, 29, 24]
FEMALES = [20, 11, 17, 12]


class MannWhitney(unittest.TestCase):
    def test_reference_exact(self):
        r = S.mann_whitney_u(MALES, FEMALES, method="exact")
        self.assertEqual(r["u1"], 17)
        self.assertEqual(r["u2"], 3)
        self.assertAlmostEqual(r["p"], 0.1111111111111111, places=15)

    def test_reference_asymptotic(self):
        self.assertAlmostEqual(S.mann_whitney_u(MALES, FEMALES, method="asymptotic")["p"],
                               0.11134688653314041, places=12)
        self.assertAlmostEqual(S.mann_whitney_u(MALES, FEMALES, method="asymptotic", use_continuity=False)["p"],
                               0.0864107329737, places=12)

    def test_symmetric_in_its_arguments(self):
        a = S.mann_whitney_u(MALES, FEMALES)
        b = S.mann_whitney_u(FEMALES, MALES)
        self.assertEqual((a["u1"], a["u2"]), (b["u2"], b["u1"]))
        self.assertAlmostEqual(a["p"], b["p"])

    def test_auto_uses_exact_without_ties(self):
        self.assertEqual(S.mann_whitney_u(MALES, FEMALES)["method"], "exact")

    def test_ties_use_the_permutation_test_when_small(self):
        r = S.mann_whitney_u([1, 2, 2, 3], [2, 4, 5])
        self.assertEqual(r["method"], "permutation")
        self.assertEqual(r["u1"] + r["u2"], 12)
        with self.assertRaises(ValueError):
            S.mann_whitney_u([1, 2, 2, 3], [2, 4, 5], method="exact")

    def test_permutation_equals_exact_without_ties(self):
        self.assertAlmostEqual(S.mann_whitney_u(MALES, FEMALES, method="permutation")["p"], 0.1111111111111111)

    def test_permutation_with_ties_is_never_below_the_exact_minimum(self):
        # Two zero-loss runs against two lossy ones: the normal approximation would say p = 0.22,
        # below anything 2-against-2 can give.
        r = S.mann_whitney_u([0.0, 0.0], [0.0288, 0.0128])
        self.assertEqual(r["method"], "permutation")
        self.assertGreaterEqual(r["p"], S.min_exact_p(2, 2) - 1e-12)
        self.assertEqual(S.mann_whitney_u([0.0, 0.0], [0.0, 0.0])["p"], 1.0)

    def test_exact_distribution_is_a_distribution(self):
        # The number of arrangements is C(n1 + n2, n1), and U is symmetric about n1 n2 / 2.
        for n1, n2 in ((1, 1), (2, 3), (4, 4), (5, 4), (7, 10), (12, 12)):
            d = S._u_distribution(n1, n2)
            self.assertEqual(len(d), n1 * n2 + 1)
            self.assertEqual(sum(d), math.comb(n1 + n2, n1))
            self.assertEqual(d, d[::-1])

    def test_total_separation_gives_the_minimum_p(self):
        for n1, n2 in ((2, 2), (2, 3), (3, 3), (4, 4)):
            x = [10.0 + i for i in range(n1)]
            y = [float(i) for i in range(n2)]
            self.assertAlmostEqual(S.mann_whitney_u(x, y)["p"], S.min_exact_p(n1, n2))

    def test_minimum_p_values(self):
        # 2 / C(n1 + n2, n1)
        self.assertAlmostEqual(S.min_exact_p(2, 2), 1 / 3)
        self.assertAlmostEqual(S.min_exact_p(2, 3), 0.2)
        self.assertAlmostEqual(S.min_exact_p(3, 3), 0.1)
        self.assertAlmostEqual(S.min_exact_p(4, 4), 2 / 70)

    def test_empty_sample(self):
        self.assertIsNone(S.mann_whitney_u([], [1, 2])["p"])


class CliffsDelta(unittest.TestCase):
    def test_reference(self):
        # Without ties delta = 2 U1 / (n1 n2) - 1 (Cliff 1993): 2 * 17 / 20 - 1 = 0.7.
        self.assertAlmostEqual(S.cliffs_delta(MALES, FEMALES), 0.7)

    def test_bounds_and_antisymmetry(self):
        self.assertEqual(S.cliffs_delta([5, 6], [1, 2, 3]), 1.0)
        self.assertEqual(S.cliffs_delta([1, 2, 3], [5, 6]), -1.0)
        self.assertEqual(S.cliffs_delta([1, 2], [1, 2]), 0.0)
        self.assertAlmostEqual(S.cliffs_delta(MALES, FEMALES), -S.cliffs_delta(FEMALES, MALES))


class ClopperPearson(unittest.TestCase):
    def test_zero_events_closed_form(self):
        # Clopper-Pearson with x = 0: (0, 1 - (alpha/2)^(1/n)) (Wikipedia, "Binomial proportion
        # confidence interval", Clopper-Pearson interval).
        for n in (1, 10, 300, 1500, 76700):
            lo, hi = S.clopper_pearson(0, n)
            self.assertEqual(lo, 0.0)
            self.assertAlmostEqual(hi, 1 - 0.025 ** (1 / n), places=12)
        self.assertAlmostEqual(S.clopper_pearson(0, 10)[1], 0.30849710781876083, places=12)

    def test_all_events_closed_form(self):
        # x = n: ((alpha/2)^(1/n), 1)
        lo, hi = S.clopper_pearson(10, 10)
        self.assertAlmostEqual(lo, 0.025 ** (1 / 10), places=12)
        self.assertEqual(hi, 1.0)

    def test_defining_property(self):
        # The bounds are where the binomial tails equal alpha/2 (Clopper & Pearson 1934). The
        # case k = 20, n = 400 is the one in the Wikipedia article's code example.
        for k, n in ((20, 400), (1, 10), (3064, 103269), (5, 76700)):
            lo, hi = S.clopper_pearson(k, n)
            self.assertLess(lo, k / n)
            self.assertGreater(hi, k / n)
            self.assertAlmostEqual(S.binom_cdf(k, n, hi), 0.025, places=9)
            self.assertAlmostEqual(1 - S.binom_cdf(k - 1, n, lo), 0.025, places=9)

    def test_one_sided_zero_events(self):
        # One-sided 95 %: (1 - p)^n = 0.05, the equation the rule of three approximates.
        self.assertAlmostEqual(S.clopper_pearson_upper(0, 1500), 1 - 0.05 ** (1 / 1500), places=14)

    def test_binom_cdf_small_case(self):
        # Binomial(10, 0.5): P(X <= 2) = (1 + 10 + 45) / 1024.
        self.assertAlmostEqual(S.binom_cdf(2, 10, 0.5), 56 / 1024, places=14)


class RuleOfThree(unittest.TestCase):
    def test_worked_examples(self):
        # Wikipedia, "Rule of three (statistics)": 1500 subjects with no adverse event -> fewer
        # than 1 in 500; 300 parachutes that all open -> fewer than 1 in 100.
        self.assertAlmostEqual(S.rule_of_three(1500), 1 / 500)
        self.assertAlmostEqual(S.rule_of_three(300), 1 / 100)

    def test_close_to_the_exact_bound(self):
        n = 76700
        self.assertAlmostEqual(S.rule_of_three(n), S.clopper_pearson_upper(0, n), delta=0.01 * 3 / n)

    def test_zero_failure_sample_size(self):
        for p in (1e-3, 1e-4, 1e-5):
            n = S.zero_failure_n(p)
            self.assertLessEqual((1 - p) ** n, 0.05)
            self.assertGreater((1 - p) ** (n - 1), 0.05)
        self.assertEqual(S.zero_failure_n(1e-5), 299572)
        self.assertEqual(S.zero_failure_n(1e-4), 29956)


class Bootstrap(unittest.TestCase):
    def test_median(self):
        self.assertEqual(S.median([3, 1, 2]), 2)
        self.assertEqual(S.median([4, 1, 2, 3]), 2.5)
        self.assertIsNone(S.median([]))

    def test_deterministic_with_a_seed(self):
        xs = [1.2, 3.4, 2.2, 5.0, 4.1, 0.7, 3.3]
        self.assertEqual(S.bootstrap_ci(xs, n_boot=2000, seed=7), S.bootstrap_ci(xs, n_boot=2000, seed=7))

    def test_interval_brackets_the_median(self):
        xs = [1.2, 3.4, 2.2, 5.0, 4.1, 0.7, 3.3, 2.9, 3.0, 1.9]
        lo, hi = S.bootstrap_ci(xs, n_boot=4000)
        self.assertLessEqual(lo, S.median(xs))
        self.assertGreaterEqual(hi, S.median(xs))
        self.assertGreaterEqual(lo, min(xs))
        self.assertLessEqual(hi, max(xs))

    def test_two_values_give_their_range(self):
        # With n = 2 the bootstrap median is min, max or their mean: the interval is the range.
        self.assertEqual(S.bootstrap_ci([6.08, 9.81], n_boot=4000), (6.08, 9.81))

    def test_degenerate_samples(self):
        self.assertEqual(S.bootstrap_ci([5.0, 5.0, 5.0], n_boot=500), (5.0, 5.0))
        self.assertEqual(S.bootstrap_ci([2.5], n_boot=500), (2.5, 2.5))
        self.assertEqual(S.bootstrap_ci([], n_boot=500), (None, None))

    def test_cluster_bootstrap_is_wider_than_pooling(self):
        # Two clusters far apart: pooling as if independent gives a narrow interval; resampling
        # the clusters keeps the between-run spread.
        a = [10.0 + 0.1 * i for i in range(20)]
        b = [20.0 + 0.1 * i for i in range(20)]
        plo, phi = S.bootstrap_ci(a + b, n_boot=3000)
        clo, chi = S.cluster_bootstrap_ci([a, b], n_boot=3000)
        self.assertGreater(chi - clo, phi - plo)

    def test_difference_interval(self):
        lo, hi = S.bootstrap_diff_ci([10, 11, 12, 13, 14], [1, 2, 3, 4, 5], n_boot=3000)
        self.assertGreater(lo, 0)
        self.assertLessEqual(hi, 13)


if __name__ == "__main__":
    unittest.main(verbosity=1)
