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
