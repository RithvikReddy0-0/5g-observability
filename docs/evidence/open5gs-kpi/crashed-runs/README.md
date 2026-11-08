# Runs that crashed (kept as the evidence for a runner fix)

Three standard KPI runs on 2026-11-08 ended without a scorecard. Each one started its 150 s
measurement and then the runner died: one eMBB iperf3 client never finished, `communicate()` raised
`TimeoutExpired` after 210 s, and everything measured in that run was lost.

| Run | Scheduler | What ended it |
|---|---|---|
| `20261108-132254Z` | sliced, 600 Mbps cell | runner exited without output (stdout was filtered in that script, so no traceback was kept) |
| `20261108-134313Z` | FIFO, 600 Mbps cell | `TimeoutExpired`: iperf3 client from 10.45.2.222, port 5600 |
| `20261108-134858Z` | FIFO, 600 Mbps cell | `TimeoutExpired`: iperf3 client from 10.45.3.13, port 5611 |

After the second FIFO run, 5 of 130 sessions had no data path and 2 interfaces were missing; the UE
logs held 4 "Radio link failure detected" lines. The stack was re-attached (`start_ues.sh`: 130
sessions, 0 broken) before anything else ran.

**Fix** (`tools/kpi/run_kpis.py`, `embb_load`): all flows share one deadline; a client still running
at the deadline is killed and recorded with `"timed_out": true` and no rate, so the run completes and
`embb.flows_completed` falls below `embb.ues_loaded`. The run log line now says `n/m flows
completed`. Checked with fake commands (three flows, one never ends): the old runner raises
`TimeoutExpired`, the new one returns at the deadline with two rates and one timed-out flow.
