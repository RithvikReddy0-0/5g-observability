# Evidence: which algorithm is best (ADR-018)

Summary and state-of-the-art table: [`docs/algorithm-comparison.md`](../../algorithm-comparison.md).

## `controller/`: the eMBB admission limit, live

`scripts/open5gs/controller_benchmark.sh`: five control laws (static, AIMD, PI, MPC, UCB), 300 s
each on the 100-UE stack, under the same seeded demand (`SEED=42`, 3 demands/s, video, file, blog,
control). Round 1 ran static → UCB and round 2 UCB → static (`FIRST_ROUND=2`), so the load one run
leaves behind does not favour the same law twice. 0 leaked flow processes in all 10 runs.

| File | Contents |
|---|---|
| `comparison.md`, `comparison.csv`, `comparison.xlsx` | the summary (mean of rounds) and every run |
| `runs.txt` | each run's time window, host load before/after, leaked processes |
| `demand-<run>.txt` | every demand: admitted or refused, and why |
| `flows-<run>.json` | every executed flow: requested vs delivered rate, loss |
| `control-<run>.json` | every move of the limit and the law's reason (PI error terms, MPC's fitted model, UCB's arm means) |
| `urllc-kpi-<run>.json` | the URLLC collector's statistics over the run (every probe packet) |
| `series-<run>-*.json` | Prometheus series: URLLC p95 (10 s), eMBB limit, eMBB measured load |

Result: **PI** is 0.0 % of the time over URLLC's 10 ms budget in both rounds, has the best p99
(13.0 ms), meets 100 % of eMBB flows and delivers 261 Mbps, as much as no control at all.

## `simulation/`: admission, sizing, switching

- `tools/slice-orchestrator/simulate.py` drives the orchestrator's real `decide()` with a simulated
  clock (20 000 s, Poisson arrivals, fixed seeds): `simulation.md` / `.json`.
- `tools/network-orchestrator/simulate_autoscale.py` drives the network orchestrator's real
  `auto_tick()` over 6 hours of periodic IoT bursts, with the activation time measured live:
  `autoscale.md` / `.json`.

Both are deterministic and run in seconds, so anyone can reproduce them:

```bash
python3 tools/slice-orchestrator/simulate.py
python3 tools/network-orchestrator/simulate_autoscale.py
```
