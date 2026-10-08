# ADR-017 — Network orchestrator: slice lifecycle, closed loop on demand, compute per slice

**Status:** Accepted, 2026-10-08 (thresholds provisional, see below)
**Builds on:** ADR-011 (a gNB, UPF and data network per slice), ADR-014 (three slices, 100 UEs),
ADR-016 (slice orchestrator: priority, feedback controller, planning)
**Implements:** the guide's "network orchestrator" — the network part of orchestration, which the
slice orchestrator (traffic part) does not cover

## Context

The orchestrator built so far works on **traffic**. It decides which existing slice serves a
demand, at what priority, and within what admission limit. ADR-016's `/plan` can say "this demand
needs three slices with these capacities". Nothing acted on that answer. Slices existed because
someone had run the deploy scripts, and stayed until someone stopped them.

A network orchestrator works on the **network**: whether a slice exists in a working state, and
with what resources. The standards split it the same way:
- 3GPP slice management (NSMF/NSSMF, TS 28.531) creates, activates, modifies and terminates slice
  instances;
- an ETSI NFV orchestrator instantiates the network functions behind them;
- slice admission control (NSACF) and O-RAN rApps work on traffic, above the core.

## Decisions

### 1. A slice's lifecycle is driven step by step, each step verified

`tools/network-orchestrator/netorch.py`, a host process like the slice orchestrator (`:9113`,
`scripts/open5gs/start_netorch.sh`, `make o5gs-network`). A slice is its gNB, UPF and
data-network containers (ADR-011), its subscribers' UEs, and its admission switch in the slice
orchestrator.

| Operation | Steps, in order — each one checked before the next |
|---|---|
| **activate** | start the UPF and wait until the SMF has associated with *this* UPF process (an older association loses every session, ADR-014) → start the data network → start the gNB and wait for NG Setup with the AMF → bring the UEs back and wait for their PDU sessions (≥ 95 %) → ping the UPF gateway from sampled UEs through their own interfaces → let the slice orchestrator admit |
| **deactivate** | stop admitting → let admitted flows drain (30 s at most) → deregister the UEs (switch-off), which releases their sessions in the core → stop gNB, data network and UPF → check that the slice has no UE up, that every other slice kept all its UEs, and the core's own session count |
| **scale** | set the CPU quota of the slice's gNB and UPF, and read it back |

**Rollback.** If any activation step fails, or the whole activation misses its deadline
(`ACTIVATE_DEADLINE`, 150 s), everything the operation started is stopped again, in reverse order.
The UEs are parked again only if they were brought up. The slice ends dormant and clean, not half
up.

**Stateless.** A slice's state (ACTIVE, DORMANT, DEGRADED) is always discovered from the
infrastructure — containers running, UEs parked or up — never remembered. A restart of the
orchestrator or of the Docker engine, or a manual `docker stop`, cannot leave it believing
something false. Operations are serialised, one at a time.

### 2. A closed loop on the slice orchestrator's plan

With `NETORCH_AUTO=1`, every 10 s the network orchestrator reads `/plan` for the last 60 s of
demand.
- An on-demand slice (`ON_DEMAND`, default mMTC) that is dormant is activated as soon as the plan
  needs it.
- One that is active is deactivated after `IDLE_SECONDS` (120 s) with no demand, and never sooner
  than `MIN_UP` (60 s) after its last change. That hysteresis stops it flapping.
- eMBB and URLLC are not on-demand by default.

This closes the loop the two orchestrators form. Demands for a dormant slice are refused, but they
are still logged, so the plan sees them and the slice is brought up. Each tick also re-asserts the
slice orchestrator's admission switches from the discovered state, in case that orchestrator
restarted and forgot them.

### 3. Compute is a slice resource the orchestrator allocates

ADR-016 found that the slices compete for the host's CPU. eMBB load inflated URLLC's latency even
though they share no gNB, UPF or data network. `scale` sets a slice's CPU quota through
`docker update --cpus`, read back to confirm. This is the NFV orchestrator's resource-allocation
job, on the resource that is actually contended here.

### 4. UEs of a dormant slice are parked, not failed

The UE supervisor (ADR-014) restarts any UE whose session stops answering, one at a time with up
to 60 s each. Stopping a slice's gNB would have kept it busy for over an hour with 70 dead mMTC
UEs, and the other slices' UEs would have gone unsupervised meanwhile.
- `ran/ue-slice.sh park <slice>` puts the slice on a parked list first, so the supervisor skips it.
  It then deregisters its UEs ten at a time and stops them.
- `unpark` starts all of them at once (safe since ADR-014).
- The parked list lives in `/tmp`, which survives a restart of the UE container, so a restart
  keeps a dormant slice dormant.
- The UE helpers moved into `ran/ue-lib.sh`, shared by the entrypoint and `ue-slice.sh`.
- `deploy.sh` refuses to run while a slice is dormant: it would half-restart it, and its KPI gate
  checks every slice.

## Results

See [`docs/evidence/open5gs-network-orchestrator/`](../evidence/open5gs-network-orchestrator/README.md).

| Experiment | Result |
|---|---|
| Deactivate mMTC | 14.5 s; core sessions 70 → 0; eMBB and URLLC kept every UE; UE container CPU 211 % → 40 %, memory 200 → 75 MiB |
| Activate mMTC | 12.2 s (24.5 s when the SMF took 15 s to associate); 70/70 UEs; data path checked; IoT reports resume |
| Other slices during the cycle | URLLC probes: 0 lost of 63 891, p95 2.8 ms |
| Activation that missed a 10 s deadline | rolled back in 11.2 s; slice dormant with 0 containers; a normal activation then succeeded |
| Closed loop | mMTC deactivated after 67 s without IoT demand; activated when IoT demand arrived (21 demands refused meanwhile, then 81 admitted); deactivated again 68 s after it stopped |
| eMBB CPU quota unlimited / 1 / 0.5 | eMBB 366 / 121 / 50 Mbps; URLLC p99 11.0 / 10.4 / 8.1 ms |

## Problems found on the way

1. **`nr-cli` lost commands to stale entries.** `/tmp/UERANSIM.proc-table/` keeps one file per UE
   process ever started, and `/tmp` survives container restarts. There were 154 files for 100 UEs.
   PIDs are reused across restarts, so a stale file can carry a live PID. `nr-cli` sent commands to
   dead ports, and they were lost silently: 38 of 70 deregistrations never arrived. Entries are now
   pruned by whether their CLI port is bound, and the table is cleared when the container starts.
2. **Killing a UE on `nr-cli`'s answer cut its deregistration short.** `nr-cli` reports
   "triggered" before the UE has sent its request. Killing the UE then left 51–58 of 70 sessions in
   the core. Each UE now switches itself off (about 0.5 s), and only a straggler is killed. With
   both fixes, the core releases 69–70 of 70 sessions.
3. **`docker update --cpus 0` is accepted and changes nothing.** "Unlimited" left a 1.5-CPU quota
   in place while the operation reported success. Unlimited is now a quota of every host core, and
   every quota is read back.
4. **The Kubernetes manifest would have broken the UE pod.** Its ConfigMap carried only the two
   entrypoints, and the UE entrypoint now sources `ue-lib.sh`. `render.py` now ships `ue-lib.sh` and
   `ue-slice.sh` as well. This is rendered and checked, but not run on minikube in this phase.

## Consequences

- New: `tools/network-orchestrator/` (`netorch.py`, 13 unit tests in CI), `ran/ue-lib.sh`,
  `ran/ue-slice.sh`, `scripts/open5gs/start_netorch.sh`, `scripts/open5gs/netorch_demo.sh`,
  `make o5gs-network`, `make o5gs-network-demo`.
- Changed: the slice orchestrator has a per-slice admission switch (`POST /slices`, metric
  `slice_active`); Prometheus scrapes the network orchestrator; there is a Grafana row for it.
- **Provisional, to confirm with the guide:** which slices are on-demand (mMTC only), the idle time
  (120 s), the activation deadline (150 s), and the 95 % UE share an activation must reach.
- **Not done:** creating a slice that has no template (a new S-NSSAI needs AMF/NSSF/SMF
  configuration and a core restart, which `deploy.sh` owns); per-slice CPU of the shared UE
  container, which `docker update` cannot split; the network orchestrator on Kubernetes, where its
  operations would be `kubectl scale` instead of `docker start/stop`.
