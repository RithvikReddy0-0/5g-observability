# Evidence: dynamic slice allocation (ADR-016)

Open5GS stack, 100 UEs (20 eMBB, 10 URLLC, 70 mMTC), 2026-10-08. free5GC was left running, as in
the KPI baseline. Produced by `scripts/open5gs/orchestrator_dynamic.sh` (`make o5gs-dynamic`).
Admitted demands run as real downlink UDP flows (`flowgen.py`) from the slice's data network,
through its UPF and gNB, to a device on that slice. A flow is `met` when it delivers ≥ 95 % of its
rate with ≤ 2 % loss.

Every run records host load before and after, and the number of flow processes that outlived their
flow: 0 in every run here. **The runs in `superseded-iperf3-load/` must not be quoted** — the old
iperf3 flows loaded the host enough to distort URLLC latency; its README explains how that was found.

## 1. Priority inside URLLC — `1-priority.txt`

The 3 critical devices (…021–023) have ARP 1 and may pre-empt. The 7 standard devices (…024–030)
have ARP 2 and may be pre-empted. Each demand is 1 Mbps for 40 s; URLLC admits 20 Mbps.

| Step | Result |
|---|---|
| 20 standard demands | all admitted: 20/20 Mbps |
| 1 more standard demand | **refused**: an equal priority cannot pre-empt |
| 5 critical demands | **all admitted, each pre-empting exactly one standard flow** |
| 1 more standard demand | refused |
| 3 IoT sensor demands | admitted to mMTC |

| Flows by requester | met | pre-empted |
|---|---|---|
| URLLC ARP 1 (critical) | 5 / 5 | — |
| URLLC ARP 2 (standard) | 15 / 15 surviving | 5, stopped within a second (delivered ≤ 0.02 Mbps) |
| mMTC ARP 12 | 3 / 3 | — |

The same run gave identical results twice.

## 2. Feedback controller — `2-comparison.txt`

Each run: the same demand for 180 s, 3 demands/s (video, file, blog, control), 20 s flows. First
with the controller off (`CONTROL=0`: eMBB admits up to its 500 Mbps policy capacity), then on.

| | controller off | controller on |
|---|---|---|
| eMBB demands admitted / refused | 210 / 0 | 89 / 186 |
| **eMBB flows that got their rate** | **68 %** (68 short) | **100 %** |
| eMBB delivered, average | 266 Mbps | 109 Mbps |
| eMBB admission limit | 500 (fixed) | 500 → 220 → 245 → 167 → 71 → 96 → 59 → 50 |
| URLLC flows that got their rate | 100 % | 100 % (116 / 116) |
| **URLLC one-way latency, mean / p95 / p99** | 5.1 / 20.5 / 55.4 ms | **3.2 / 12.5 / 34.0 ms** |
| URLLC probe loss | 0 | 0 |

With the controller off, the orchestrator admits more eMBB than this laptop can carry. A third of
those flows miss their rate, and URLLC's tail suffers. With it on, the limit falls to what can be
carried: every admitted eMBB flow gets its rate, and URLLC's p95 and p99 drop by about 40 %.

**What it does not do:** hold URLLC's p95 under the 10 ms target over the whole run. In the last
minute, with eMBB's limit between 96 and its 50 Mbps floor, the controller still read p95 values of
9–19 ms. So some of the remaining tail is not eMBB's doing. The price is eMBB volume: 109 Mbps delivered against 266.
But much of that 266 was broken flows.

`controller-throttling-urllc-too/` is the same experiment with `CONTROL_THROTTLE="eMBB URLLC"`.
URLLC p95 went 21.1 → 8.4 ms, but URLLC's own limit was cut to 2–5 Mbps and only 82 % of URLLC flows
got their rate. Section 4 shows URLLC's own flows do not inflate its latency, so that cost buys
nothing. The default is eMBB only.

Raw data: `2-flows-*.json` (every flow), `2-control-log-controlled.json` (every cut and raise, with
its reason), `2-series-*.json` (Prometheus series of the limit, admitted and measured load, and
URLLC p95), `2-urllc-kpi-*.json` (URLLC collector run statistics), `2-runs.txt` (times, host load).

## 3. Planning — `3-planning.txt`

| Demand set | Slices needed | Required (× 1.25 headroom) |
|---|---|---|
| Observed by the orchestrator in run 2 (Little's law) | 2 | eMBB 363 Mbps, URLLC 13.5 Mbps |
| Phase 2b device mix: 10 control, 20 video, 70 sensor | 3 | eMBB 200, URLLC 12.5, mMTC 0.88 Mbps |
| Delay-tolerant only: 30 video, 10 file | **1** | eMBB 487.5 Mbps |

Each answer also says whether the requirement fits the current admission limit. After run 2 the
eMBB limit was 50 Mbps, so the observed demand did not fit what the host could carry at that moment.

## 4. What inflates URLLC's latency — `4-diagnosis.txt`

The same generator for 120 s per row, controller off:

| Load | URLLC p95 | p99 | host load after |
|---|---|---|---|
| idle | 2.5 ms | 5.4 ms | 2.3 |
| URLLC demand only (control, blog) | 2.8 ms | 5.3 ms | 5.0 |
| eMBB demand only (video, file) | **8.9 ms** | **17.5 ms** | 36.9 |

eMBB demand is what hurts URLLC; URLLC's own flows do not. eMBB does not share URLLC's gNB, UPF or
data network (ADR-011). What they share is the laptop's CPU: carrying hundreds of Mbps through the
userspace gNB and UE is expensive. That is why the controller throttles eMBB.

## Reproduce

```bash
make o5gs-dynamic                      # all four, about 25 minutes
ONLY=priority bash scripts/open5gs/orchestrator_dynamic.sh
```
