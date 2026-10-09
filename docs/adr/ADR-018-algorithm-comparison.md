# ADR-018 — Choosing each orchestration algorithm by comparison, not by default

**Status:** Accepted, 2026-10-09
**Builds on:** ADR-016 (slice orchestrator: AIMD, ARP, planning), ADR-017 (network orchestrator)
**Summary table:** [`docs/algorithm-comparison.md`](../algorithm-comparison.md)

## Context

ADR-016 and ADR-017 each picked one algorithm per decision: AIMD for the admission limit, greedy
admission, mean × 1.25 sizing, reactive switching. Each was reasonable, but none had been compared
with the alternatives. The team asked for a state-of-the-art comparison, and for the best ones to
be implemented.

## Decisions

### 1. Compare the strongest candidates on this testbed, under the same conditions

For each decision, the strongest candidates from the literature were implemented behind a setting
and compared head to head.

| Decision | Candidates implemented | How compared |
|---|---|---|
| eMBB admission limit (protects URLLC latency) | static, AIMD, PI (PIE-style, RFC 8033), one-step MPC with an online least-squares model, UCB1 bandit (`CONTROL_POLICY`) | **live**, 300 s each, the same seeded demand (`SEED`), 2 rounds in opposite orders (`scripts/open5gs/controller_benchmark.sh`) |
| Admission when contested | greedy, trunk reservation, online-knapsack threshold pricing (`ADMISSION_POLICY`) | simulation, 20 000 s of Poisson demand through the real `decide()` (`tools/slice-orchestrator/simulate.py`) |
| Capacity planning | mean × 1.25, Kaufman–Roberts for a blocking target | the same simulation, plus KR's own prediction |
| Switching an on-demand slice | reactive, predictive (learned period) + reactive, always on (`AUTO_POLICY`) | simulation, 6 h of periodic IoT bursts through the real `auto_tick()`, with the live-measured activation time (`tools/network-orchestrator/simulate_autoscale.py`) |

**Live, or simulated?** Live wherever the network itself shapes the outcome. The controller acts on
measured latency, which depends on the host's CPU, so only a live run is honest. Admission,
sizing and switching are decisions about bookkeeping and timing. A simulation driving the real
decision code sees tens of thousands of demands, where a live run sees a few hundred.

**Fairness of the live comparison.**
- The same demand sequence (`SEED`, subscribers sorted).
- The same duration and rate.
- Two rounds in opposite orders, because each run leaves load behind on the host.
- Host load and leaked flow processes recorded for every run (none leaked).

### 2. Not implemented: deep reinforcement learning, and why

DRL is the family most reported as best in recent slicing papers, in simulation. Here, one control
step takes 5 s of real time, so training would take many hours of live load. Exploration would
mean deliberately violating URLLC's budget. The learned policy could not be explained in a review.
It is listed in the comparison with that reason, and the next step is stated: train it offline
against a model fitted to these measurements, then compare it with PI under the same seeded demand.

### 3. The winners become the defaults

| Setting | Default before | Default now | Evidence |
|---|---|---|---|
| `CONTROL_POLICY` | `aimd` | `pi` | live benchmark |
| `ADMISSION_POLICY` | (greedy only) | `reservation`, `RESERVE="URLLC=4"`, `RESERVE_EXEMPT="control sensor"` | control blocking 20.9 % → 2.1 %, most value carried |
| `/plan` `required_mbps` | mean × 1.25 | Kaufman–Roberts for `PLAN_BLOCKING` (1 %); the old figure stays as `mean_headroom_mbps` | 3.3–6.4 % blocking → 0.5–1.2 % |
| `AUTO_POLICY` | (reactive only) | `predictive` | refused 42 % → 3.6–5.1 % |

Every old behaviour stays one setting away, and the benchmark scripts reproduce every comparison.

## Results

Live, 300 s per run, the same seeded demand (3 demands/s), mean of 2 rounds in opposite orders
(`docs/evidence/open5gs-algorithm-comparison/controller/comparison.md`, every run in `comparison.csv`
and `comparison.xlsx`):

| Policy | URLLC time over 10 ms | URLLC p95 / p99 | eMBB flows met | eMBB delivered | limit std |
|---|---|---|---|---|---|
| static | 71.2 % | 25.4 / 52.7 ms | 60 % | 260 Mbps | 0 |
| AIMD (ADR-016) | 20.5 % | 7.2 / 21.0 ms | 87 % | 170 Mbps | 125 |
| **PI** | **0.0 %** | 5.8 / **13.0** ms | **100 %** | **261 Mbps** | **41** |
| MPC | 9.0 % | **5.1** / 15.5 ms | 95 % | 154 Mbps | 106 |
| UCB | 9.0 % | 5.4 / 17.2 ms | 97 % | 163 Mbps | 148 |

PI was 0.0 % over budget in both rounds, and delivered 239 and 282 Mbps. It is the only law that is
best on latency and on eMBB at once.

Simulation results are in [`docs/algorithm-comparison.md`](../algorithm-comparison.md) §2, §4 and §5.

## Consequences

- New: `tools/slice-orchestrator/simulate.py`, `compare_runs.py`, `tools/network-orchestrator/simulate_autoscale.py`,
  `scripts/open5gs/controller_benchmark.sh`; `traffic_gen.py` takes `SEED`.
- Kaufman–Roberts is exact for Poisson arrivals and complete sharing (its blocking does not depend
  on how long demands hold, only on the mean). Bursty or correlated arrivals block more than it
  predicts. Size with a margin there, or feed it a forecast.
- Trunk reservation needs its reserve tuned. 4 Mbps protects control traffic at the loads simulated
  here; a different mix needs the simulation re-run.
- The predictive switch only helps when demand has a period. Without one it behaves like the
  reactive one, at no cost.
- Pre-emption is unchanged. With every URLLC flow at 1 Mbps, the multi-criteria policies of
  de Oliveira et al. choose the same victims as the 3GPP rule.
