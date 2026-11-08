# Scheduler algorithms on the cell — HTB, strict priority, DRR, HFSC (ADR-021)

Decision record: [`../../adr/ADR-021-scheduler-algorithms.md`](../../adr/ADR-021-scheduler-algorithms.md).
The same classifier in every mode (ADR-019); only the tree changes. Kernel support was tested first:
htb, prio, drr, hfsc (and qfq) work on this WSL2 kernel; **cake, pie/fq_pie, ets, taprio and cbs are
not compiled in**, so they could not be compared (details in the ADR).

Everything here ran on one UTC day, 2026-11-08, on one code version (`61f4c5b`; the scheduler modes
are from `36f49ad`, the commit after it only added KPI counters):

| Block | When (UTC) | What |
|---|---|---|
| A — nominal load | rounds 1–3 14:57–17:14, round 4 19:07–19:50 | standard KPI instance, cell 250/100 Mbps, 4 runs per mode, round-robin (forward, reverse, forward, reverse), interleaved with the URLLC-tail controls of [`../open5gs-urllc-tail/`](../open5gs-urllc-tail/README.md) |
| B — overload | 18:42–19:05 and 19:51–20:02 | `experiment.py` part D: eMBB offers 150 Mbps into a 100 Mbps cell. First experiment: all six modes, 2 rounds (second reversed). Second: the four modes that caused no radio-link failure, 2 more rounds — 4 runs each for off / sliced / drr / hfsc, 2 for prio / fifo |

Files: `campaign.tsv` (index of block A: arm, round, run directory, UEs without a data path after,
radio-link-failure log lines, scheduler applies), `campaign.log`, `kpi/<run>/` (each a `run_kpis.py`
run: `instances.jsonl`, `scorecard.md`, `kpi-report.xlsx`, `scheduler-mode.txt`,
`scheduler-state*.json`, `tc-stats.txt`), `overload/<run>/` (`results.json`, `summary.md`,
`results.xlsx`, `run.out`), `overload/summary-all.md`.
Tables: `python3 tools/kpi/compare_arms.py docs/evidence/open5gs-scheduler-algorithms/campaign.tsv`
and `python3 tools/packet-scheduler/compare_overload.py docs/evidence/open5gs-scheduler-algorithms/overload/*/`.

## Block A — nominal load (standard KPI instance, 250 / 100 Mbps cell)

The algorithm runs and `off` (no cell, for reference — not a scheduler at the same capacity), in the
order they ran. URLLC latency one-way uplink in ms; ">10 ms" = URLLC packets later than the slice's
10 ms budget; "pkts" = URLLC packets received; "devs w/ loss" = URLLC devices that lost any packet.

| # | Round | Run | Mode | URLLC p50 / p95 / p99 / p99.9 / max | >10 ms | pkts | devs w/ loss | eMBB agg / p5 (Mbps) | flows | UE CPU % | gNB-URLLC CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | `20261108-145743Z` | off | 1.50 / 8.67 / 21.06 / 47.2 / 112 | 3080 | 76414 | 0 | 419 / 13.8 | 20 | 406 | 10.4 |
| 6 | 1 | `20261108-152254Z` | sliced | 1.35 / 4.50 / 10.64 / 27.0 / 56 | 876 | 77312 | 0 | 225 / 8.4 | 20 | 588 | 9.8 |
| 7 | 1 | `20261108-152743Z` | prio | 1.47 / 4.88 / 10.79 / 25.0 / 55 | 914 | 77569 | 0 | 224 / 11.0 | 20 | 589 | 9.8 |
| 8 | 1 | `20261108-153343Z` | drr | 1.71 / 5.72 / 13.17 / 33.7 / 67 | 1215 | 78019 | 0 | 218 / 10.4 | 20 | 571 | 11.2 |
| 9 | 1 | `20261108-153824Z` ¹ | hfsc | 1.68 / 4.81 / 9.91 / 30.5 / 56 | 1058 | 107551 | 0 | 184 / 9.3 | 19 | 589 | 12.3 |
| 10 | 2 | `20261108-154550Z` | hfsc | 1.82 / 5.87 / 11.62 / 31.6 / 43 | 1122 | 78789 | 0 | 192 / 9.1 | 20 | 587 | 11.8 |
| 11 | 2 | `20261108-155255Z` | drr | 1.60 / 6.10 / 14.53 / 32.0 / 53 | 1571 | 77957 | 0 | 219 / 7.8 | 20 | 580 | 10.5 |
| 12 | 2 | `20261108-155738Z` | prio | 1.30 / 4.52 / 10.43 / 29.0 / 77 | 820 | 77331 | 0 | 224 / 11.0 | 20 | 598 | 9.7 |
| 13 | 2 | `20261108-160339Z` | sliced | 1.36 / 4.63 / 10.61 / 29.5 / 57 | 855 | 77741 | 0 | 226 / 11.1 | 20 | 600 | 9.4 |
| 18 | 2 | `20261108-162814Z` | off | 1.63 / 6.94 / 15.22 / 35.6 / 56 | 1846 | 76757 | 0 | 393 / 10.2 | 20 | 400 | 11.3 |
| 19 | 3 | `20261108-163238Z` | off | 1.67 / 6.38 / 12.55 / 30.4 / 48 | 1195 | 77140 | 0 | 361 / 12.2 | 20 | 397 | 9.6 |
| 24 | 3 | `20261108-165657Z` | sliced | 1.30 / 4.39 / 8.07 / 22.5 / 58 | 447 | 76460 | 0 | 223 / 10.9 | 20 | 590 | 8.8 |
| 25 | 3 | `20261108-170115Z` | prio | 1.16 / 4.28 / 7.80 / 21.3 / 40 | 457 | 76380 | 0 | 223 / 6.9 | 20 | 586 | 8.3 |
| 26 | 3 | `20261108-170536Z` | drr | 1.24 / 4.37 / 7.52 / 20.4 / 41 | 367 | 76332 | 0 | 223 / 7.0 | 20 | 590 | 8.5 |
| 27 | 3 | `20261108-170957Z` | hfsc | 1.18 / 4.29 / 8.21 / 23.3 / 45 | 488 | 76389 | 0 | 211 / 10.5 | 20 | 609 | 8.2 |
| 28 | 4 | `20261108-190734Z` | hfsc | 1.32 / 4.25 / 8.98 / 294.4 / 429 | 677 | 78357 | 0 | 219 / 10.8 | 20 | 625 | 8.2 |
| 29 | 4 | `20261108-191226Z` | drr | 1.17 / 3.54 / 6.68 / 22.6 / 58 | 328 | 77467 | 0 | 229 / 11.4 | 20 | 624 | 9.1 |
| 30 | 4 | `20261108-191908Z` | prio | 1.17 / 3.70 / 7.38 / 23.0 / 42 | 360 | 76850 | 0 | 229 / 6.8 | 20 | 620 | 8.6 |
| 31 | 4 | `20261108-192338Z` | sliced | 1.24 / 3.83 / 6.60 / 19.4 / 33 | 282 | 76710 | 0 | 229 / 11.3 | 20 | 624 | 8.2 |
| 36 | 4 | `20261108-194618Z` | off | 1.15 / 3.12 / 5.65 / 17.7 / 38 | 191 | 78040 | 0 | 479 / 15.5 | 20 | 430 | 7.6 |

¹ One eMBB download never finished; the runner waited up to 60 s for it, so URLLC was measured over a
longer window (107 551 packets instead of ~77 000). The other 35 runs of the campaign completed all 20.

Per mode, median (min–max) of four runs:

| Arm | runs | URLLC p99 (ms) | URLLC p99.9 (ms) | URLLC >10 ms (pkts) | URLLC loss | eMBB agg (Mbps) | eMBB p5 (Mbps) | mMTC p99 (ms) | UE CPU (%) | Overall |
|---|---|---|---|---|---|---|---|---|---|---|
| off | 4 | 13.89 (5.65–21.06) | 33.0 (17.7–47.2) | 1520 (191–3080) | 0 (0–0) | 406 (361–479) | 13.0 (10.2–15.5) | 13 (5–20) | 403 (397–430) | 0.80 (0.79–0.86) |
| sliced | 4 | 9.34 (6.60–10.64) | 24.7 (19.4–29.5) | 651 (282–876) | 0 (0–0) | 225 (223–229) | 11.0 (8.4–11.3) | 8 (5–10) | 595 (588–624) | 0.77 (0.76–0.77) |
| prio | 4 | 9.12 (7.38–10.79) | 24.0 (21.3–29.0) | 638 (360–914) | 0 (0–0) | 224 (223–229) | 9.0 (6.8–11.0) | 8 (6–12) | 593 (586–620) | 0.76 (0.75–0.77) |
| drr | 4 | 10.35 (6.68–14.53) | 27.3 (20.4–33.7) | 791 (328–1571) | 0 (0–0) | 221 (218–229) | 9.1 (7.0–11.4) | 8 (5–10) | 585 (571–624) | 0.75 (0.74–0.77) |
| hfsc | 4 | 9.45 (8.21–11.62) | 31.0 (23.3–294.4) | 868 (488–1122) | 0 (0–0) | 202 (184–219) | 9.9 (9.1–10.8) | 8 (6–9) | 599 (587–625) | 0.76 (0.75–0.77) |

Against HTB (permutation test on the difference of medians; 4 vs 4, smallest attainable p 0.03):

| Comparison (urllc.owd_p99_ms) | n | Median difference | p |
|---|---|---|---|
| prio − sliced | 4 / 4 | -0.22 | 0.83 |
| drr − sliced | 4 / 4 | +1.01 | 0.63 |
| hfsc − sliced | 4 / 4 | +0.11 | 0.80 |

Ranked inside each round (removes the drift between rounds; all 9 arms of the campaign), mean rank of
URLLC p99 from 1 (shortest) to 9: sliced 4.75, prio 5.00, drr 5.50, hfsc 6.00, off 6.75; Friedman
p = 0.65 ([`../open5gs-urllc-tail/stats.md`](../open5gs-urllc-tail/stats.md)).

At nominal load:

1. **The four algorithms cannot be told apart on URLLC.** Medians 9.1–10.4 ms, every range overlaps
   every other, p ≥ 0.63 against HTB. The same mode varies up to 2× between rounds (round 4, in the
   evening, was the fastest for every mode). None lost a URLLC packet on any device or an mMTC
   report; no run left a UE without a data path or logged a radio-link failure.
2. **HFSC delivers less eMBB throughput at the same configured cell**: 184–219 Mbps (median 202)
   against 218–229 for the three HTB-shaped trees, and one HFSC run had a download that never
   finished. Its root upper-limit curve (`ul m2 250 Mbit`) is honoured more strictly than HTB's rate
   with a 256 kB burst; its eMBB queue dropped fewer packets (865 vs ~4 800 in the HTB run of the same
   round, `tc-stats.txt`). The cause of the shortfall was not investigated further. HFSC's round-4 run
   also had the campaign's longest URLLC p99.9 / max (294 / 429 ms) while its p99 was ordinary.
3. **DRR and prio give eMBB the lowest 5th percentile** (median 9.0–9.1 Mbps vs 11.0 for HTB).
4. **Strict priority is as good as HTB when the cell is not overloaded**: nothing is starved.

## Block B — overload: eMBB offers 150 Mbps into a 100 Mbps cell

Latency is ping RTT from each device's own session interface to its UPF (the reply comes back
through the cell's downlink queue), ms; loss is lost pings (1 s timeout); probes every 20 ms (URLLC),
200 ms (mMTC), 100 ms (an eMBB device that is not downloading). RLF = radio-link failures logged by
the UEs during the run; "UEs w/o data path after" counts session interfaces (100 home + 30 second)
that could not reach their UPF afterwards. Before any run that would have inherited broken UEs, the
stack was re-attached (`start_ues.sh`) — "re-attached before".

| Experiment | Run | Re-attached before | RLF | UEs w/o data path after | URLLC crit p50 / p99 / loss | URLLC std p50 / p99 / loss | mMTC p50 / p99 / loss | eMBB idle device p99 / loss | eMBB delivered (Mbps) |
|---|---|---|---|---|---|---|---|---|---|
| `20261108-184258Z` | round 1 off | no | 0 | 0 | 3.1 / 58.4 / 0.0 % | 3.3 / 58.5 / 0.0 % | 3.4 / 50.8 / 0.0 % | 4635 / 0.0 % | 149.32 |
| `20261108-184258Z` | round 1 fifo | no | 98 | 126 | 104.0 / 116.4 / 64.33 % | 104.0 / 144.5 / 32.37 % | 104.0 / 129.4 / 40.0 % | — / 100.0 % | 46.22 |
| `20261108-184258Z` | round 1 sliced | yes | 0 | 0 | 17.9 / 105.0 / 0.0 % | 17.6 / 108.0 / 0.0 % | 16.9 / 85.7 / 0.0 % | 64181 / 0.37 % | 96.03 |
| `20261108-184258Z` | round 1 prio | no | 88 | 110 | 2.4 / 18.6 / 0.0 % | 2.4 / 20.0 / 0.0 % | — / — / 100.0 % | 179.4 / 43.7 % | 49.79 |
| `20261108-184258Z` | round 1 drr | yes | 0 | 0 | 4.2 / 13.9 / 0.0 % | 4.2 / 13.7 / 0.0 % | 4.2 / 11.6 / 0.0 % | 57.5 / 0.0 % | 93.4 |
| `20261108-184258Z` | round 1 hfsc | no | 0 | 0 | 1.9 / 21.2 / 0.0 % | 1.9 / 21.8 / 0.0 % | 2.0 / 30.6 / 0.0 % | 263.8 / 0.0 % | 93.3 |
| `20261108-184258Z` | round 2 hfsc | no | 0 | 0 | 1.9 / 10.0 / 0.0 % | 1.9 / 9.8 / 0.0 % | 2.0 / 7.2 / 0.0 % | 35.5 / 0.0 % | 93.38 |
| `20261108-184258Z` | round 2 drr | no | 0 | 0 | 4.0 / 14.7 / 0.0 % | 4.0 / 13.9 / 0.0 % | 4.1 / 15.0 / 0.0 % | 122.9 / 0.0 % | 93.39 |
| `20261108-184258Z` | round 2 prio | no | 180 | 130 | 2.0 / 24.0 / 94.81 % | 2.1 / 21.6 / 94.89 % | — / — / 100.0 % | 100.2 / 81.48 % | 68.92 |
| `20261108-184258Z` | round 2 sliced | yes | 0 | 0 | 1.8 / 5.5 / 0.0 % | 1.8 / 5.8 / 0.0 % | 1.9 / 5.3 / 0.0 % | 9.5 / 0.0 % | 93.08 |
| `20261108-184258Z` | round 2 fifo | no | 97 | 124 | 104.0 / 106.0 / 10.19 % | 104.0 / 106.6 / 94.93 % | 104.0 / 106.0 / 11.11 % | 108.0 / 67.59 % | 61.52 |
| `20261108-184258Z` | round 2 off | yes | 0 | 0 | 2.0 / 5.3 / 0.0 % | 2.0 / 6.1 / 0.0 % | 2.1 / 4.3 / 0.0 % | 8.2 / 0.0 % | 150.0 |
| `20261108-195102Z` | round 1 hfsc | no | 0 | 0 | 1.9 / 8.8 / 0.0 % | 1.9 / 10.4 / 0.0 % | 1.9 / 7.1 / 0.0 % | 46.2 / 0.0 % | 93.36 |
| `20261108-195102Z` | round 1 drr | no | 0 | 0 | 3.6 / 7.4 / 0.0 % | 3.6 / 7.3 / 0.0 % | 3.6 / 7.5 / 0.0 % | 7.5 / 0.0 % | 90.85 |
| `20261108-195102Z` | round 1 sliced | no | 0 | 0 | 1.8 / 4.9 / 0.0 % | 1.8 / 5.4 / 0.0 % | 1.8 / 4.3 / 0.0 % | 9.6 / 0.0 % | 92.46 |
| `20261108-195102Z` | round 1 off | no | 0 | 0 | 2.0 / 5.9 / 0.0 % | 2.0 / 5.4 / 0.0 % | 2.1 / 5.0 / 0.0 % | 8.7 / 0.0 % | 150.0 |
| `20261108-195102Z` | round 2 off | no | 0 | 0 | 1.8 / 4.7 / 0.0 % | 1.8 / 4.7 / 0.0 % | 1.8 / 4.9 / 0.0 % | 6.5 / 0.0 % | 150.0 |
| `20261108-195102Z` | round 2 sliced | no | 0 | 0 | 1.6 / 6.6 / 0.0 % | 1.6 / 6.8 / 0.0 % | 1.6 / 8.1 / 0.0 % | 44.8 / 0.0 % | 93.4 |
| `20261108-195102Z` | round 2 drr | no | 0 | 0 | 3.6 / 8.4 / 0.0 % | 3.6 / 8.6 / 0.0 % | 3.8 / 7.9 / 0.0 % | 24.4 / 0.0 % | 92.07 |
| `20261108-195102Z` | round 2 hfsc | no | 0 | 0 | 1.6 / 7.3 / 0.0 % | 1.6 / 7.1 / 0.0 % | 1.7 / 6.7 / 0.0 % | 36.1 / 0.0 % | 93.36 |

| Mode | runs | RLF per run | URLLC crit p50 (ms) | URLLC crit p99 (ms) | URLLC loss (crit, std) | mMTC p99 (ms) | mMTC loss | eMBB delivered (Mbps) |
|---|---|---|---|---|---|---|---|---|
| off | 4 | 0, 0, 0, 0 | 2.0 (1.8–3.1) | 5.6 (4.7–58.4) | 0/0 %, 0/0 %, 0/0 %, 0/0 % | 5.0 (4.3–50.8) | 0 %, 0 %, 0 %, 0 % | 150.0 (149.3–150.0) |
| fifo | 2 | 98, 97 | 104.0 (104.0–104.0) | 111.2 (106.0–116.4) | 64.33/32.37 %, 10.19/94.93 % | 117.7 (106.0–129.4) | 40 %, 11.11 % | 53.9 (46.2–61.5) |
| sliced | 4 | 0, 0, 0, 0 | 1.8 (1.6–17.9) | 6.0 (4.9–105.0) | 0/0 %, 0/0 %, 0/0 %, 0/0 % | 6.7 (4.3–85.7) | 0 %, 0 %, 0 %, 0 % | 93.2 (92.5–96.0) |
| prio | 2 | 88, 180 | 2.2 (2.0–2.4) | 21.3 (18.6–24.0) | 0/0 %, 94.81/94.89 % | — | 100 %, 100 % | 59.4 (49.8–68.9) |
| drr | 4 | 0, 0, 0, 0 | 3.8 (3.6–4.2) | 11.2 (7.4–14.7) | 0/0 %, 0/0 %, 0/0 %, 0/0 % | 9.7 (7.5–15.0) | 0 %, 0 %, 0 %, 0 % | 92.7 (90.8–93.4) |
| hfsc | 4 | 0, 0, 0, 0 | 1.9 (1.6–1.9) | 9.4 (7.3–21.2) | 0/0 %, 0/0 %, 0/0 %, 0/0 % | 7.2 (6.7–30.6) | 0 %, 0 %, 0 %, 0 % | 93.4 (93.3–93.4) |

Permutation tests against sliced (difference of medians, two-sided):

| Mode − sliced | URLLC crit p50: diff, p | URLLC crit p99: diff, p | mMTC p99: diff, p |
|---|---|---|---|
| off (4 vs 4) | +0.2 ms, p = 0.66 | -0.4 ms, p = 0.83 | -1.7 ms, p = 0.72 |
| fifo (2 vs 4) | +102.2 ms, p = 0.07 | +105.1 ms, p = 0.07 | +111.0 ms, p = 0.07 |
| prio (2 vs 4) | +0.4 ms, p = 0.60 | +15.2 ms, p = 0.60 | — (no replies) |
| drr (4 vs 4) | +2.0 ms, p = 0.14 | +5.1 ms, p = 0.23 | +3.0 ms, p = 0.57 |
| hfsc (4 vs 4) | +0.0 ms, p = 0.66 | +3.4 ms, p = 0.49 | +0.5 ms, p = 0.89 |

Under overload:

1. **Strict priority and a plain FIFO cell are unsafe.** prio starved the mMTC band completely (100 %
   probe loss in both runs) and caused 88 and 180 radio-link failures, leaving 110 and all 130 session
   interfaces without a data path; in its second run URLLC lost 95 % too. FIFO caused 97–98 radio-link
   failures and 124–126 broken interfaces, with URLLC loss of 10–95 %. Signalling has the top band in
   prio, so these failures are not lost heartbeats; a plausible mechanism (not verified here) is that
   UERANSIM declares a radio-link failure when RLS user-data PDUs stay unacknowledged, which is what
   starving the lower bands produces.
2. **HTB (sliced), DRR and HFSC kept every UE up** — 0 radio-link failures and 0 loss on URLLC and mMTC
   in all 12 runs — and all delivered 91–96 Mbps of eMBB through the 100 Mbps cell.
3. **DRR costs URLLC median latency under load**: p50 3.6–4.2 ms in all four runs against 1.6–2.0 ms
   for HTB and HFSC in 7 of their 8 (the eighth, HTB right after a re-attach: 17.9). That is DRR's
   round: eMBB's quantum is 45 420 bytes, 3.6 ms of a 100 Mbps cell, and a URLLC packet that arrives
   behind it waits for it. Against HTB it is not significant with 4 runs because of that one HTB
   outlier (p = 0.14), but it is what the algorithm predicts and it repeats in every DRR run.
4. **HFSC's delay curves did not beat HTB here**: URLLC p99 7.3–21.2 ms against 4.9–6.6 ms in HTB's
   three ordinary runs; medians 9.4 vs 6.0 ms, p = 0.49. URLLC on this testbed is never backlogged
   (64 B every 20 ms against a 7.5 Mbps guarantee), so HTB's priority already serves it first and
   HFSC's decoupling has nothing to add; its value would show when URLLC itself queues. HFSC's
   eMBB-queue drop counter read 0 while ~57 of the offered 150 Mbps were not delivered: where HFSC
   dropped them was not established.
5. **Two of the 16 runs of the safe modes had very long tails** (off round 1: URLLC p99 58 ms, idle eMBB
   device p99 4.6 s; sliced round 1: 105 ms and 64 s), both in the first half-hour of block B. An idle
   eMBB device's RTT of seconds is queueing inside the eMBB gNB process (seen before:
   [`../open5gs-scheduler/`](../open5gs-scheduler/)), not in the cell — `off` has no cell and showed
   it too. Neither outlier repeated in rounds 3–4.

## Verdict

On both nominal-load KPIs and overload safety, **no algorithm was clearly and repeatably better than
HTB**: prio and FIFO are disqualified by overload (mass radio-link failures), DRR adds ~2 ms of median
URLLC latency under load, HFSC delivered ~10 % less eMBB at nominal load without a URLLC gain.
**The default stays `sliced` (HTB).**
