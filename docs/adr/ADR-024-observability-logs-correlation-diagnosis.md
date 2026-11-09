# ADR-024 — Per-slice logs, decision traces, a correlation engine and rule-based fault diagnosis

**Status:** Accepted, 2026-11-09. Built and verified offline, then deployed on the live stack the same
day ([evidence](../evidence/open5gs-observability/README.md)): ~220 MiB in all, KPI gate and
acceptance still pass. Deploying found three faults no test had caught (the scheduler's spans dropped
by `fs.protected_regular`, LogQL quoting Alloy refused, a backfill Loki rejected); all three are fixed
and now tested, the Alloy one in CI against the pinned image.
**Builds on:** ADR-005 / [`docs/logging.md`](../logging.md) (the cores write text logs, never JSON),
ADR-015 (KPI collectors), ADR-016/018 (slice orchestrator and its controller), ADR-017 (network
orchestrator), ADR-019 (packet scheduler).
**Answers:** group 3 of the work division — per-slice logs and traces, a correlation engine, fault
diagnosis.

## Context

Metrics already answered *whether* a slice meets its KPIs (KPI gate, scorecard). When one does not,
finding *why* was manual every time: the FIFO cell's mass radio-link failures, URLLC's tail under eMBB
load, the controller throttling URLLC for nothing, a gNB pointed at the wrong AMF port, a stopped
orchestrator failing the gate, 240 UE logs for 100 UEs. Each was found by someone reading
`docker logs`, `docker stats`, Prometheus and the orchestrators' own logs side by side, and each
diagnosis is now written down in an evidence README. Three things were missing:

1. **Logs per slice.** Open5GS and UERANSIM write coloured text; slice identity sits in a few message
   shapes (an S-NSSAI at the AMF, a DNN at the SMF and UPF, the container of a per-slice gNB or UPF).
2. **Traces.** Open5GS v2.8.0 has **no tracing**: the pinned source (157f611a) contains no
   OpenTelemetry, OTLP, Jaeger or Zipkin code; its only match for "tracing" is the `-t` flag, which
   raises the log level (`src/main.c`). UERANSIM v3.3.0 has none either. The decisions that shape a
   slice's service are made by *our* components — admission, pre-emption, controller moves, lifecycle
   steps, scheduler re-applies — and were visible only as counters or free text.
3. **Correlation and diagnosis.** Nothing lined those sources up in time around a breach, and nothing
   mapped a set of symptoms to the causes this project has already met.

Constraints: a laptop with ~7.6 GiB RAM shared with 100 UEs and the 5G core; anything new must be
small, optional (the KPI gate must not depend on it) and pinned; Python standard library only, as
everywhere else in `tools/`.

## Decisions

### 1. One set of parsing rules, used three ways

`tools/observability/logparse.py` holds `RULES`: (event, components, pattern). Every message text
came from a real line captured on the stack or from the pinned upstream source; the file and line
are noted next to each rule. The same rules:

- parse lines in Python (`parse_line`) — for tests and for the correlation engine, which reads
  `docker logs --timestamps` and the UEs' own `/tmp/ue-*.log` (a single read-only `grep`) when Loki is
  not running;
- **generate** the Grafana Alloy pipeline (`logparse.py --alloy` →
  `deployments/open5gs/observability/alloy/config.alloy`). CI regenerates it and fails on a
  difference, exactly as for the Kubernetes manifests (ADR-013), so the shipped pipeline can never
  drift from the tested rules. Patterns are written in the common subset of Python `re` and Go RE2
  (named groups only, no look-around or back-references), and a test enforces that.

Events: registration request/complete/reject, authentication failure, PDU session request /
established / released, UPF session add/remove, IP pool exhausted, PFCP associated / de-associated /
heartbeat lost / restoration / unanswered, NF registered / de-registered / terminating, NG Setup,
N2 connect failed, UE signal lost, radio-link failure, "PDU session not found" at the gNB, barred
access, cell selection failure, UE supervisor restart, scheduler re-apply, controller and lifecycle
actions.

Slice identity, most trusted first: S-NSSAI in the line → DNN → the per-slice container → a UE pool
or UPF N4 address → the IMSI's *home* slice (since ADR-020 a device also holds a session on its other
slice, so IMSI-only attribution is marked `slice_from: imsi`). The mapping is read from
`deployments/slices.env` (`O5GS_UES`, S-NSSAIs) and the UE configs (DNNs), not hard-coded.

### 2. Loki + Grafana Alloy, optional, in a Compose project of its own

`deployments/open5gs/docker-compose.logs.yaml` (project `o5gs-logs`) on the existing `o5gs_net`:
Loki 3.7.8 (single binary, filesystem, 72 h retention) and Alloy v1.20.1, both pinned by the
multi-arch index digest Docker Hub reports for the tag. **Alloy, not Promtail:** Promtail reached end
of life on 2026-03-02 (Grafana Loki documentation, "Promtail agent"), and its pipeline stages live
on in Alloy's `loki.process`. Hard memory limits 384 MiB (Loki) + 192 MiB (Alloy), a Go soft limit
below each, half a CPU each. Loki labels are low-cardinality only — job, container, component, slice,
level, event; IMSIs and addresses stay in the line (a test fails if an IMSI becomes a label).

Grafana gets a Loki datasource and a dashboard row (generated by `gen_dashboard.py`): log events per
slice per minute, fault events per slice, a logs panel per slice (warnings, errors and events), and the
diagnosis scores. Without Loki only those panels show a datasource error.

### 3. Decision traces from our own components (`tools/observability/trace.py`)

A dependency-free tracer: spans with parent/child nesting per thread, instant events, written as JSON
lines to `OBS_TRACE_FILE`, and optionally exported over OTLP/HTTP with JSON encoding to
`OTEL_EXPORTER_OTLP_ENDPOINT` (`POST /v1/traces`, hex trace/span ids, nanosecond times as decimal
strings, enums as integers — the OTLP specification's JSON rules). Off unless one of the two is set,
so tests and CLI runs write nothing; a tracing failure never reaches the caller. No collector is
deployed: the JSON-lines file is what the correlation engine reads.

Instrumented here: the slice orchestrator (`admission.decide` with verdict, slice and reason;
`admission.preempt` as a child; `controller.cut|raise` with from/to/why/policy; `slice.active`) and
the network orchestrator (`netorch.activate|deactivate|scale` with one `netorch.step` child per step and
its duration; its operation log also gains the epoch start `t0`). The start scripts set
`OBS_TRACE_FILE=/tmp/o5gs_traces.jsonl`. The packet scheduler (`scheduler.apply` spans with mode, devices, outcome,
duration) and the KPI runner (`kpi.instance`, `kpi.embb_load`, `kpi.attach`) were being changed on the
live stack at the time, so their instrumentation was first kept as patches and applied once the
scheduler experiments (ADR-021) had finished. Scheduler re-applies are visible without the patch anyway: its
`applied mode=…` log lines are a parsing rule.

### 4. Correlation engine (`tools/observability/correlate.py`)

Given a window, or a trigger (a signal such as `urllc_owd_p95`, or a failed KPI gate verdict from
`kpi_gate.py --json`), it pulls a fixed catalogue of 27 per-slice signals with `query_range`, the
parsed logs, and the trace spans, all over the window plus a baseline (by default as long as the
window, just before it). For each series:

- **level and worst-value anomaly**: the robust ("modified") z-score `(x − median) / (1.4826·MAD)`
  of the window against the baseline, cut-off |z| ≥ 3.5 as recommended by Iglewicz and Hoaglin
  (NIST/SEMATECH e-Handbook of Statistical Methods, §1.3.5.17). Each signal states which direction is
  bad and a floor on the spread, so a flat baseline (a session count) does not make every change
  infinite;
- **when** it changed: the CUSUM change point, argmax_k |Σ_{i≤k}(x_i − x̄)| (E. S. Page, "Continuous
  inspection schemes", *Biometrika* 41(1/2):100–115, 1954, doi:10.1093/biomet/41.1-2.100);
- **lead/lag** against the trigger: the Pearson correlation of the trigger with the series shifted by
  −60…+60 s; a positive lag means the series moved first.

Log events are counted per (event, slice) against the baseline's rate; a fault event absent from the
baseline is always reported. Output: ranked anomalies (z, weighted up when correlated with and leading
the trigger) and a merged timeline of change points, first log occurrences and trace decisions.

### 5. Fault diagnosis (`tools/observability/diagnose.py`)

Rules, not a model: nine rules, one per failure this project has met, each reading only named facts,
scoring 0–1 and citing every metric, log event, trace or snapshot it used, with suggested actions
taken from the ADR or runbook that fixed it:

| Cause | Main evidence | From |
|---|---|---|
| `cpu-saturation` | host load ≥ 75 % of CPUs; UPF at ≥ 0.85 core (`process_cpu_seconds_total`, one forwarding thread); a container at its quota; slowed UE probe rounds — **and** a latency breach or short flows. Without a CPU measurement: URLLC latency over budget while its own queue lost nothing and another slice carries ≥ 100 Mbps (score 0.55, marked *inferred*) | ADR-016 §4, ADR-019 finding 5 |
| `cell-congestion` | scheduler class drops/backlog, FIFO mode, offered load above the cell, loss on slices that did not cause it; signalling drops are critical | ADR-019 |
| `rlf-no-data-path` | "Radio link failure", gNB "PDU session not found", supervisor restarts, UEs without data path, reachability falling while the core still counts sessions; periodic failures point at wall-clock steps | ADR-014, open5gs-clock |
| `pfcp-association-loss` | `pfcp_peers_active` below 3; heartbeat lost / restoration / unanswered requests; UPF sessions dropping | operating notes |
| `gnb-n2-down` | `gnb` below the slices; SCTP refused / NG Setup failed; gate failures | open5gs-deploy |
| `registration-failure` | `reginitfail`, auth failures, rejects, NFs de-registered and not back, UEs leaving the RAN | KPI gate advisory |
| `orchestrator-throttling` | controller cuts (traces), the admission limit far below capacity, capacity refusals; flags the controller cutting the slice it protects | ADR-016, controller-throttling-urllc-too |
| `target-down` | `up == 0`; optional host processes (orchestrators, scheduler, this service) are advisory with their start script, core targets critical | kpi-gates.json, 2026-11-08 |
| `observability-artifact` | a count above ground truth (240 UE logs for 100 UEs; core sessions on a dormant slice) | ue-entrypoint.sh, ADR-017 finding 4 |

A post-pass links consequences to causes (radio-link failures explained by a FIFO cell or a starved
host; registration failures by a gNB without N2 or a lost PFCP association) and ranks root causes
first. `diagnose.py --serve` (port 9115, `scripts/open5gs/start_diagnosis.sh`) runs the correlation
and diagnosis every 120 s over the last 600 s and exposes `/diagnosis`, `/correlation` and
`fault_diagnosis_*` metrics. Prometheus scrapes it as job `fault-diagnosis`, excluded from the
gate's `no-scrape-failures` like the orchestrators, with an advisory of its own.

## Verification

All offline, no container touched:

- `tools/observability/test_observability.py` — 48 tests: every fixture line of real captured logs
  parses (AMF with its ANSI colours, SMF, both UPFs, NRF, gNB, the UE supervisor, a UE's own log, the
  scheduler, and the gNB that could not reach the AMF quoted in `open5gs-deploy`); slice attribution
  by S-NSSAI, DNN (a URLLC device's second session on eMBB is eMBB), container, address and IMSI;
  RE2 compatibility; the committed Alloy pipeline equals the generated one; tracer nesting, errors,
  failure isolation and OTLP/JSON received by a stub collector; the slice orchestrator's
  decisions/pre-emptions/controller cuts and the network orchestrator's steps become spans; robust z,
  CUSUM, a known 30 s lead recovered; recorded controller series; each diagnosis rule positive and
  negative; the service's metrics; an unreachable Prometheus is asked once and makes the verdict
  *unverified* (exit 2, `fault_diagnosis_verified 0`), never "healthy".
- The existing suites still pass unchanged: `test_decide.py` 53, `test_netorch.py` 19,
  `test_scheduler.py` 24 (also with the scheduler patch applied); `kpi_gate.py --self-test` on both
  definition files; `check_metric_names.py`; `check_open5gs_slices.py`; the Kubernetes render is
  unchanged (the new job is a host job, skipped like the orchestrators).
- Both patches `git apply --check` cleanly on the base commit and were run: the scheduler emits an
  `ok` span and an `error` span with tc's message; the KPI runner emits `kpi.instance` with its
  `kpi.embb_load` child.
- One live read-only smoke test: three `query_range` GETs to Prometheus over 20 minutes. The engine
  found the eMBB class dropping up to 5.6 k packets/s from 18:50Z and a FIFO cell class (another
  experiment was running), and diagnosed cell congestion. That run is not labelled and not scored.

## Results — diagnosis on the recorded incidents

`tools/observability/incidents.py` (`diagnose.py --validate`, run in CI) turns 68 recorded runs into
cases, each labelled with the cause its evidence README or ADR documents:

| Source | Cases |
|---|---|
| packet scheduler runs A–D (`open5gs-scheduler`) | 22 (1 unlabelled: an `off` run whose URLLC p99 is over budget, cause not stated) |
| controller on/off and URLLC-throttling runs (`open5gs-orchestrator-dynamic`), 10 control-law runs (`open5gs-algorithm-comparison`) — through the correlation engine on their recorded series and control logs | 14 |
| KPI instances (`open5gs-kpi`) | 24 (18 unlabelled: URLLC p99 over 10 ms, "cause open" in the repository) |
| incidents from logs: PFCP loss of the URLLC/mMTC UPFs (live SMF log), gNB port typo (deploy log + gate), a healthy re-attach | 3 |
| incidents documented only as numbers (reconstructed): network orchestrator down after a WSL restart, 240/100 UE logs, clock-step RLF bursts, the crashed FIFO-600 KPI run, stale core sessions on a dormant slice | 5 |

On the **49 labelled cases**: precision **1.000** (60 of 60 reported causes expected or acceptable),
recall **1.000** (50 of 50 expected causes found), top-1 **0.929** (39 of 42 faulty cases: in
`algo-aimd-r1/r2` and `algo-mpc-r2` CPU saturation, true but not the headline, outranks the
controller's throttling), and **7 of 7** healthy cases report nothing critical or warning.

**Read these numbers for what they are.** The rules were written knowing these incidents, so this is
an in-sample consistency check, not a held-out accuracy. One rule was changed after a case failed:
a single barred access attempt on a healthy re-attach was reported as a registration failure
(precision 0.984 before the fix; UE-side events now need three occurrences, like authentication
failures). Five cases are reconstructed from documented numbers. "Acceptable" secondary causes are a
judgment, stated per case. The scheduler runs carry no CPU measurement, so CPU saturation there is the
*inferred* form (0.55). On the 18 unlabelled KPI runs the diagnosis says CPU saturation (eMBB's UPF at
70–97 % of a core, URLLC p99 10–20 ms); that is consistent with ADR-016's diagnosis but the repository
leaves those causes open, so they are not counted (nor is the one unlabelled scheduler run).

## Consequences

- A KPI breach or a failed gate now comes with a ranked, cited explanation, live (`:9115/diagnosis`)
  or after the fact (`correlate.py --gate verdict.json`, `--series` on recorded evidence).
- Parsing text logs stays brittle across upstream versions (docs/logging.md). The rules are tested on
  real lines and every rule names its source line, so a version bump fails CI where a message changed.
- What remains:
  - **Not yet run on the stack:** Loki/Alloy (images not pulled here; digests checked against Docker
    Hub's API, not by a pull), the Alloy pipeline (`alloy fmt` has not been run on it), the Grafana
    panels, the service under live load, and the memory footprint, which is only bounded by the
    limits above (expected well below them; to be measured with `docker stats`).
  - The UEs' own log files (radio-link failures) are inside the UE container's `/tmp`, which Alloy
    cannot read; Loki sees their consequences (gNB "signal lost", supervisor restarts). Shipping them
    would need the UE entrypoint to log to stdout or a shared volume — a UE container change.
  - The packet scheduler and KPI runner patches await the coordinator (files were being edited live).
  - No OpenTelemetry collector or trace UI is deployed; JSON lines are enough for the correlation.
  - The rules need cases they were not written from; every new incident should be added to
    `incidents.py` with its label before the rules are touched.
