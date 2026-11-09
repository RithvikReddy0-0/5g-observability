# Phase 2b KPI runs (Open5GS, three slices)

`make o5gs-kpi-all`, i.e. `tools/kpi/run_kpis.py all`
([ADR-015](../../adr/ADR-015-traffic-profiles-and-kpi-scorecard.md)). Each run directory holds:

| File | Contents |
|---|---|
| `kpi-report.xlsx` | **the Excel report.** README, standard scorecard, slice scores, one sheet per sweep, all instances, eMBB per device, URLLC/mMTC per device, KPI definitions. Opens in Excel; written with the standard library. |
| `scorecard.md` | the same results as Markdown tables |
| `sweep-*.csv` | one CSV per sweep |
| `instances.jsonl` | every instance raw: measured values, the scorecard, per-device data |
| `run.log`, `meta.json` | what ran, when, at which commit |

KPIs, targets and weights: [`deployments/open5gs/kpi-scorecard.json`](../../../deployments/open5gs/kpi-scorecard.json),
explained in [`docs/kpi-scorecard.md`](../../kpi-scorecard.md).

## Same-day comparison, 2026-11-08 — what the packet scheduler does at nominal load

The run below (`092934Z`) was first compared with the October run, five weeks and three changes
earlier, and read as "the scheduler lifted URLLC from 0.90 to 1.00". That comparison was not
controlled. These runs are: one day, one stack (commit `fbd68fb`), standard instance (20 eMBB devices
downloading, URLLC and mMTC profiles on, 150 s), only the scheduler's mode and cell changed through
`POST :9114/config`. Off and sliced at 250 Mbps ran in ABBA order (off, sliced, sliced, off). Each run
directory has `scheduler-mode.txt` and the scheduler's `/state` at the end of the run.

| Run | Scheduler, cell (Mbps) | URLLC p99 / p95 (ms) | URLLC loss | eMBB aggregate / p5 (Mbps) | eMBB flows | mMTC loss / p99 | Scores URLLC / eMBB / mMTC | Overall |
|---|---|---|---|---|---|---|---|---|
| `20261108-130046Z` | off (no cell) | 6.08 / 3.14 | 0 % | 548 / 21.1 | 20/20 | 0 % / 5 ms | 1.00 / 0.65 / 1.00 | 0.88 |
| `20261108-131657Z` | off (no cell) | 9.81 / 4.99 | 0 % | 483 / 15.1 | 20/20 | 0 % / 8 ms | 1.00 / 0.57 / 1.00 | 0.86 |
| `20261108-092934Z` | sliced, 250 | 7.26 / 3.85 | 0 % | 228 / 11.2 | 20/20 | 0 % / 5 ms | 1.00 / 0.32 / 1.00 | 0.77 |
| `20261108-130550Z` | sliced, 250 | 12.24 / 5.57 | 0 % | 216 / 10.6 | 20/20 | 0 % / 8 ms | 0.97 / 0.30 / 1.00 | 0.76 |
| `20261108-131207Z` | sliced, 250 | 14.69 / 5.76 | 0 % | 214 / 10.5 | 20/20 | 0 % / 13 ms | 0.95 / 0.30 / 1.00 | 0.75 |
| `20261108-133106Z` | sliced, 600 | 10.41 / 4.81 | 0 % | 436 / 12.1 | 20/20 | 0 % / 11 ms | 0.99 / 0.49 / 1.00 | 0.83 |
| `20261108-133647Z` | sliced, 600 | 14.02 / 3.88 | 0 % | 444 / 14.9 | 20/20 | 0 % / 8 ms | 0.96 / 0.53 / 1.00 | 0.83 |
| `20261108-140404Z` | FIFO, 600 | 8.02 / 3.89 | **2.88 %** | 414 / 13.9 | **19/20** | 0.54 % / 9 ms | 0.80 / 0.50 / 0.67 | 0.66 |
| `20261108-141118Z` | FIFO, 600 | 8.89 / 4.31 | **1.28 %** | 399 / 14.9 | **18/20** | 0 % / 9 ms | 0.80 / 0.50 / 1.00 | 0.77 |

What this shows:

1. **`off` is not "a cell without a scheduler", it is no cell at all**: a veth with no capacity, so at
   this load nothing queues and it scores best (0.86–0.88). It is the stack before ADR-019, not a model
   of a radio cell, which always has a capacity.
2. **At the same cell capacity, the scheduler beats a plain queue.** FIFO at 600 Mbps lost 1.3–2.9 %
   of URLLC packets (score 0.80), all of it on **one device per run** whose radio link failed (device
   26: 3 064 packets; device 29: 1 322; the other nine lost none), and once 0.5 % of mMTC reports; 1–2 eMBB downloads never finished
   and the second run left 3 radio-link failures (3 UEs without a data path afterwards). Sliced at
   600 Mbps lost nothing on any slice, every download finished, no radio-link failure: overall 0.83
   against 0.66 and 0.77.
3. **The 250 Mbps cell, not the scheduler, is what costs eMBB**: 214–228 Mbps at 250, 436–444 at 600.
   At 250 the cell splits eMBB evenly (5th percentile = mean ≈ 10.7 Mbps per device).
4. **URLLC's tail at nominal load: these runs suggested the scheduler made it longer; a controlled
   test did not reproduce that (update below).** Sliced p99
   7.3–14.7 ms (mean 11.4) at 250 and 10.4–14.0 ms at 600, against 6.1–9.8 ms off and 8.0–8.9 ms
   FIFO (whose p99 counts only the packets that arrived). The cause is not established. URLLC's
   one-way latency is uplink, where the cell (100 Mbps) is far from full; one candidate is the cost
   of putting any classful qdisc in place of the veth's default `noqueue` under eMBB's packet rate.
   This is open.
   **Update, same day (later):** a controlled test of 36 interleaved runs did not reproduce this and
   does not support the `noqueue` hypothesis; the tail varies 2–4× from run to run regardless of the
   scheduler. See [`../open5gs-urllc-tail/`](../open5gs-urllc-tail/README.md).
5. **The October → November rise (0.90 → 1.00) is not the scheduler's doing**: with it off, URLLC
   scores 1.00 today as well. The earlier text that credited it is withdrawn.

Three further runs crashed before writing a scorecard: one eMBB download never ended and the runner
died on `TimeoutExpired`. They are kept in [`crashed-runs/`](crashed-runs/README.md) with the fix to
the runner (a stuck flow is now killed and counted as not completed). The two FIFO runs above ran with
that fix (their `meta.json` says "+ uncommitted changes": the fix, not yet committed then). In WSL,
`git diff` also lists 9 evidence files that differ from the Windows checkout only in line endings;
the code that ran is `fbd68fb`.

## Run `20261108-092934Z` — with the packet scheduler and multi-session UEs

Standard instance only (`make o5gs-kpi`), same load as below, with the packet scheduler on (ADR-019,
250 Mbps downlink cell) and eMBB/URLLC devices holding a second session (ADR-020).

| Slice | Measured | Score |
|---|---|---|
| URLLC | latency mean 1.65 ms, p95 3.85 ms, **p99 7.26 ms** (target 10 ms: met), loss 0 | 1.00 |
| eMBB | 5th percentile 11.25 Mbps, aggregate 228.1 Mbps (capped by the cell) | 0.32 |
| mMTC | registration 100 %, report loss 0, p99 5.0 ms | 1.00 |

This is the best of the three sliced runs at 250 Mbps that day; the other two gave URLLC p99 12.2
and 14.7 ms (see the same-day comparison above, which replaces the comparison with October).
[Scorecard](20261108-092934Z/scorecard.md).

## Run `20261001-124845Z`

The stack ran with eMBB 20, URLLC 10 and mMTC 70 UEs, the URLLC and mMTC traffic profiles always on,
and the free5GC stack stopped. Host: WSL2, 12 threads, 7.6 GiB.

### Standard instance — all three slices at once for 150 s, 20 eMBB devices downloading

| Slice | KPI | Measured | Target | Result |
|---|---|---|---|---|
| URLLC | latency mean (one-way) | 3.93 ms | ≤ 10 ms | ✅ |
| URLLC | latency p95 / p99 | 11.4 / 22.0 ms | ≤ 10 ms | ❌ / ❌ |
| URLLC | packet loss (77 555 packets) | 0 | ≤ 0.01 % | ✅ |
| eMBB | 5th-percentile DL per device | 10.8 Mbps | ≥ 50 Mbps | ❌ |
| eMBB | aggregate DL | 299 Mbps | ≥ 500 Mbps | ❌ |
| mMTC | registration success | 100 % (70/70) | ≥ 99 % | ✅ |
| mMTC | report loss (1 089 reports, resolution 0.09 %) | 0 | ≤ 0.1 % | ✅ |
| mMTC | report latency p99 | 18 ms | ≤ 10 s | ✅ |

**Slice scores: URLLC 0.90 · eMBB 0.37 · mMTC 1.00 · overall 0.76.**

### Sweeps — one parameter changes, everything else as above (60 s each)

| eMBB devices downloading | 0 | 5 | 10 | 20 |
|---|---|---|---|---|
| eMBB p5 / aggregate (Mbps) | — | **46.4** / 358 | 12.6 / 303 | 7.0 / 275 |
| URLLC p99 (ms) | **8.6** | 14.6 | 16.4 | 15.2 |

| IoT devices attached (re-run, see below) | 70 | 140 | 210 |
|---|---|---|---|
| UEs in total | 100 | 170 | 240 |
| mMTC registered · registration success · report loss | 70 · 100 % · 0 | 140 · 100 % · 0 | 210 · 100 % · 0 |
| URLLC mean / p99 (ms) | 2.8 / 17.2 | 2.0 / 10.4 | 2.4 / 17.0 |
| eMBB p5 / aggregate (Mbps) | 10.9 / 424 | 8.4 / 455 | 13.7 / 436 |

| IoT reporting interval (s) | 1 | 5 | 10 | 30 |
|---|---|---|---|---|
| mMTC reports in 60 s · loss | 4 605 · 0 | 969 · 0 | 490 · 0 | 140 · 0 |
| URLLC p99 (ms) | 11.9 | 10.5 | 18.8 | 19.8 |

### What the numbers say

- **URLLC meets its mean budget comfortably** (2.0–4.3 ms). Its tail passes 10 ms whenever eMBB is
  loaded (p99 8.6 ms with no eMBB load, 10.4–22.0 ms with it), and no URLLC packet was lost. The slices
  have separate user planes but share one laptop CPU — the tail is where that shows.
- **eMBB is capped by the laptop, not by the slice design.** Five devices get 46 Mbps each at the
  5th percentile, close to the 50 Mbps target. Twenty share 255–455 Mbps and get 5–14 Mbps. The
  notes' two eMBB targets need 1 Gbps (50 × 20) and 500 Mbps respectively.
- **mMTC is solid at every scale tried:** 100 % registration (70/70, 140/140, 210/210) and zero
  report loss, and zero loss at reporting intervals from 1 to 30 s. Within this range IoT scale
  shows no effect on the other slices that stands out from run-to-run variation (below).
- **The IoT reporting interval barely matters** at these rates: 70 devices at 1 s is 70 packets/s.
  URLLC's p99 across that sweep moves with eMBB contention, not with the interval.

### Run-to-run variation — read single instances with care

Every instance with 20 eMBB devices loaded used identical settings, yet the eMBB aggregate ranged
from **255 to 455 Mbps**: about 255–300 Mbps for the first nine, ~425–455 Mbps for the three re-run
points 15 minutes later. The 5th percentile ranged 4.9–13.7 Mbps and URLLC's p99 10.4–22.0 ms. On
this laptop, host conditions move eMBB more than any parameter swept here.

What holds in **every** instance is robust:
- URLLC mean ≤ 4.3 ms, with no packet lost;
- URLLC p99 above 10 ms whenever eMBB is loaded;
- the eMBB 5th percentile far below 50 Mbps with 20 devices, and the aggregate below 500 Mbps;
- mMTC 100 % and zero loss.

For finer comparisons, repeat each point (n ≥ 3) — ideally on the lab machine.

### The device sweep was run twice

The first device sweep (13:04–13:10) counted registration for only the first 70 IoT devices. The
harness read the UE plan from the container's environment, not from the one-off override the sweep
uses, so 140 and 210 devices showed as "70 / 70". Reports and loss were counted correctly (140 and
210 devices reported). The harness was fixed, and the sweep was re-run into this run directory
(13:16–13:23). The report uses the re-run; both are kept in `instances.jsonl`.

### Caveats

- The mMTC loss resolution is 1 / reports, stated per instance. Only the standard instance
  (1 089 reports), the 1 s interval (4 605) and the 210-device point (1 405) can resolve 0.1 %;
  the 5 s interval (969) nearly can.
- Registration success describes the latest attach. The device sweep re-attaches for each point,
  so each point has its own value.
- This laptop also ran WSL, Docker Desktop's UI and the AnyDesk session. Absolute numbers will
  differ on the lab machine; the scripts are the same.
