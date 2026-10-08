# Evidence: the network orchestrator (ADR-017)

Open5GS stack, 100 UEs (20 eMBB, 10 URLLC, 70 mMTC), 2026-10-08. free5GC was left running.
Produced by `scripts/open5gs/netorch_demo.sh` (`make o5gs-network-demo`). Each operation lists its
steps with their timings; `ops-last-run.json` holds every operation as JSON.

## 1. Lifecycle of the mMTC slice — `1-lifecycle.txt`

| Operation | Time | Steps |
|---|---|---|
| **deactivate** | **14.5 s** | stop admitting 0.0 s · drain 0.2 s · deregister and stop 70 UEs 10.2 s · stop gNB, data network, UPF 2.5 s · verify 0.6 s |
| **activate** | **12.2 s** | UPF + PFCP association with the SMF 2.7 s · data network 0.4 s · gNB + NG Setup 1.5 s · 70/70 UEs with a session 4.1 s · data path 3/3 UEs 3.5 s · admit 0.0 s |

| | mMTC active | mMTC dormant |
|---|---|---|
| mMTC sessions in the core (SMF) | 70 | **0** |
| eMBB / URLLC UEs | 20 / 10 | 20 / 10 |
| nr-ue processes | 100 | 30 |
| UE container CPU / memory | 211 % / 200 MiB | **40 % / 75 MiB** |
| mMTC gNB + UPF + data network | running | stopped |
| A sensor demand | admitted to mMTC | refused: "slice 3/334455 (mMTC) is not active" |

After activation, all 70 IoT devices were reporting to their data network again
(`kpi_devices_active{slice="mMTC"} 70`).

**The other slices were not disturbed.** URLLC's latency probes ran for the whole experiment:
63 891 received, 0 lost, one-way mean 1.7 / p95 2.8 / p99 6.9 ms.

Activation time varies with the SMF. It associated with a fresh UPF in 2.7 s here, and in 15.4 s in
an earlier manual run (24.5 s activation).

## 2. Rollback — `2-rollback.txt`

With `ACTIVATE_DEADLINE=10`, the SMF had not associated with the fresh UPF within 10 s. The
activation was **rolled back in 11.2 s**: the UPF it had started was stopped again. mMTC was left
DORMANT with 0 containers running, eMBB and URLLC kept all their UEs, and no UEs had been unparked,
so none were touched. A normal activation right after succeeded in 12.1 s.

## 3. Closed loop on demand — `3-closed-loop.txt`

`NETORCH_AUTO=1`, on-demand slice mMTC, idle time 60 s, plan window 30 s, a tick every 5 s. Demand
from `traffic_gen.py` at 1 demand/s.

| Phase | Demand | What the network orchestrator did |
|---|---|---|
| A, 120 s | phones only | **deactivated mMTC** ("idle: no demand for 67s"), 17.3 s |
| B, 120 s | IoT devices ask for service | **activated mMTC** ("demand: 0.01 Mbps needs mMTC"), 17.2 s |
| C, 150 s | phones only | **deactivated mMTC** again ("idle: no demand for 68s"), 14.3 s |

The IoT demands of phase B, in order (r = refused, A = admitted):

```
rrrrrrrrrrrrrrrrrrrrrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
```

21 refused, while the loop saw the demand and the slice was brought up. Every one after that, 81,
was admitted. The refused demands are what triggered the activation: they are logged, so `/plan`
counts them. That is the cost of on-demand: the first ~20 s of a new slice's demand is not served.

## 4. Compute per slice — `4-compute.txt`

The same load each time: TCP downlink for 60 s on 5 eMBB UEs. Only the CPU quota of eMBB's gNB and
UPF changes.

| eMBB gNB + UPF CPU quota | eMBB carried | URLLC probes meanwhile (mean / p95 / p99) |
|---|---|---|
| unlimited (12 cores) | 366 Mbps | 2.1 / 5.7 / 11.0 ms |
| 1 CPU | 121 Mbps | 2.0 / 5.0 / 10.4 ms |
| 0.5 CPU | 50 Mbps | 1.9 / 4.5 / 8.1 ms |

The quota controls eMBB's capacity almost in proportion. URLLC's tail improves, but modestly: p99
11.0 → 8.1 ms for 86 % less eMBB. Part of eMBB's CPU cost is in the shared UE container and the
data network, which the quota on gNB and UPF does not reach. So the lever works, and whether the
trade is worth it is a policy decision; the default leaves eMBB unlimited.

## Under heavy load: stale sessions in the core (found in the screenshot run)

During `scripts/open5gs/capture_orchestrator_screens.sh`, mMTC was deactivated while 3 demands/s
loaded the host. 11 of the 70 deregistrations did not reach the core before the UEs stopped. The
core kept counting 11 mMTC sessions while the slice was off, until the UEs re-attached on activation
(screenshot `docs/screenshots/open5gs/15-ues-and-core-sessions-per-slice.png`). The slice was
genuinely down, so the operation succeeded, but this used to show only in a detail line. It is now
reported as a **WARNING** on the operation. This is not fixed: nothing on this stack can make the
AMF release a context for a UE that is already gone.

## Screenshots

`docs/screenshots/open5gs/10-*.png` to `21-*.png`: the new Grafana panels, captured over one
scripted 5-minute scenario (controller under load, mMTC off and on, ARP pre-emption) by
`scripts/open5gs/capture_orchestrator_screens.sh`.

## Reproduce

```bash
make o5gs-network                   # state of every slice
python3 tools/network-orchestrator/netorch.py deactivate mMTC
python3 tools/network-orchestrator/netorch.py activate mMTC
make o5gs-network-demo              # all four experiments, ~16 minutes
```
