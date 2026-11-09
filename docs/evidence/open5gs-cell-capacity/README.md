# Default cell capacity: 250 against 600 Mbps downlink, interleaved (2026-11-09)

The default cell was changed from 250 to 600 Mbps downlink (uplink 100 both) on the strength of two
600 Mbps runs from 2026-11-08 that were not interleaved with the 250 runs (ADR-023 flagged this).
This campaign tests the change properly.

**Setup**
- **Runner:** `tools/kpi/campaign.py`, five rounds, each running both arms once.
- **Order:** alternating, 250-600, 600-250, ... (ABBA), so drift through the morning falls on both arms equally.
- **Runs:** each is `tools/kpi/run_kpis.py standard`: a fresh attach of all 100 UEs, then 150 s with 20 eMBB downloads and the URLLC and mMTC profiles.
- **Changed between arms:** only the scheduler's cell, set through `POST :9114/config`. Each run directory records it in `scheduler-mode.txt` and `scheduler-state.json`.
- **Code:** commit `4ce595c`. Round 5 also ran with the fixed KPI collector (below).
- **UEs:** no run left a UE without a data path; every run had 20/20 downloads finish.

Files:
- `campaign.tsv`: the index.
- `campaign.log`.
- `kpi/<run>/`: one directory per run.
- `analyse.py` and `analysis.txt`: the tables below.
- `collector-stall.txt`: the round-1 fault.

## Result

| | 250 Mbps (rounds 2–5) | 600 Mbps (rounds 2–5) | Exact Mann–Whitney |
|---|---|---|---|
| Overall score | 0.770, 0.770, 0.758, 0.749 (median 0.764) | 0.841, 0.798, 0.817, 0.846 (median 0.829) | p = 0.029, Cliff's δ = 1 |
| eMBB aggregate | 222, 224, 212, 212 Mbps | 431, 410, 408, 462 Mbps | p = 0.029, δ = 1 |
| eMBB score | 0.29–0.31 | 0.45–0.54 | p = 0.029 |
| URLLC p99 | 8.0, 6.4, 11.7, 14.3 ms | 7.3, 15.5, 11.8, 6.0 ms | p = 1.0 |
| URLLC packets later than 10 ms | 523, 310, 1 089, 1 585 | 393, 1 682, 1 106, 180 | p = 1.0 |
| URLLC loss | 0 in every run | 0 in every run | — |
| mMTC loss | 0 | 0 | — |

**Paired by round (600 minus 250):**
- **Overall:** +0.072, +0.028, +0.059, +0.097.
- **eMBB:** +209, +186, +196, +251 Mbps.

600 won both measures in every round. 4 against 4 is the smallest design whose exact test can reach
p < 0.05 (the minimum is 0.029), and it does. With round 1 included, eMBB and the overall score
differ even more clearly (p = 0.008), and URLLC still does not differ (p99 p = 0.55).

**Decision confirmed:** the default stays 600 / 100 Mbps. It doubles eMBB, and URLLC shows no
measurable cost: the tail varies 2–3× from run to run at either size.

## Round 1's 250 run: an instrument fault, not a result

`20261109-070351Z` lost 5.2 % of URLLC packets, with p99 29 ms and max 9.5 s. It was the only run in
either arm with any loss. The loss happened **at the URLLC KPI collector's own socket**:
- the kernel's `RcvbufErrors` counter for that socket read exactly 4 280, the run's lost packets;
- every device lost the same ~428 packets;
- the collector was unreachable for its scrape at the moment the run started.

The collector had kept every sample since the previous KPI run, ~10.5 hours earlier. Replacing that
tally at `/run/start` blocked its receive loop for ~9 s, and its socket buffer, which the kernel
had capped at 416 KiB, held only about a second of traffic. Details, the inferred part, and what
the fault diagnosis said at the time are in [`collector-stall.txt`](collector-stall.txt).

**Fix** (`deployments/open5gs/observability/kpi_collector.py`, with tests):
- samples are flat arrays, capped at 4 million per run, with counts still exact;
- the old tally is freed outside the lock;
- the receive buffer is forced to 8 MiB;
- the collector exports `kpi_receiver_drops_total` and a per-run `receiver_drops`. `run_kpis.py` records it and prints a warning, so loss at the instrument is never counted as network loss without a flag.

Round 5 ran on the fixed collector: 0 receiver drops in both runs.

Any earlier KPI run that started after a long idle period could have had the same artefact. Of the
71 KPI instances under `docs/evidence`, only three lost any URLLC packet: this one (all 10 devices)
and the two FIFO runs of 2026-11-08 (one device each, its radio link failed). None of the others
matches this signature, where every device loses equally.
