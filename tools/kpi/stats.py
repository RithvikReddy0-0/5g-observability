#!/usr/bin/env python3
"""
stats.py — the statistics the KPI comparisons need, standard library only (ADR-023).

Why this exists: on 2026-11-08 a single run from October was compared with a single run from
November and read as "the scheduler lifted URLLC from 0.90 to 1.00". It had not; run-to-run
variation on this laptop is as large as most effects we look for. A difference between two arms
is reported as supported only when a test says so, and a reliability figure only with the bound
its sample size allows.

    median(xs)                          sample median
    bootstrap_ci(xs, stat)              percentile bootstrap confidence interval
    cluster_bootstrap_ci(groups, stat)  two-stage bootstrap: resample runs, then values in a run
    bootstrap_diff_ci(x, y, stat)       CI of stat(x) - stat(y)
    mann_whitney_u(x, y)                Mann-Whitney U test, exact (no ties, small n) or normal
    min_exact_p(n1, n2)                 smallest two-sided p the exact test can ever give
    cliffs_delta(x, y)                  effect size: P(X > Y) - P(X < Y)
    clopper_pearson(k, n)               exact two-sided binomial CI
    clopper_pearson_upper(k, n)         exact one-sided upper bound
    rule_of_three(n)                    3/n: approximate one-sided 95 % upper bound when k = 0
    zero_failure_n(p_max, conf)         trials with no failure needed to show p <= p_max

References for the definitions (and the test values in test_stats.py):
  Mann-Whitney U, exact and normal approximation with tie and continuity correction: as
    implemented by SciPy's scipy.stats.mannwhitneyu; reference example from R. Shier,
    "Statistics: 2.3 The Mann-Whitney U Test", Mathematics Learning Support Centre, 2004.
  Cliff's delta: N. Cliff, "Dominance statistics: ordinal analyses to answer ordinal
    questions", Psychological Bulletin 114(3), 1993; delta = 2U/(n1 n2) - 1 when there are no ties.
  Clopper-Pearson: C. J. Clopper, E. S. Pearson, Biometrika 26(4), 1934; closed form for k = 0
    is (0, 1 - (alpha/2)^(1/n)).
  Rule of three: J. A. Hanley, A. Lippman-Hand, "If nothing goes wrong, is everything all
    right?", JAMA 249(13), 1983: (1 - p)^n = 0.05 gives p ~= 3/n.
  Percentile bootstrap: B. Efron, R. J. Tibshirani, "An Introduction to the Bootstrap", 1993.
"""

import math
import random
import statistics

DEFAULT_BOOT = 10000
DEFAULT_SEED = 20261108


# ------------------------------------------------------------------ location and its uncertainty

def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _percentile(sorted_vals, q):
    """Linear interpolation between order statistics (the 'type 7' definition)."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def bootstrap_ci(xs, stat=median, n_boot=DEFAULT_BOOT, alpha=0.05, seed=DEFAULT_SEED):
    """Percentile bootstrap CI of stat(xs) -> (low, high).

    With very few values the interval cannot be wider than the data: for n = 2 the bootstrap
    median CI is simply [min, max]. It is then a description of the spread, not a confidence
    statement; callers report n beside it."""
    xs = [x for x in xs if x is not None]
    if not xs:
        return (None, None)
    if len(xs) == 1:
        return (xs[0], xs[0])
    rng = random.Random(seed)
    n = len(xs)
    reps = sorted(stat([xs[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    return (_percentile(reps, alpha / 2), _percentile(reps, 1 - alpha / 2))


def cluster_bootstrap_ci(groups, stat=median, n_boot=DEFAULT_BOOT, alpha=0.05, seed=DEFAULT_SEED):
    """Two-stage bootstrap for values clustered in groups (devices within runs).

    Each replicate resamples the groups with replacement, then the values within each chosen
    group, pools them and applies stat. This keeps the run-to-run variation in the interval,
    which pooling every device of every run as if independent would hide. With 2-3 runs per arm
    the interval is still only as good as 2-3 clusters allow."""
    groups = [[v for v in g if v is not None] for g in groups]
    groups = [g for g in groups if g]
    if not groups:
        return (None, None)
    rng = random.Random(seed)
    reps = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(groups)):
            g = groups[rng.randrange(len(groups))]
            pool.extend(g[rng.randrange(len(g))] for _ in range(len(g)))
        reps.append(stat(pool))
    reps.sort()
    return (_percentile(reps, alpha / 2), _percentile(reps, 1 - alpha / 2))


def bootstrap_diff_ci(x, y, stat=median, n_boot=DEFAULT_BOOT, alpha=0.05, seed=DEFAULT_SEED):
    """Percentile bootstrap CI of stat(x) - stat(y), resampling each sample independently."""
    x = [v for v in x if v is not None]
    y = [v for v in y if v is not None]
    if not x or not y:
        return (None, None)
    rng = random.Random(seed)
    reps = sorted(stat([x[rng.randrange(len(x))] for _ in x]) - stat([y[rng.randrange(len(y))] for _ in y])
                  for _ in range(n_boot))
    return (_percentile(reps, alpha / 2), _percentile(reps, 1 - alpha / 2))


# ------------------------------------------------------------------ two-sample comparison

def _ranks(values):
    """Mid-ranks (1-based) and the tie group sizes."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    ties = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = r
        if j > i:
            ties.append(j - i + 1)
        i = j + 1
    return ranks, ties


def _u_distribution(n1, n2):
    """Number of the C(n1 + n2, n1) equally likely arrangements giving each U = 0 .. n1*n2 (no
    ties). These are the coefficients of the Gaussian binomial coefficient [n1 + n2 choose n1]_q,
    built as prod_{i=1..n1} (1 - q^(n2 + i)) / (1 - q^i); every partial product is a polynomial,
    so the integer division is exact."""
    poly = [1]
    for i in range(1, n1 + 1):
        k = n2 + i
        poly = poly + [0] * k                     # multiply by (1 - q^k)
        for d in range(len(poly) - 1, k - 1, -1):
            poly[d] -= poly[d - k]
        for d in range(i, len(poly)):             # divide by (1 - q^i)
            poly[d] += poly[d - i]
        while len(poly) > 1 and poly[-1] == 0:
            poly.pop()
    return poly + [0] * (n1 * n2 + 1 - len(poly))


def _norm_sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


PERMUTATION_LIMIT = 200000


def _permutation_p(ranks, n1, u_obs):
    """Exact two-sided p over every split of the pooled mid-ranks into n1 + n2 (valid with ties):
    P(|U - mu| >= |u_obs - mu|) under the null that the labels are exchangeable."""
    import itertools
    n = len(ranks)
    n2 = n - n1
    mu = n1 * n2 / 2.0
    obs = abs(u_obs - mu) - 1e-9
    hits = total = 0
    for idx in itertools.combinations(range(n), n1):
        u = sum(ranks[i] for i in idx) - n1 * (n1 + 1) / 2.0
        total += 1
        if abs(u - mu) >= obs:
            hits += 1
    return hits / float(total)


def mann_whitney_u(x, y, method="auto", use_continuity=True):
    """Two-sided Mann-Whitney U test of x against y.

    Returns {"u1", "u2", "p", "method", "n1", "n2"} where u1 is the U statistic of x (the number
    of pairs with x > y, ties counting one half). method:
      "exact"        exact null distribution of U (no ties only)
      "permutation"  exact, by enumerating every split of the mid-ranks (handles ties; small n)
      "asymptotic"   normal approximation with tie correction (continuity correction by default)
      "auto"         exact without ties and n1, n2 <= 30; permutation with ties when there are
                     at most PERMUTATION_LIMIT splits; otherwise asymptotic.
    The normal approximation is not used for tiny samples: with 2 runs against 2 it can return
    p = 0.22 where no exact outcome is below 0.33."""
    x = [v for v in x if v is not None]
    y = [v for v in y if v is not None]
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return {"u1": None, "u2": None, "p": None, "method": None, "n1": n1, "n2": n2}
    ranks, ties = _ranks(x + y)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    if method == "auto":
        if not ties and n1 <= 30 and n2 <= 30:
            method = "exact"
        elif math.comb(n1 + n2, n1) <= PERMUTATION_LIMIT:
            method = "permutation"
        else:
            method = "asymptotic"
    if method == "permutation":
        p = _permutation_p(ranks, n1, u1)
    elif method == "exact":
        if ties:
            raise ValueError("the exact distribution assumes no ties; use method='asymptotic'")
        counts = _u_distribution(n1, n2)
        total = float(sum(counts))
        u = int(round(max(u1, u2)))
        p_upper = sum(counts[u:]) / total          # P(U >= max(u1, u2))
        p = min(1.0, 2.0 * p_upper)
    else:
        n = n1 + n2
        mu = n1 * n2 / 2.0
        tie_term = sum(t ** 3 - t for t in ties) / (n * (n - 1)) if n > 1 else 0.0
        s = math.sqrt(n1 * n2 / 12.0 * ((n + 1) - tie_term))
        if s == 0:
            p = 1.0
        else:
            z = (max(u1, u2) - mu - (0.5 if use_continuity else 0.0)) / s
            p = min(1.0, 2.0 * _norm_sf(z))
    return {"u1": u1, "u2": u2, "p": p, "method": method, "n1": n1, "n2": n2}


def min_exact_p(n1, n2):
    """Smallest two-sided p-value the exact Mann-Whitney test can produce with n1 and n2 values:
    total separation of the samples, 2 / C(n1 + n2, n1). With 2 runs against 2 it is 0.33, with
    2 against 3 it is 0.20, with 3 against 3 it is 0.10: no outcome can reach 0.05."""
    if n1 <= 0 or n2 <= 0:
        return None
    return min(1.0, 2.0 / math.comb(n1 + n2, n1))


def cliffs_delta(x, y):
    """Cliff's delta = (#(x > y) - #(x < y)) / (n1 n2), in [-1, 1]. +1: every x exceeds every y."""
    x = [v for v in x if v is not None]
    y = [v for v in y if v is not None]
    if not x or not y:
        return None
    gt = lt = 0
    for a in x:
        for b in y:
            if a > b:
                gt += 1
            elif a < b:
                lt += 1
    return (gt - lt) / float(len(x) * len(y))


# ------------------------------------------------------------------ proportions (loss, reliability)

def _log_binom_pmf(k, n, p):
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log1p(-p))


def binom_cdf(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p), summed in log space (exact up to float rounding)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    logs = [_log_binom_pmf(i, n, p) for i in range(k + 1)]
    m = max(logs)
    if m == -math.inf:
        return 0.0
    return min(1.0, math.exp(m) * sum(math.exp(v - m) for v in logs))


def _solve_decreasing(f, target, lo=0.0, hi=1.0, iters=200):
    """p in [lo, hi] with f(p) = target, f decreasing in p (bisection)."""
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-15:
            break
    return (lo + hi) / 2.0


def clopper_pearson_upper(k, n, conf=0.95):
    """Exact one-sided upper confidence bound on a binomial proportion: the p at which
    P(X <= k; n, p) = 1 - conf. For k = 0 this is 1 - (1 - conf)^(1/n)."""
    if n <= 0:
        return None
    if k >= n:
        return 1.0
    a = 1.0 - conf
    if k == 0:
        return 1.0 - a ** (1.0 / n)
    return _solve_decreasing(lambda p: binom_cdf(k, n, p), a, lo=k / float(n), hi=1.0)


def clopper_pearson(k, n, alpha=0.05):
    """Exact two-sided (1 - alpha) Clopper-Pearson interval -> (low, high)."""
    if n <= 0:
        return (None, None)
    # Lower bound: P(X >= k; n, p) = alpha/2, and P(X >= k) = 1 - P(X <= k-1) increases with p.
    lo = 0.0 if k == 0 else _solve_increasing(lambda p: 1.0 - binom_cdf(k - 1, n, p), alpha / 2.0,
                                              0.0, k / float(n))
    hi = 1.0 if k == n else clopper_pearson_upper(k, n, conf=1.0 - alpha / 2.0)
    return (lo, hi)


def _solve_increasing(f, target, lo, hi, iters=200):
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-15:
            break
    return (lo + hi) / 2.0


def rule_of_three(n):
    """Approximate one-sided 95 % upper bound on a rate after n trials with no event."""
    return None if n <= 0 else 3.0 / n


def zero_failure_n(p_max, conf=0.95):
    """Smallest n such that n trials without a single failure bound the failure rate below p_max
    at the given one-sided confidence: (1 - p_max)^n <= 1 - conf."""
    return int(math.ceil(math.log(1.0 - conf) / math.log1p(-p_max)))
