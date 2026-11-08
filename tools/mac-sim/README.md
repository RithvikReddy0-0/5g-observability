# MAC scheduler simulator (ADR-022) — SIMULATION ONLY

A slot-based downlink MAC simulator for **one 5G NR cell** carrying the project's three slices. It
answers the question the testbed cannot: *what would a real MAC scheduler do?* UERANSIM has no MAC
layer, PRBs or channel, so the testbed's packet scheduler (ADR-019) works on IP packets. This tool
models the radio instead. It does not touch the testbed. Every number it produces is a simulation result.

Design, assumptions, results and limitations: [ADR-022](../../docs/adr/ADR-022-mac-scheduler-simulation.md).
Evidence: [`docs/evidence/mac-sim/`](../../docs/evidence/mac-sim/README.md).

```
python3 tools/mac-sim/test_macsim.py                    # unit tests, a few seconds (in CI)
python3 tools/mac-sim/macsim.py                         # full study: 4 scenarios x 11 schedulers x 10 seeds
python3 tools/mac-sim/macsim.py --seeds 3 --duration 2  # quicker
python3 tools/mac-sim/macsim.py --one overload radiosaber default 1    # one run, metrics as JSON
```

Standard library only (Python 3.8+). Plots are drawn only if matplotlib is installed. The study runs
in parallel (`--jobs`, default: CPU count − 1); the results do not depend on it.

## What is modelled

| Part | Model | Source / status |
|---|---|---|
| Carrier | numerology 1: 30 kHz SCS, 0.5 ms slots; 40 MHz = 106 PRBs | TS 38.101-1 Table 5.3.2-1 |
| Allocation | type 0, RBGs of 8 PRBs (13 × 8 + 1 × 2) | TS 38.214 Table 5.1.2.2.1-1 (BWP 73–144, config 1) |
| Rate | CQI per RBG → spectral efficiency × 132 RE per PRB (12 PDSCH symbols, 12 DMRS RE), 1 layer | TS 38.214 Table 5.2.2.1-2 (4-bit CQI table) |
| SNR → CQI | highest CQI with efficiency ≤ log2(1 + SNR / 3 dB) | **assumption** |
| Channel | mean SNR from a distance (exponent 3.5) with log-normal shadowing; 4-tap Rayleigh, Gauss-Markov taps with ρ = J0(2π f_d T), 3 km/h at 3.5 GHz | **assumption** (synthetic) |
| CSI | CQI reported every 5 slots, channel held constant in between, no error | **assumption**: so no BLER and **no HARQ** |
| Traffic | eMBB 20 UEs (Poisson 1500 B or full buffer); URLLC 3 critical + 7 standard, 64 B / 20 ms, 10 ms budget, discarded past it; mMTC 70 × 10 B / 10 s | the testbed's profiles (ADR-015) |

Intra-slice: Round Robin, Max-C/I, Proportional Fair, M-LWDF, EXP/PF.
Inter-slice: static split, NVS, priority (HTB-like, the ADR-019 analogue), quota-seq (ablation),
RadioSaber, none (one M-LWDF scheduler over all UEs). Shares: eMBB 60 %, URLLC 10 %, mMTC 5 %, the
packet scheduler's guarantees.

## Importing real channel traces

The simulator does not download anything. A real CQI trace (for example one recorded with an SDR,
as the RadioSaber authors did) can replace the synthetic channel. Write a class with the same
interface as `FixedChannel`: `at(period)` returns a list of 14 CQIs (one per RBG) for CSI report
period `period`. Then give it to each `UE` in `build()`. Read the file locally and resample it to the
RBG grid and the 2.5 ms report period. Keep one trace per UE, so that every scheduler still sees the
same channel.
