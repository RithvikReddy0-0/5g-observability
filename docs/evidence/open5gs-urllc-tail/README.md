# Why was URLLC's p99 longer with the packet scheduler? — controlled test, 2026-11-08

**Question** (open in [`../open5gs-kpi/README.md`](../open5gs-kpi/README.md), "Same-day comparison"):
five standard KPI runs that morning gave URLLC p99 6.1 / 9.8 ms with the scheduler off and 7.3 /
12.2 / 14.7 ms with it on (sliced, 250 Mbps). The untested hypothesis: *any* classful qdisc in place
of the veth's default `noqueue` adds latency under eMBB's packet rate (qdisc lock, deferred softirq
transmit), not the classification itself.

**Answer: the hypothesis is not supported, and the observation did not reproduce.** In four
interleaved rounds (36 runs, 4 per arm), a classful-but-unshaped tree was not slower than `noqueue`,
a scheduler on one direction was not slower than none, and the full scheduler was not slower than
`off`. Ranked inside each round (which removes the drift between rounds), `off` came out *last*,
and no arm differs from the others (Friedman p = 0.65). What does track URLLC's tail is how much CPU
time the lightly loaded containers needed for the same constant work — a host-wide effect, not a
queueing one. That is a correlation; it is consistent with the host itself running slower in some
runs but does not prove it.

## Design

Same stack, same code (commit `61f4c5b`; the controls were added in `36f49ad`), one UTC day:
rounds 1–3 at 14:57–17:14, round 4 at 19:07–19:50 UTC. Standard KPI instance each run
(`tools/kpi/run_kpis.py standard`: fresh attach of all 100 UEs, 20 eMBB TCP downloads, URLLC
64 B every 20 ms from 10 devices ≈ 500 pkt/s, mMTC reports, 150 s). Only `POST :9114/config`
changed between runs. Round order: forward, reverse, forward, reverse. These arms ran interleaved
with the algorithm comparison of ADR-021 (one campaign, one index:
[`../open5gs-scheduler-algorithms/campaign.tsv`](../open5gs-scheduler-algorithms/campaign.tsv), run
directories in [`../open5gs-scheduler-algorithms/kpi/`](../open5gs-scheduler-algorithms/kpi/)).
URLLC latency is one-way **uplink**, device → data network, on one monotonic clock.

| Arm | Downlink (host veth) | Uplink (UE eth0) | What it isolates |
|---|---|---|---|
| `off` | noqueue | noqueue | the stack as before ADR-019 |
| `fifo-10g` | HTB, one class at 10 Gbit/s, pfifo | same | a classful qdisc, no classifier, no shaping |
| `sliced-10g` | full ADR-019 tree + filters at 10 Gbit/s | same | classifier and tree, no shaping |
| `sliced-ul-only` | noqueue | sliced, 100 Mbps | the uplink scheduler alone (`directions: ["uplink"]`) |
| `sliced-dl-only` | sliced, 250 Mbps | noqueue | the downlink scheduler alone |
| `sliced` | sliced, 250 Mbps | sliced, 100 Mbps | the default |

Each run directory has `scheduler-mode.txt`, the scheduler's `/state` before and after, and
`tc -s qdisc` of both directions at the end (`tc-stats.txt`). In every run the scheduler's last
re-apply happened during the attach, before the measurement window (`last_apply_utc` in the index).
No run lost a URLLC packet (loss is also checked per device: no device lost any), no run left a UE
without a data path, no radio-link failure was logged.

## Results — all runs of these arms

URLLC one-way latency in ms; ">10 ms" = URLLC packets later than the slice's 10 ms budget; "pkts" =
URLLC packets received in the window; "devs w/ loss" = URLLC devices that lost any packet; gNB-URLLC
CPU = % of one core used by the URLLC gNB container, whose work (≈ 500 pkt/s) is the same in every run.

| # | Round | Run | Arm | p50 / p95 / p99 / p99.9 / max | >10 ms | pkts | devs w/ loss | eMBB agg / p5 (Mbps) | flows | UE CPU % | gNB-URLLC CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `20261108-145743Z` | off | 1.50 / 8.67 / 21.06 / 47.2 / 112 | 3080 | 76414 | 0 | 419 / 13.8 | 20 | 406 | 10.4 |
| 18 | 2 | `20261108-162814Z` | off | 1.63 / 6.94 / 15.22 / 35.6 / 56 | 1846 | 76757 | 0 | 393 / 10.2 | 20 | 400 | 11.3 |
| 19 | 3 | `20261108-163238Z` | off | 1.67 / 6.38 / 12.55 / 30.4 / 48 | 1195 | 77140 | 0 | 361 / 12.2 | 20 | 397 | 9.6 |
| 36 | 4 | `20261108-194618Z` | off | 1.15 / 3.12 / 5.65 / 17.7 / 38 | 191 | 78040 | 0 | 479 / 15.5 | 20 | 430 | 7.6 |
| 2 | 1 | `20261108-150217Z` | fifo-10g | 1.27 / 4.82 / 9.15 / 20.3 / 51 | 581 | 76539 | 0 | 478 / 15.0 | 20 | 421 | 9.4 |
| 17 | 2 | `20261108-162355Z` | fifo-10g | 1.36 / 6.09 / 14.88 / 41.1 / 105 | 1524 | 76890 | 0 | 446 / 11.7 | 20 | 417 | 9.4 |
| 20 | 3 | `20261108-163703Z` | fifo-10g | 1.63 / 5.93 / 12.24 / 28.6 / 49 | 1129 | 77083 | 0 | 374 / 12.0 | 20 | 398 | 10.1 |
| 35 | 4 | `20261108-194141Z` | fifo-10g | 1.13 / 2.96 / 5.69 / 20.3 / 29 | 295 | 77650 | 0 | 484 / 15.4 | 20 | 430 | 7.8 |
| 3 | 1 | `20261108-150801Z` | sliced-10g | 1.18 / 3.34 / 5.94 / 18.3 / 31 | 341 | 77860 | 0 | 413 / 11.5 | 20 | 418 | 9.2 |
| 16 | 2 | `20261108-161710Z` | sliced-10g | 1.21 / 4.56 / 9.04 / 24.3 / 56 | 630 | 77004 | 0 | 460 / 14.1 | 20 | 420 | 9.9 |
| 21 | 3 | `20261108-164132Z` | sliced-10g | 1.84 / 6.35 / 15.42 / 37.6 / 64 | 1610 | 78197 | 0 | 358 / 11.4 | 20 | 397 | 9.8 |
| 34 | 4 | `20261108-193707Z` | sliced-10g | 1.08 / 2.67 / 4.97 / 11.0 / 31 | 109 | 77055 | 0 | 473 / 14.3 | 20 | 431 | 9.3 |
| 4 | 1 | `20261108-151357Z` | sliced-ul-only | 1.19 / 3.45 / 6.76 / 19.8 / 53 | 422 | 77430 | 0 | 426 / 12.5 | 20 | 425 | 8.0 |
| 15 | 2 | `20261108-161249Z` | sliced-ul-only | 1.27 / 4.85 / 10.00 / 26.7 / 49 | 765 | 76503 | 0 | 463 / 17.6 | 20 | 424 | 9.1 |
| 22 | 3 | `20261108-164804Z` | sliced-ul-only | 1.38 / 6.67 / 16.95 / 40.3 / 109 | 2019 | 76829 | 0 | 454 / 16.2 | 20 | 415 | 11.3 |
| 33 | 4 | `20261108-193234Z` | sliced-ul-only | 1.16 / 3.20 / 5.12 / 20.2 / 28 | 210 | 77500 | 0 | 483 / 14.5 | 20 | 432 | 7.3 |
| 5 | 1 | `20261108-151826Z` | sliced-dl-only | 1.28 / 4.06 / 9.13 / 27.0 / 57 | 674 | 77567 | 0 | 229 / 7.3 | 20 | 603 | 8.9 |
| 14 | 2 | `20261108-160817Z` | sliced-dl-only | 1.43 / 5.96 / 17.60 / 46.8 / 80 | 1762 | 77546 | 0 | 217 / 10.6 | 20 | 579 | 10.3 |
| 23 | 3 | `20261108-165235Z` | sliced-dl-only | 1.21 / 4.03 / 7.06 / 21.8 / 35 | 335 | 76361 | 0 | 223 / 11.0 | 20 | 584 | 8.4 |
| 32 | 4 | `20261108-192809Z` | sliced-dl-only | 1.23 / 4.01 / 7.74 / 25.8 / 46 | 443 | 76827 | 0 | 229 / 11.3 | 20 | 621 | 8.4 |
| 6 | 1 | `20261108-152254Z` | sliced | 1.35 / 4.50 / 10.64 / 27.0 / 56 | 876 | 77312 | 0 | 225 / 8.4 | 20 | 588 | 9.8 |
| 13 | 2 | `20261108-160339Z` | sliced | 1.36 / 4.63 / 10.61 / 29.5 / 57 | 855 | 77741 | 0 | 226 / 11.1 | 20 | 600 | 9.4 |
| 24 | 3 | `20261108-165657Z` | sliced | 1.30 / 4.39 / 8.07 / 22.5 / 58 | 447 | 76460 | 0 | 223 / 10.9 | 20 | 590 | 8.8 |
| 31 | 4 | `20261108-192338Z` | sliced | 1.24 / 3.83 / 6.60 / 19.4 / 33 | 282 | 76710 | 0 | 229 / 11.3 | 20 | 624 | 8.2 |

## Per arm: median (min–max) of four runs

| Arm | runs | URLLC p99 (ms) | URLLC p99.9 (ms) | URLLC >10 ms (pkts) | URLLC loss | eMBB agg (Mbps) | eMBB p5 (Mbps) | mMTC p99 (ms) | UE CPU (%) | Overall |
|---|---|---|---|---|---|---|---|---|---|---|
| off | 4 | 13.89 (5.65–21.06) | 33.0 (17.7–47.2) | 1520 (191–3080) | 0 (0–0) | 406 (361–479) | 13.0 (10.2–15.5) | 13 (5–20) | 403 (397–430) | 0.80 (0.79–0.86) |
| fifo-10g | 4 | 10.70 (5.69–14.88) | 24.4 (20.3–41.1) | 855 (295–1524) | 0 (0–0) | 462 (374–484) | 13.5 (11.7–15.4) | 10 (4–10) | 419 (398–430) | 0.84 (0.81–0.86) |
| sliced-10g | 4 | 7.49 (4.97–15.42) | 21.3 (11.0–37.6) | 486 (109–1610) | 0 (0–0) | 436 (358–473) | 12.8 (11.4–14.3) | 6 (5–11) | 419 (397–431) | 0.83 (0.79–0.85) |
| sliced-ul-only | 4 | 8.38 (5.12–16.95) | 23.4 (19.8–40.3) | 594 (210–2019) | 0 (0–0) | 458 (426–483) | 15.4 (12.5–17.6) | 8 (4–15) | 425 (415–432) | 0.84 (0.83–0.86) |
| sliced-dl-only | 4 | 8.43 (7.06–17.60) | 26.4 (21.8–46.8) | 558 (335–1762) | 0 (0–0) | 226 (217–229) | 10.8 (7.3–11.3) | 6 (5–13) | 593 (579–621) | 0.76 (0.75–0.77) |
| sliced | 4 | 9.34 (6.60–10.64) | 24.7 (19.4–29.5) | 651 (282–876) | 0 (0–0) | 225 (223–229) | 11.0 (8.4–11.3) | 8 (5–10) | 595 (588–624) | 0.77 (0.76–0.77) |

## Statistics ([`stats.md`](stats.md), `tools/kpi/compare_arms.py --stats`)

Permutation tests on the difference of medians (two-sided, 20 000 shuffles):

| Comparison (urllc.owd_p99_ms) | n | Median difference | p |
|---|---|---|---|
| off − fifo-10g+sliced-10g | 4 / 8 | +4.79 | 0.39 |
| off − fifo-10g | 4 / 4 | +3.19 | 0.46 |
| off − sliced-10g | 4 / 4 | +6.40 | 0.49 |
| off − sliced | 4 / 4 | +4.55 | 0.17 |
| fifo-10g+sliced-10g+sliced-ul-only − off | 12 / 4 | -4.79 | 0.25 |
| sliced − sliced-dl-only | 4 / 4 | +0.90 | 0.74 |
| sliced-ul-only − off | 4 / 4 | -5.51 | 0.49 |
| off+fifo-10g+sliced-10g+sliced-ul-only − sliced-dl-only+sliced+prio+drr+hfsc | 16 / 20 | +0.52 | 0.74 |
| prio − sliced | 4 / 4 | -0.22 | 0.83 |
| drr − sliced | 4 / 4 | +1.01 | 0.63 |
| hfsc − sliced | 4 / 4 | +0.11 | 0.80 |

With 4 runs against 4 the smallest attainable p is 0.03, so these comparisons could have shown a
consistent effect; none did. Inside each round every arm ran once, so ranking within a round removes
the drift between rounds (round 4, in the evening, was the fastest round for every arm):

| Arm | mean rank of urllc.owd_p99_ms within a round (1 = shortest), 4 complete rounds |
|---|---|
| sliced-10g | 2.75 |
| sliced-ul-only | 3.75 |
| sliced | 4.75 |
| prio | 5.00 |
| fifo-10g | 5.25 |
| sliced-dl-only | 5.25 |
| drr | 5.50 |
| hfsc | 6.00 |
| off | 6.75 |

Friedman chi-square 5.93, df 8, p = 0.65

Spearman rank correlation of URLLC p99 with what else was measured, over all 36 runs: the URLLC and
mMTC gNBs and UPFs and the URLLC data network +0.66 to +0.86 (gNB-mMTC +0.86, gNB-URLLC +0.80);
eMBB throughput −0.27; host load average −0.20 to −0.29; the UE container −0.39; run order −0.54
(later runs were faster).

## What this shows

1. **A classful qdisc in place of `noqueue` does not lengthen URLLC's tail here.** The three arms
   with a qdisc but no shaping (`fifo-10g`, `sliced-10g`, `sliced-ul-only`) have *shorter* median p99
   than `noqueue` (7.5–10.7 vs 13.9 ms), and `off` has the worst mean rank within rounds. The
   hypothesis predicted the opposite sign; nothing is significant either way.
2. **Neither direction's scheduler is the cause.** Uplink only, downlink only and both give
   overlapping ranges (5.1–17.0, 7.1–17.6 and 6.6–10.6 ms).
3. **The morning's "the scheduler lengthens the tail" did not reproduce.** One run's p99 varies by a
   factor of 2–4 inside one arm (`off`: 5.7–21.1 ms); five runs that morning could not separate that
   from an effect. Point 4 of the earlier same-day comparison should be read as **not confirmed**.
4. **What tracks the tail is CPU time for fixed work.** The URLLC gNB needed 7.3–12.3 % of a core for
   the same 500 pkt/s; runs where it (and the other lightly loaded containers) needed more had the
   longer URLLC tail (ρ up to +0.86). Neither the arm, the eMBB load nor the host load average does.
   The same work costing up to 70 % more CPU time points at the host itself running slower in some
   runs — this is a laptop running Windows with the stack in a WSL2 VM (frequency scaling, Windows
   activity and VM scheduling are outside the experiment). Consistent, not proven.

## What remains open

- The cause of the URLLC tail itself (p99 5–21 ms with p50 ≈ 1.2–1.8 ms). The data point at host-wide
  slowdowns, not at queueing in the cell. Showing it directly needs host-level measurements logged
  with every run (CPU frequency, VM steal time, Windows-side load), or the same test on the ODE's
  dedicated hardware.
- Shaping the downlink to 250 Mbps raises the UE container's CPU from ~420 % to ~600 % of a core
  while it carries half the eMBB traffic. It did not lengthen URLLC's tail (point 2); its cause (TCP
  recovering from the cell's drops, or softirq time charged to the UE container) was not investigated.
