# Superseded: runs where the load generator loaded the host

**Do not quote numbers from this folder.** Every run here was measured on a host that our own
traffic tool was overloading. The folder is kept because it shows how the problem was found.

## What was wrong — two things, found one after the other

The orchestrator ran each admitted demand as an iperf3 UDP flow inside the containers.

1. **Leaks.** If a flow overran, the orchestrator's timeout killed only the `docker exec` wrapper,
   never the iperf3 inside the container. A client whose 20 s test had gone wrong was found still
   running five minutes later, at 90 % CPU.
2. **Busy-waiting, even without leaks.** With the leaks fixed (both ends under `timeout -s KILL`),
   the host's 1-minute load still went from about 1 to 30 under URLLC demand and to 57 under eMBB
   demand. The cause: iperf3 3.16's UDP sender paces by busy-waiting. Each flow's sender took
   45–90 % of a core whatever its rate — 90 % for a 0.01 Mbps sensor flow. Twenty flows asked for
   more cores than the VM has.

Either way, the CPU contention inflated exactly what the feedback controller watches: URLLC's
latency tail. So the controller was reacting to the load generator, not to the network.

## What it looked like

| Folder | What it seemed to show |
|---|---|
| `controller-v1-embb-only/` | Controller throttling eMBB only: eMBB pinned at its 50 Mbps floor within a minute; URLLC p95 still 12–40 ms |
| `4-diagnosis.txt` | URLLC p95: idle 2.9 ms, URLLC demand only 16.2 ms, eMBB demand only 10.6 ms (host load 23.5 and 53.8) |
| `controller-v2-embb-and-urllc/` | Controller throttling eMBB and URLLC: both pinned at their floors (50 and 2 Mbps); URLLC p95 still 11–39 ms; 12 of 22 URLLC flows got their rate on 2 Mbps |
| `after-leak-fix-iperf-still-busy/` | Leaks fixed, iperf3 still in use. Diagnosis: URLLC p95 25.7 ms (URLLC demand) and 31.3 ms (eMBB demand), host load 30 and 57 |

The third row gave it away: 2 Mbps of URLLC flows cannot congest anything. The fourth row showed
the leak fix alone was not enough.

## The fix

Executed flows now use `tools/slice-orchestrator/flowgen.py`, a paced UDP sender that sleeps
between bursts, and a receiver on the device. Measured: 1–10 % of a core per process, every flow at
its exact rate with 0 % loss. Both ends still run under `timeout -s KILL`. Every run records the
host load before and after, and the number of flow processes that outlived their flow. See the
parent folder for the clean runs.
