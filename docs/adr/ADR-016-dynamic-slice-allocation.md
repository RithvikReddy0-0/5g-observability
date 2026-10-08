# ADR-016 — Dynamic slice allocation: priority inside a slice, a feedback controller, and planning

**Status:** Accepted, 2026-10-08 (controller thresholds and tier sizes provisional, see below)
**Builds on:** ADR-011/012 (orchestrator on real traffic), ADR-014 (three slices, 100 UEs),
ADR-015 (per-slice KPIs measured device to data network)
**Implements:** the shared orchestrator work of [`docs/briefs/phase2b-work-division.md`](../briefs/phase2b-work-division.md):
"priority within the URLLC slice, deciding how many slices a demand set needs, and allocating from
monitored load"

## Context

Before this, the orchestrator placed each demand on the cheapest slice that met its latency need
and refused it when that slice was full, counting measured load as well as admitted load. Three
things were static:

- **Every demand inside a slice was equal.** With URLLC's 20 Mbps taken, the most critical new
  demand was refused like any other.
- **The admission ceiling was a fixed number** from `slices.env`. The KPI notes set an eMBB
  aggregate target of 500 Mbps; this laptop delivers 255–455 Mbps between identical runs (ADR-015).
  A fixed ceiling is either above what the host delivers (admitted flows starve) or below the
  target (capacity left unused), and it never reacts to what the other slices experience.
- **The number of slices was a given**, not something derived from demand.

The KPI sweep (ADR-015) showed why the ceiling matters to the other slices: with eMBB devices
downloading, URLLC's one-way p95 rose from 3.7 ms to 7.5 ms and p99 from 8.6 ms to about 15 ms,
against a 10 ms budget.

The guide's direction: in the end, allocation has to be dynamic.

## Decisions

### 1. Priority inside URLLC is ARP sub-classes of one slice, not more slices

The open question in the team's notes ("sub-classes in one slice or separate slices") is decided
for sub-classes. Separate slices per priority would each need their own gNB, UPF and data network
on this design (ADR-011) — more containers than a laptop running 100 UEs can spare — and would
split 20 Mbps of URLLC capacity into pieces that cannot lend to each other.

3GPP already has the mechanism: **Allocation and Retention Priority** (TS 23.501 §5.7.2.2) — a
priority level, whether the flow *may pre-empt* others, and whether it *may be pre-empted*.

| URLLC devices | IMSIs | ARP priority | May pre-empt | May be pre-empted |
|---|---|---|---|---|
| critical | …021–023 | 1 | yes | no |
| standard | …024–030 | 2 | no | yes |

- Defined once, `SLICE_B_ARP_TIERS` in `deployments/slices.env`; `provision_subscribers.py` writes
  it into each subscriber's record; CI checks the tiers cover exactly the slice's UEs.
- The orchestrator reads each subscriber's ARP **from the core**, not from its own configuration.
- When a slice is full, a demand that may pre-empt stops admitted flows that may be pre-empted and
  have a strictly lower priority — lowest priority first, newest first, only as many as needed, and
  none at all if even all of them would not free enough. A pre-empted flow's real traffic is
  stopped (its sender is killed) and the flow is recorded as `preempted`.

**Boundary:** Open5GS stores ARP and sends it to the gNB in the session's QoS parameters, but
UERANSIM's gNB does not schedule by it. On this testbed ARP is therefore enforced at admission,
by the orchestrator. In a production RAN the same ARP would also act in the scheduler.

### 2. A feedback controller moves eMBB's admission limit

Each slice now has an **admission limit** at or below its policy capacity. A controller in the
orchestrator (`CONTROL=1`, on by default in `scripts/open5gs/start_orchestrator.sh`) steps every
5 s and moves the limits of the slices in `CONTROL_THROTTLE`, by default eMBB only:

| Signal | Source | Congested when |
|---|---|---|
| URLLC one-way p95, last 10 s | `kpi_owd_recent_ms`, the URLLC collector (new 10 s window) | > 8 ms (80 % of the 10 ms budget) |
| the slice's own flows that missed their rate | the orchestrator's executed flows, last 30 s | > 20 % of at least 4 flows |

- **Congested:** cut the limit to 0.7 × min(limit, load in use) — from the operating point, because
  cutting an unused limit changes nothing. Then hold for one flow's duration (20 s): a cut only
  takes effect as flows already admitted end.
- **Healthy** (p95 < 5 ms, flows met) **and demand pressing** (load ≥ 80 % of the limit): raise
  by 5 % of the capacity.
- Otherwise hold. Never below 10 % of the capacity. No latency reading means no raise.

This is AIMD, the rule TCP uses for the same problem: find the capacity that is actually
deliverable, and back off fast when the protected traffic suffers. mMTC is never throttled: its
load is a few kbit/s.

**What to throttle was measured, not assumed.** Each kind of demand alone, controller off:

| Load (120 s, same generator, 3 demands/s) | URLLC probe p95 | p99 | host load after |
|---|---|---|---|
| idle | 2.5 ms | 5.4 ms | 2.3 |
| URLLC demand only | 2.8 ms | 5.3 ms | 5.0 |
| eMBB demand only | 8.9 ms | 17.5 ms | 36.9 |

URLLC's own admitted flows do not hurt its latency; eMBB's do. They do not share a gNB, UPF or
data network with URLLC (ADR-011) — what they share is the CPU: carrying 300 Mbps through the
userspace gNB and UE is expensive, and on one laptop every slice runs on the same cores. So eMBB is
throttled by default. `CONTROL_THROTTLE="eMBB URLLC"` also caps URLLC's own admission under latency
pressure; ARP then decides which URLLC devices get the smaller capacity (a critical device still
pre-empts a standard one under a cut limit, pinned by a test). Measured, that variant only cost
URLLC service: see the results.

**This took three attempts, and the first two measured our own tool.** The orchestrator ran its
flows with iperf3. Leaked iperf3 processes, then iperf3 3.16's busy-waiting UDP sender (45–90 % of
a core per flow at any rate), loaded the host until URLLC's tail reflected the load generator.
Those runs blamed URLLC's own flows and pinned the controller at its floors. Flows now use
`flowgen.py`, a paced sender that sleeps (1–10 % of a core per process), and every run records host
load and leaked processes. The superseded runs are kept in
`docs/evidence/open5gs-orchestrator-dynamic/superseded-iperf3-load/`.

eMBB's policy capacity rises from 200 to **500 Mbps**, the aggregate target. The controller, not a
hand-calibrated `CAPACITY_OVERRIDE`, now finds what a given host can deliver below it.

### 3. Planning: how many slices a demand set needs

`/plan` answers, for a set of demands (POST) or the demand the orchestrator has actually seen over
the last few minutes (GET), which slices are needed and with how much capacity. Each class goes to
the cheapest adequate slice — the same rule admission uses — so a slice is needed only when some
demand requires a guarantee no looser slice gives. Required capacity is demand × 1.25, compared
with the policy capacity and with the current admission limit. Observed demand uses Little's law:
concurrent load = arrival rate × holding time, refused demands included.

### 4. IoT demands have a class

`sensor` (0.01 Mbps, ≤ 1000 ms) goes to mMTC. `traffic_gen.py` gives devices permitted only on
mMTC their own mix (`IOT_MIX`) when `SUBSCRIBER_SSTS` includes 3. Their real reports keep flowing
from `traffic_agent.py` either way.

## Results

See [`docs/evidence/open5gs-orchestrator-dynamic/`](../evidence/open5gs-orchestrator-dynamic/README.md).

| Experiment | Result |
|---|---|
| Priority: URLLC full of standard flows, then 5 critical demands | 5/5 critical admitted, each pre-empting exactly one standard flow; standard demands refused while full; all 20 surviving flows got their rate. Identical in two runs |
| Controller off vs on, same saturating demand, 180 s | eMBB flows that got their rate **68 % → 100 %**; URLLC p95 **20.5 → 12.5 ms**, p99 **55 → 34 ms**; URLLC flows 100 % in both; eMBB delivered 266 → 109 Mbps |
| Variant also throttling URLLC | URLLC p95 21.1 → 8.4 ms, but URLLC flows that got their rate fell to 82 % — a cost the diagnosis shows buys nothing |
| Planning | Phase 2b mix → 3 slices; delay-tolerant demand only → 1 slice; observed demand → eMBB 363 + URLLC 13.5 Mbps required |

The controller does not keep URLLC's p95 under 10 ms over a whole run on this laptop. In the last
minute, with eMBB at 50–96 Mbps, p95 still read 9–19 ms. It removes the part of the tail that eMBB
causes and stops the orchestrator from promising eMBB capacity the host cannot deliver.

## Three bugs in how flows were executed, found on the way

1. **Port clash.** The orchestrator handed executed flows ports from 5400. iperf3 runs its UDP test
   on the same port number as its control connection, and since ADR-015 the URLLC collector owns
   UDP 5400 in the same container (mMTC's owns 5401). A flow given 5400 failed at once, released the
   port, and the next flow drew it again: 7 of 26 URLLC flows failed in the first priority run.
   Flow ports now start at 5410 (`FLOW_PORT_FIRST`), pinned by a unit test.
2. **Leaked processes.** A Python timeout on `docker exec` kills the wrapper, not the process inside
   the container. One iperf3 client was found five minutes past its 20 s flow, at 90 % CPU. Both
   ends now run under `timeout -s KILL` inside their containers.
3. **A load generator that loaded the host.** iperf3 3.16's UDP sender busy-waits: 45–90 % of a
   core per flow at any rate. Flows now use `tools/slice-orchestrator/flowgen.py`, a paced sender
   that sleeps and costs 1–10 % of a core per process. It is shipped to the containers on stdin, so
   it needs no volume mount.

Bugs 2 and 3 had made the first two versions of the controller look like failures, and briefly made
URLLC's own flows look responsible for its latency.

## Consequences

- `slices.env`: eMBB capacity 500; URLLC ARP tiers; URLLC's slice-default vulnerability becomes
  pre-emptable (only the critical tier is protected); a `sensor` traffic class. The free5GC stack
  reads the same file: its orchestrator now models eMBB at 500 Mbps, and its URLLC subscribers
  become pre-emptable when next provisioned, which free5GC does not act on.
- Executed flows use `flowgen.py`, not iperf3. The KPI harness's eMBB TCP load (ADR-015) still uses
  iperf3: TCP does not pace by busy-waiting.
- New metrics: `slice_admission_limit_mbps`, `slice_decisions_by_priority_total`,
  `slice_preempted_total`, `slice_control_*`; `kpi_owd_recent_ms` from the collectors. Grafana has
  a row for them.
- `make o5gs-plan`, `make o5gs-dynamic`; 35 unit tests in `tools/slice-orchestrator/test_decide.py`
  (CI).
- **Provisional, to confirm with the guide:** the tier sizes (3 critical / 7 standard), the
  controller's thresholds (8 ms / 5 ms on p95), its step sizes, and the planning headroom.
- **Not done:** pre-empting running eMBB flows to protect URLLC latency (the controller only stops
  admitting); planning that also counts devices and sessions, not only bandwidth; the orchestrator
  on Kubernetes.
