# ADR-015 — Per-slice traffic, KPI scorecard, sweeps and the Excel report (Phase 2b, Tracks 2–4)

**Status:** Accepted, 2026-10-01 (traffic values, weights and sweep parameters provisional, see below)
**Builds on:** ADR-014 (three slices, 100 UEs)
**Implements:** Tracks 2, 3 and 4 of [`docs/briefs/phase2b-work-division.md`](../briefs/phase2b-work-division.md),
against the team's KPI notes ([review](../briefs/phase2b-kpi-targets-review.md))

## Context

The guide's minutes:
- 10 time-sensitive devices judged on latency;
- 20 bandwidth devices judged on throughput;
- 70 IoT devices that send 10 bytes periodically;
- a KPI per slice, compared with industry values;
- an Excel report of KPIs at all instances, varying one parameter.

The team's notes fixed the KPIs and their targets. ADR-014 delivered the stack. Nothing yet
generated each class's traffic or measured its KPIs.

## Decisions

### 1. Each slice carries its own kind of traffic (Track 2)

| Slice | Traffic | Where it runs |
|---|---|---|
| URLLC | 64-byte packets every 20 ms from each of its 10 devices | `traffic_agent.py` in the UE container, **always on** |
| mMTC | a 10-byte report every 10 s from each of its 70 devices, silent in between, random phase | same agent, **always on** |
| eMBB | TCP bulk download on as many devices as an instance asks for | `run_kpis.py`, **only during a measurement** — a permanent flood would distort everything else |

Every packet leaves from the device's own PDU-session address, so it crosses that slice's gNB and
UPF. The URLLC interval, the mMTC interval and the packet sizes are provisional: the notes fix the
KPIs, not the traffic.

### 2. Measured where it arrives, one-way, on one clock

`kpi_collector.py` runs in the URLLC and mMTC data networks. The sender puts its monotonic time in
each packet. Sender and receiver are containers on one kernel, and Docker gives them no separate
time namespace, so `CLOCK_MONOTONIC` is the same clock on both sides. This was checked: readings
taken UE, DN, UE came out in order.

Latency is therefore the exact one-way time from device to destination — the definition in the
notes. It needs no clock sync, and the VM's wall-clock steps can't touch it (ADR-014).
- **Loss** comes from per-device sequence numbers. An epoch byte keeps a restarted sender from
  counting as loss.
- **The device id is in the packet**, so the UPF's NAT on Kubernetes changes nothing.

The mMTC packet is exactly 10 bytes: device (2), epoch (1), sequence (3), time (4).

### 3. KPIs, targets and weights in one file (Track 3)

[`deployments/open5gs/kpi-scorecard.json`](../../deployments/open5gs/kpi-scorecard.json) has 12
KPIs, 9 of them scored. The notes' targets are used **as written**. The four added KPIs say where
they come from:
- URLLC loss (5QI 82 PER, TS 23.501);
- mMTC report latency (TR 38.913);
- URLLC jitter and eMBB mean throughput, both reported only.

Scoring: 1 when the target is met, otherwise the fraction of the way to it. A slice score is the
weighted sum of its KPI scores, and the overall score is the mean of the slices. The industry value
sits beside every score as "vs benchmark", per the notes' first assumption. The weights are
provisional. This file is **not** the deployment gate, which only asks whether every slice is up.

Explained for the team in [`docs/kpi-scorecard.md`](../kpi-scorecard.md). CI validates the file
and proves the scoring rules on known values.

### 4. Instances, one-parameter sweeps, Excel (Track 4)

An **instance** serves all three slices at once for a fixed window and scores the result. A
**sweep** repeats it while one parameter changes:
- eMBB devices downloading: 0, 5, 10, 20;
- IoT reporting interval: 1, 5, 10, 30 s;
- IoT devices attached: 70, 140, 210 — "later increase the UEs".

`tools/kpi/run_kpis.py all` writes `docs/evidence/open5gs-kpi/<run>/`:
- every instance raw (`instances.jsonl`);
- a CSV per sweep;
- `scorecard.md`;
- `kpi-report.xlsx`.

The workbook has sheets for the README, the standard scorecard, slice scores, each sweep, all
instances, eMBB per device, URLLC and mMTC per device, and the KPI definitions. It is written
with the standard library ([`tools/kpi/xlsx.py`](../../tools/kpi/xlsx.py)), so producing it
needs no `pip install`.

### 5. Live in Grafana

A "Phase 2b KPIs" row shows URLLC latency mean/p95/p99, loss for both measured slices, mMTC
devices reporting and report latency, from the collectors' Prometheus metrics (`kpi_*`).

## Found while deploying this

- **`deploy.sh` reported DEPLOYED for a change it never applied.**
  - The `docker compose` plugin was gone: Docker Desktop's engine, which provides it, was
    stopped. `compose up` failed unseen, the gate passed on the old containers, and the script
    reported success.
  - Separately, Prometheus was never restarted on deploy, so a new scrape job never loaded.
  - Now the script checks that `compose` works before touching anything, treats a failed
    `compose up` as a failed apply, and restarts Prometheus. Shown: same candidate, ABORTED,
    stack unchanged.
- **The standalone Compose v1.29 must not be used on this host.** It crashed on this Docker
  engine (`KeyError: 'ContainerConfig'`) and left two containers stopped and renamed. They were
  recreated by hand to match the compose file.
- **Measurement runs no longer need Compose to change the UE plan.** The UE entrypoint reads a
  one-off override file (`/tmp/ue-plan.override`), and a plain `docker restart` applies it. It
  also clears its startup record, because `/tmp` survives a restart.

## Result

Run `20261001-124845Z` ([evidence](../evidence/open5gs-kpi/README.md),
[deck](../presentation/phase2b-kpi-results.pdf)). The standard instance served all three slices at
once for 150 s, with 20 eMBB devices downloading:

| Slice | Key results | Score |
|---|---|---|
| URLLC | mean 3.9 ms ✅, p95 11.4 ms ❌, p99 22.0 ms ❌, 0 loss in 77 555 packets ✅ | 0.90 |
| eMBB | 5th percentile 10.8 Mbps ❌ (target 50), aggregate 299 Mbps ❌ (target 500) | 0.37 |
| mMTC | registration 100 % ✅, 0 report loss in 1 089 reports ✅, report latency p99 18 ms ✅ | 1.00 |

Overall score: 0.76.

What held in every one of the run's instances:
- **URLLC:** mean ≤ 4.3 ms and no packet lost. Its p99 exceeds 10 ms whenever eMBB is loaded: the
  slices share one CPU.
- **eMBB:** its 5th percentile reached 46 Mbps with 5 devices, but never more than 14 Mbps with 20.
  The laptop delivers 255–455 Mbps in total, varying that much between identical runs.
- **mMTC:** 100 % registration and zero loss with 70, 140 and 210 devices, and at reporting
  intervals of 1–30 s.

Found by checking the run's own numbers: the first device sweep counted registration for only 70
devices (the plan override was not read). Fixed and re-run; both runs are kept.

## Consequences

- Every Phase 2b KPI in the notes is measured on the running system and scored against its target,
  with the industry benchmark alongside.
- The traffic agent and collectors run permanently. They cost the UE container ~500 small
  packets/s, and the slices carry realistic background traffic in every other measurement.
- Provisional choices are listed in `docs/kpi-scorecard.md` and must be confirmed with the guide.
- The orchestrator work (priority within URLLC, deciding the number of slices) can now use these
  KPIs. It is not part of this ADR.
