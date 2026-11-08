# Statistics of the same-day comparison, 2026-11-08 — what the runs support

[ADR-023](../../adr/ADR-023-standards-and-statistics.md). Earlier on 2026-11-08 a single run was
compared with a single run from October and read as "the scheduler lifted URLLC from 0.90 to
1.00". It had not. This page applies `tools/kpi/stats.py` to the nine runs of the
[same-day comparison](README.md#same-day-comparison-2026-11-08--what-the-packet-scheduler-does-at-nominal-load)
and says which of its readings a test supports, which it cannot, and why. Nothing was re-run:
everything below is computed from the runs' `instances.jsonl`.

Reproduce (from the repository root):

```sh
E=docs/evidence/open5gs-kpi
python3 tools/kpi/compare_runs.py --markdown \
  --arm off=$E/20261108-130046Z,$E/20261108-131657Z \
  --arm sliced250=$E/20261108-130550Z,$E/20261108-131207Z \
  --arm sliced600=$E/20261108-133106Z,$E/20261108-133647Z \
  --arm fifo600=$E/20261108-140404Z,$E/20261108-141118Z
# sensitivity: the morning sliced run added, and all scheduled runs against off
python3 tools/kpi/compare_runs.py --markdown --baseline off \
  --arm off=$E/20261108-130046Z,$E/20261108-131657Z \
  --arm sliced250x3=$E/20261108-092934Z,$E/20261108-130550Z,$E/20261108-131207Z \
  --arm scheduled_all=$E/20261108-092934Z,$E/20261108-130550Z,$E/20261108-131207Z,$E/20261108-133106Z,$E/20261108-133647Z,$E/20261108-140404Z,$E/20261108-141118Z
python3 tools/standards/check_nest.py --run $E/20261108-130550Z     # one run against the NESTs
```

## The design, and three things that limit it

| Arm | Runs | Started (local) | Notes |
|---|---|---|---|
| off | `130046Z`, `131657Z` | 13:02, 13:18 | ABBA with sliced250 |
| sliced250 | `130550Z`, `131207Z` | 13:08, 13:13 | ABBA with off |
| sliced250 (morning) | `092934Z` | 09:32 | **different commit** (`80dc4ac`, not `fbd68fb`) and 3.5 h earlier: used only as a sensitivity check |
| sliced600 | `133106Z`, `133647Z` | 13:32, 13:38 | run as a block |
| fifo600 | `140404Z`, `141118Z` | 14:06, 14:12 | run as a block, half an hour after sliced600 |

1. **Two runs per arm.** The run is the experimental unit (the scheduler mode is set per run), and
   with 2 runs against 2 the exact Mann–Whitney test cannot give a p-value below **0.333**, whatever
   the data. Smallest two-sided p by runs per arm: 2 v 2 = 0.333, 2 v 3 = 0.200, 3 v 3 = 0.100,
   2 v 7 = 0.056, 3 v 4 = 0.057, **4 v 4 = 0.029**, 5 v 5 = 0.008. Even all seven scheduled runs
   against the two `off` runs cannot reach 0.05. **No per-run comparison in these nine runs can be
   statistically significant**, and none is.
2. **The FIFO runs measured URLLC over ~210 s, not 150 s.** URLLC's packet counts imply windows of
   ~213 s and ~207 s (10 devices × 50 packets/s). A stuck eMBB download made the runner wait up to
   60 s more before reading the collectors, so FIFO's URLLC statistics include about a minute
   **without** the eMBB load: its latency percentiles are diluted (optimistic) and its loss is spread
   over more packets. `compare_runs.py` now flags this ("URLLC window check").
3. **The 600 Mbps arms were not interleaved.** sliced600 ran before fifo600; time-of-day drift on
   this laptop (the README's run-to-run note) is confounded with the arm. Only off/sliced250 was
   counterbalanced (ABBA).

## Per run: medians, spread, effect sizes

Median [bootstrap interval] per arm. With n = 2 the interval is simply the two runs' range — a
description of spread, not a 95 % confidence interval.

| KPI | off (n=2) | sliced250 (n=2) | sliced600 (n=2) | fifo600 (n=2) |
|---|---|---|---|---|
| URLLC latency mean (ms) | 1.89 [1.88, 1.91] | 2.15 [2.14, 2.17] | 1.76 [1.67, 1.85] | 1.64 [1.56, 1.71] |
| URLLC latency p95 (ms) | 4.06 [3.14, 4.99] | 5.66 [5.57, 5.76] | 4.35 [3.88, 4.81] | 4.10 [3.89, 4.31] |
| URLLC latency p99 (ms) | 7.95 [6.08, 9.81] | 13.5 [12.2, 14.7] | 12.2 [10.4, 14.0] | 8.45 [8.02, 8.89] |
| URLLC latency max (ms) | 230 [51.1, 410] | 92.0 [64.6, 119] | 78.7 [62.4, 94.9] | 108 [105, 112] |
| URLLC jitter, mean abs. IPDV (ms) | 0.91 [0.75, 1.08] | 1.39 [1.37, 1.41] | 1.04 [1.00, 1.07] | 0.82 [0.77, 0.88] |
| URLLC loss | 0 % | 0 % | 0 % | 2.08 % [1.28 %, 2.88 %] |
| eMBB DL p5 per device (Mbps) | 18.1 [15.1, 21.1] | 10.6 [10.5, 10.6] | 13.5 [12.1, 14.9] | 14.4 [13.9, 14.9] |
| eMBB DL aggregate (Mbps) | 516 [483, 548] | 215 [214, 216] | 440 [436, 444] | 406 [399, 414] |
| eMBB downloads completed (of 20) | 20 | 20 | 20 | 18.5 [18, 19] |
| mMTC report loss | 0 % | 0 % | 0 % | 0.27 % [0 %, 0.54 %] |
| Overall score | 0.87 [0.86, 0.88] | 0.75 [0.75, 0.76] | 0.83 [0.83, 0.83] | 0.71 [0.66, 0.77] |

Cliff's delta between arms is ±1.00 for most of these — every run of one arm above every run of
the other — but at n = 2 a delta of ±1 happens by chance with probability 1/3 (two-sided).

## Per device: many samples, few independent ones

`instances.jsonl` keeps each device's own result (eMBB download rate, URLLC and mMTC mean latency,
URLLC loss). These are 20–140 values per arm, but devices of one run share the host, the eMBB load
and the moment: they are clustered by run. They are summarised with a **two-stage bootstrap**
(resample runs, then devices), and their pooled Mann–Whitney p-values — tiny, e.g. 1.4·10⁻¹⁴ for
eMBB rate off vs sliced250 — are **not** evidence about the arm: they treat 2 runs as 40. What the
per-device view does show is how consistent the ordering is inside the runs.

| KPI | off | sliced250 | sliced600 | fifo600 |
|---|---|---|---|---|
| eMBB DL per device, median [cluster CI] (Mbps) | 25.3 [22.0, 28.1] | 10.8 [10.7, 10.8] | 19.4 [18.8, 24.7] | 21.4 [20.0, 23.4] (37 devices) |
| URLLC mean latency per device (ms) | 1.90 [1.85, 1.94] | 2.15 [2.14, 2.19] | 1.77 [1.66, 1.88] | 1.65 [1.55, 1.73] |
| mMTC mean latency per device (ms) | 1.47 [1.32, 1.62] | 1.73 [1.65, 1.93] | 1.56 [1.47, 1.73] | 1.56 [1.48, 1.67] |

Per-packet delays are not stored (the collector keeps counts and percentiles), so latency tails can
only be compared per run.

## The README's readings, one by one

| Reading (README, same-day comparison) | Statistical status | Why |
|---|---|---|
| 1. `off` (no cell) scores best, 0.86–0.88 | **Consistent, not testable** | Overall score: Cliff's δ = +1 against every other arm; p = 0.333 = the minimum possible at 2 v 2. Also against all seven scheduled runs pooled: δ = −1.00, p = 0.056 = the minimum at 7 v 2. |
| 2. At 600 Mbps, sliced beats FIFO (URLLC loss, completed downloads, overall) | **Not testable per run; the loss difference is real but is two events, not 4 386 packets** | URLLC loss 0 of 153 403 (sliced600) against 4 386 of 209 841 (fifo600): Clopper–Pearson intervals [0, 0.0024 %] and [2.03 %, 2.15 %] do not overlap — *if* packets were independent. They are not: all of `140404Z`'s 3 064 lost packets came from one device (26), all of `141118Z`'s 1 322 from one device (29), each a radio-link failure. The honest count is **2 of 2 FIFO runs had a URLLC device lose its data path, 0 of 2 sliced runs**; an exact test of 2/2 against 0/2 gives p = 0.33. Add the longer FIFO windows (limitation 2) and the non-interleaved order (limitation 3). |
| 3. The 250 Mbps cell, not the scheduler, costs eMBB | **Best supported, by mechanism more than by test** | Aggregate 214–216 Mbps at 250 against 436–444 at 600 (same scheduler); every one of the 40 device rates at 250 is below every one at 600 (δ = −1.00), with non-overlapping cluster intervals [10.7, 10.8] vs [18.8, 24.7] Mbps. Per run, p = 0.333 (2 v 2). A 250 Mbps cell caps the sum at 250 Mbps by construction, which is the stronger argument. |
| 4. The scheduler made URLLC's tail longer (p99) | **Not supported** | ABBA block: p99 6.08 and 9.81 ms (off) against 12.24 and 14.69 ms (sliced250), δ = −1, p = 0.333 — the most extreme result 2 v 2 allows, and it is still a one-in-three event. Adding the morning sliced run (7.26 ms, a different commit) puts a sliced run inside the `off` range: δ = +0.67, p = 0.40. The mean latency moves the same way (1.89 → 2.15 ms, every device slower, δ = −1 per device), so the direction is consistent; its size is not established. |
| 5. The October → November rise (0.90 → 1.00) was not the scheduler's doing | **Supported as a logical point, not by a test** | `off` scores URLLC 1.00 on the same day, so the rise needs no scheduler. Statistically the original comparison was 1 run against 1 — no test exists for it. |

## URLLC reliability: what the runs prove

Two things count against reliability in 3GPP's sense (TS 22.261 3.1: packets delivered *within the
time constraint* ÷ packets sent; TS 23.501 5.7.3.4/5.7.3.5: a delay-critical GBR packet later than
the PDB is counted in the PER): **lost** packets and **late** ones (> 10 ms for 5QI 82).

**Loss only.** No URLLC packet was lost in seven of the nine runs:

| Pool | Packets | Lost | One-sided 95 % upper bound on loss | Delivered (any time), at least |
|---|---|---|---|---|
| off (2 runs) | 153 522 | 0 | 1.95·10⁻⁵ (rule of three 1.95·10⁻⁵) | 99.998 % |
| sliced250 (2 runs) | 152 926 | 0 | 1.96·10⁻⁵ | 99.998 % |
| sliced600 (2 runs) | 153 403 | 0 | 1.95·10⁻⁵ | 99.998 % |
| all zero-loss runs (7, incl. `092934Z`) | 537 481 | 0 | 5.6·10⁻⁶ | 99.9994 % |
| fifo600 (2 runs) | 209 841 | 4 386 | 2.14 % (and see reading 2) | 97.86 % |

Each two-run arm shows loss below 2·10⁻⁵ — inside 5QI 82's 10⁻⁴ — but **not** below 10⁻⁵: no single
arm demonstrates 99.999 % even for loss alone. Only the pool of all seven zero-loss runs does
(5.6·10⁻⁶), and pooling is fair only under the assumption that those configurations share one loss
process; FIFO shows a loss mechanism (radio-link failure under a full queue) that a zero count in
150 s cannot rule out. The bound also assumes independent packets, which a burst loss violates.

**Late packets.** Every one of the nine runs had at least one URLLC packet over 10 ms (the
smallest maximum is 33 ms). From the stored percentiles, the late fraction per run is bounded:

| Runs | p99 | Late fraction (> 10 ms) | Reliability within 10 ms, at most |
|---|---|---|---|
| off, morning sliced, fifo (5 runs) | 6.1–9.8 ms | between 0 and ~1 % (p99 ≤ 10 ms < max) | 100 % — unresolved; at least ~99 % |
| sliced250, sliced600 (4 runs) | 10.4–14.7 ms | more than ~1 % (p99 > 10 ms), less than 5 % (p95 ≤ 10 ms) | **~99 %** |

So **none of the runs demonstrates 99.999 % reliability in the TS 22.261 sense**, and the four
sliced runs of the controlled block demonstrably fall short of 99 %: more than 1 % of their packets
are late, i.e. lost in 5QI 82's terms, 100 times its 10⁻⁴ PER. The off and FIFO runs cannot be
placed more precisely than "between 99 % and 100 %" because the runs keep p99 and max, not the count
of packets over 10 ms. The 99.999 % figure itself is, in TS 22.261 V17.11.0, an *example* value
(Table 7.2.3.2-1, wireless road-side backhaul, with 30 ms latency); see
[standards-mapping.md](../../standards-mapping.md) §5.

**How many packets a claim needs** (one-sided 95 %, Clopper–Pearson; at the URLLC profile's 10
devices × 50 packets/s):

| Claim | With 0 failures | With 1 | With 2 | With 5 |
|---|---|---|---|---|
| failure rate ≤ 10⁻⁴ (5QI 82 PER) | 29 956 packets (1.0 min) | 47 437 | 62 956 | 105 128 |
| failure rate ≤ 10⁻⁵ (99.999 %) | 299 572 packets (10.0 min) | 474 385 | 629 578 | 1 051 301 (35 min) |

"Failure" means lost **or later than 10 ms**. A 150-second standard instance carries ~76 000
URLLC packets: enough for 10⁻⁴ with zero failures, a quarter of what 99.999 % needs.

## What would make the next comparison testable

- **At least 4 runs per arm, interleaved** (ABBA…, or a randomised order), so that 0.05 is
  reachable (min p 0.029 at 4 v 4) and drift is not confounded with the arm.
- **Count late packets in the collector** (packets over the PDB, per device), so reliability in the
  3GPP sense is a count, not a bound from percentiles; ideally keep a fine latency histogram.
- **Read URLLC and mMTC statistics at the end of the load window**, not after stuck downloads time
  out, so every arm measures the same window.
- **Treat a radio-link failure as an event**: report events per run alongside packet loss, because
  the packets of one event are not independent.
- **Name the primary KPI before running** (e.g. URLLC p99 for the scheduler question), so the
  16 KPIs × 6 comparisons above are not a search for the one that differs.
