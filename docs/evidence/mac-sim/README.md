# Evidence: MAC scheduler simulation (ADR-022) — SIMULATION, not the testbed

Every number here comes from `tools/mac-sim/macsim.py`, a slot-level downlink simulator of **one
modelled 5G NR cell**. None was measured on the Open5GS/UERANSIM stack. The radio and channel are
synthetic, and the assumptions are listed in [ADR-022](../../adr/ADR-022-mac-scheduler-simulation.md)
and [`tools/mac-sim/README.md`](../../../tools/mac-sim/README.md).

## Reproduce

```bash
python3 tools/mac-sim/macsim.py            # 440 runs; about 7 minutes on 11 worker processes
python3 tools/mac-sim/macsim.py --report-only   # rebuild results.md and the plots from results.json
python3 tools/mac-sim/test_macsim.py       # the unit tests (also in CI)
```

The command is `python3 tools/mac-sim/macsim.py --seeds 10 --duration 5.5 --warmup 0.5` (the
defaults). It is deterministic: the same command writes the same `results.json`, `summary.csv` and
`runs.csv`, whatever the number of worker processes. A stored run re-run on its own matched exactly.

| File | Contents |
|---|---|
| `results.md` | every table below and the paired differences (generated) |
| `results.json` | configuration, summary (mean and 95 % CI half-width per metric) and all 440 runs |
| `summary.csv` | the summary, one row per (scenario, inter-slice, intra-slice) |
| `runs.csv` | every run, one row per (scenario, scheduler, seed) |
| `inter-*.png` | inter-slice comparison (eMBB throughput, URLLC miss, SE of used RBGs), drawn by the script from the summary |

## Setup in one paragraph

One cell at 30 kHz SCS (0.5 ms slots), 40 MHz = 106 PRBs, 14 RBGs. CQI per RBG maps to rate through
TS 38.214 Table 5.2.2.1-2. The fading is synthetic: frequency-selective Rayleigh, 3 km/h. CQI is
reported every 2.5 ms, with no CSI error and no HARQ. Slices and traffic follow the testbed:
**eMBB** 20 UEs, **URLLC** 3 critical + 7 standard devices sending 64 B every 20 ms with a 10 ms budget,
and **mMTC** 70 devices sending 10 B every 10 s. Shares are 60 / 10 / 5 % (the packet scheduler's).
Scenarios:

- **normal**: eMBB offers 30 Mbps.
- **overload**: eMBB full buffer.
- **urllc-flood**: overload, plus the 7 standard URLLC devices flood at 14.3 Mbps (the ADR-019 B analogue).
- **spread**: as urllc-flood, with a wide SNR spread and strongly frequency-selective fading.

Each scheduler runs 10 seeds × 5.5 s (0.5 s warm-up excluded).

## Headline tables (mean ± 95 % CI, 10 seeds)

### Inter-slice schedulers (intra: eMBB PF, URLLC M-LWDF, mMTC RR)

| Scheduler | overload: eMBB (Mbps) | overload: URLLC miss (%) | urllc-flood: eMBB (Mbps) | urllc-flood: URLLC miss (%) | urllc-flood: critical miss (%) | spread: SE of used RBGs (bit/RE) | spread: URLLC miss (%) |
|---|---|---|---|---|---|---|---|
| static split | 74.1 ± 2.9 | 1.63 ± 1.21 | 74.1 ± 2.9 | 53.2 ± 8.2 | 1.20 ± 1.18 | 2.09 ± 0.28 | 65.5 ± 9.2 |
| NVS | 78.1 ± 3.8 | 0.22 ± 0.29 | 74.4 ± 3.0 | 39.8 ± 8.1 | 0.11 ± 0.18 | 2.14 ± 0.29 | 56.9 ± 10.8 |
| priority (HTB-like, ADR-019 analogue) | 85.3 ± 3.4 | **0.07 ± 0.12** | 55.2 ± 2.2 | 14.7 ± 6.2 | 0.24 ± 0.29 | 1.89 ± 0.26 | 37.5 ± 13.2 |
| quota-seq (ablation) | 85.4 ± 3.4 | 1.87 ± 1.35 | **77.1 ± 3.1** | 57.1 ± 7.9 | 1.35 ± 1.34 | 2.11 ± 0.29 | 65.9 ± 9.1 |
| RadioSaber | **85.5 ± 3.4** | 0.35 ± 0.35 | 73.1 ± 2.8 | 34.7 ± 8.4 | 0.75 ± 0.99 | **2.18 ± 0.29** | 52.0 ± 11.5 |
| none (one M-LWDF, no slicing) | 85.3 ± 3.4 | 0.07 ± 0.12 | 54.1 ± 3.6 | **5.1 ± 3.9** | 0.01 ± 0.03 | 1.92 ± 0.29 | **29.2 ± 12.0** |

### Paired by seed (same radio, same packets): A − B, mean ± 95 % CI

| Comparison | Scenario | Δ SE of used RBGs (bit/RE) | Δ eMBB (Mbps) | Δ URLLC miss (pp) |
|---|---|---|---|---|
| RadioSaber − quota-seq (**channel awareness alone**) | overload | +0.016 ± 0.002 | +0.11 ± 0.07 | −1.52 ± 1.01 |
| | urllc-flood | −0.007 ± 0.013 | −3.97 ± 0.25 | −22.4 ± 2.3 |
| | spread | +0.070 ± 0.028 | −1.30 ± 0.38 | −13.9 ± 4.5 |
| RadioSaber − static split | overload | +0.020 ± 0.003 | +11.46 ± 0.48 | −1.28 ± 0.88 |
| priority − static split | urllc-flood | −0.319 ± 0.105 | −18.87 ± 0.74 | −38.5 ± 3.9 |

### Intra-slice schedulers (inter: static split, the same scheduler in all slices)

| Scheduler | overload: eMBB (Mbps) | overload: eMBB 5th pct UE (Mbps) | overload: eMBB Jain | urllc-flood: URLLC miss (%) | urllc-flood: critical miss (%) |
|---|---|---|---|---|---|
| Round Robin | 49.4 ± 2.9 | 0.70 ± 0.16 | 0.69 ± 0.03 | 63.2 ± 7.2 | 4.95 ± 3.66 |
| Max-C/I | **127.1 ± 2.7** | 0.00 ± 0.00 | 0.18 ± 0.03 | 66.2 ± 2.4 | 53.3 ± 16.2 |
| Proportional Fair | 74.1 ± 2.9 | **1.42 ± 0.29** | **0.79 ± 0.03** | 59.1 ± 6.2 | **1.01 ± 1.06** |
| M-LWDF | 74.1 ± 2.9 | 1.42 ± 0.29 | 0.79 ± 0.03 | **53.2 ± 8.2** | 1.20 ± 1.18 |
| EXP/PF | 74.1 ± 2.9 | 1.42 ± 0.29 | 0.79 ± 0.03 | 53.5 ± 8.1 | 1.12 ± 1.13 |

The full tables, with every metric and scenario (normal included), are in [`results.md`](results.md).

## What the numbers say (in this model)

1. **Work conservation is the biggest throughput lever.** Every policy that lends idle RBGs within the slot carries
   about 85 Mbps of eMBB in overload, against 74 for the static split (+11.5 Mbps paired, +15 %).
2. **Channel awareness between slices pays off for the small slice, not in cell throughput.** With
   RadioSaber's quotas fixed (the quota-seq ablation), choosing RBGs by channel cuts URLLC misses by
   22 pp under the flood and 14 pp on the spread radio. The SE of the RBGs used rises by 0.07 bit/RE
   (+3 %) on the spread radio. That is far below the paper's 17–72 % throughput gain. Here one slice
   (eMBB, 20 UEs, 80 % of the weight) dominates, and its own PF scheduler already finds a good UE on
   almost every RBG. RadioSaber's evaluation used many slices with comparable weights.
3. **The ADR-019 policy is the best for URLLC among the sliced policies**, and costs eMBB the most
   when URLLC floods (−18.9 Mbps against static). That is the same trade-off ADR-019 measured: URLLC
   borrows first. Without slicing (one M-LWDF), URLLC does better still, but nothing isolates eMBB
   from it (54 Mbps).
4. **Inside a slice:** Max-C/I buys +72 % eMBB throughput by starving the edge (5th percentile 0,
   Jain 0.18). PF is the fair choice. Under the URLLC flood, Max-C/I lets the flooders starve the
   critical devices (53 % miss), while PF, M-LWDF and EXP/PF keep them near 1 %. The delay-aware two
   lower the total URLLC miss rate (53 % against 59 % for PF).
5. **Coverage dominates the spread radio.** Edge UEs below CQI 1 cannot be served by any scheduler
   (eMBB 5th percentile ≈ 0.2 Mbps, mMTC p95 delay ~0.1 s with wide intervals).
