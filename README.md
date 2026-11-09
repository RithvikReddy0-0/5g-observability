# Cloud-Native Observability Plane for free5GC

A slice-aware observability plane built on a reproducible free5GC + UERANSIM baseline. This
repository owns **only our code and config**; the upstream projects (free5GC, UERANSIM,
gtp5g) are pinned by SHA in [`manifest.lock`](manifest.lock) and fetched into a git-ignored
`external/` by [`scripts/bootstrap.sh`](scripts/bootstrap.sh).

- **Source of truth:** [`SPEC.md`](SPEC.md) (SPEC-000) — architecture, ADR-001…008, acceptance criteria.
- **Phase re-sequencing:** [`docs/adr/ADR-009-phase-resequencing.md`](docs/adr/ADR-009-phase-resequencing.md).
- **M0 run report:** [`docs/runbooks/m0-report.md`](docs/runbooks/m0-report.md).
- **Runbook:** [`docs/runbooks/clean-install.md`](docs/runbooks/clean-install.md).
- **Results & findings:** [`docs/RESULTS.md`](docs/RESULTS.md) — all measured outcomes in one place.
- **KPI gate:** [`docs/kpi-gate.md`](docs/kpi-gate.md) — the pass/fail contract a deployment must meet.
- **Review deck:** [`docs/presentation/`](docs/presentation/) — Phase 2 Review 1 slides (LaTeX Beamer).
- **Open5GS stack:** [`docs/open5gs.md`](docs/open5gs.md) — a second core ([ADR-010](docs/adr/ADR-010-open5gs-core.md)) with a **working user plane on this laptop**. `make o5gs-build && make o5gs-up`.
- **Phase 2b (100 UEs, three slices):** [work division](docs/briefs/phase2b-work-division.md) · [Track 1 handover](docs/briefs/phase2b-track1-handover.md) · [KPI targets review](docs/briefs/phase2b-kpi-targets-review.md) · [KPI scorecard](docs/kpi-scorecard.md) · [KPI results + Excel](docs/evidence/open5gs-kpi/README.md) · [results deck](docs/presentation/phase2b-kpi-results.pdf) · ADR-014, ADR-015.
- **Open5GS completion report:** [`docs/reports/2026-09-14-open5gs-completion-report.md`](docs/reports/2026-09-14-open5gs-completion-report.md) — what was built, every problem found and how it was fixed, final measured state, what is not done.

> **Baseline environment (ODE):** bare-metal **Ubuntu 24.04 LTS, x86_64, ≥16 GB RAM** (ADR-003).
> **WSL2 / VMs / cloud are non-baseline.** free5GC's user plane (UPF/gtp5g) runs only on a conforming ODE;
> the Open5GS stack's userspace UPF runs on WSL2 too.

## Current state

| Area | Status |
|---|---|
| Pinned upstreams + bootstrap | ✅ fully validated, incl. drift detection and `--clean` determinism |
| Control plane (8 NFs + MongoDB + WebUI) | ✅ running, all NRF-registered |
| Subscribers | ✅ 20 provisioned, split across two slices |
| UE registration | ✅ 20 UEs REGISTERED (10 per slice) |
| Slices | ✅ two modeled S-NSSAIs, NSSF selects each independently |
| Observability | ✅ Prometheus + Grafana on free5GC-native metrics |
| Slice-labeled metrics | ✅ derived exporter (closes risk R-02) |
| **User plane (PDU session, N3/N6)** — free5GC | 🔒 **ODE-only** — needs the gtp5g kernel module |
| **User plane — Open5GS stack** | ✅ 100/100 PDU sessions in three slices — eMBB 20, URLLC 10, mMTC 70 — a gNB + UPF per slice, AMBR enforced ([details](docs/open5gs.md), [ADR-014](docs/adr/ADR-014-mmtc-slice-and-100-ues.md)) |
| 100 UEs attach | ✅ 35 s ten at a time, 7 s all at once; survives VM clock steps (UERANSIM monotonic-clock patch); this laptop tops out near 300 UEs ([scale](docs/evidence/open5gs-scale/README.md)) |
| Slice isolation — Open5GS | ✅ URLLC 10/10 reachable with eMBB saturated, 3 of 3 runs ([ADR-011](docs/adr/ADR-011-dedicated-user-plane-per-slice.md)); CPU not isolated |
| Deliverable capacity — Open5GS | ✅ eMBB 200 Mbps, URLLC 20 Mbps of real UDP ([ADR-012](docs/adr/ADR-012-ueransim-udp-buffers.md)) |
| Slice orchestrator on real traffic | ✅ 164/165 admitted flows delivered their rate; refuses on measured load, no spill-over |
| Dynamic allocation (Phase 2b) | ✅ ARP priority inside URLLC: critical devices pre-empt standard ones, 5/5. Feedback controller moves eMBB's admission limit: eMBB flows getting their rate 68 % → 100 %, URLLC p95 20.5 → 12.5 ms. Slice planning from demand ([results](docs/evidence/open5gs-orchestrator-dynamic/README.md), [ADR-016](docs/adr/ADR-016-dynamic-slice-allocation.md)) |
| Algorithms compared (state of the art) | ✅ For every orchestration decision, the strongest methods from the literature were implemented and raced under identical conditions. A **PI controller** kept URLLC within budget 100 % of the time (AIMD: 79.5 %), with as much eMBB as no control. Trunk reservation, Kaufman–Roberts sizing and predictive switching won their comparisons. All four are now the defaults ([comparison](docs/algorithm-comparison.md), [ADR-018](docs/adr/ADR-018-algorithm-comparison.md)) |
| Network orchestrator (slice lifecycle) | ✅ mMTC deactivated in 14.5 s (core sessions 70 → 0, other slices untouched) and activated in 12.2 s with every step verified. A missed deadline is rolled back automatically. Closed loop switches mMTC on IoT demand and off when idle. CPU quota per slice ([results](docs/evidence/open5gs-network-orchestrator/README.md), [ADR-017](docs/adr/ADR-017-network-orchestrator.md)) |
| Packet scheduler (ADR-019) | ✅ Every radio packet is classified from its headers (signalling or user data, which slice, which ARP tier) and the slices share an emulated cell by guaranteed shares and priorities. Without it, an overloaded shared cell made 93–99 of 100 UEs lose their radio link; with it, none, and URLLC p99 4–5 ms where the cell is the bottleneck. At nominal load, against a plain queue at the same cell: 0 % URLLC loss against 1.3–2.9 % (one device per run losing its radio link); against no cell at all it costs eMBB throughput ([same-day KPI comparison](docs/evidence/open5gs-kpi/README.md), [evidence](docs/evidence/open5gs-scheduler/README.md), [ADR-019](docs/adr/ADR-019-packet-scheduler.md)) |
| Scheduler algorithms (ADR-021) | ✅ Strict priority, DRR and HFSC run on the same classifier as HTB. In 36 interleaved KPI runs none differed from HTB on URLLC (Friedman p = 0.65). Under 1.5× eMBB overload, strict priority (88–180) and FIFO (97–98) caused mass radio-link failures; HTB, DRR and HFSC none, DRR adding ~2 ms of URLLC median. HTB stays the default. The earlier "longer URLLC tail with the scheduler" did not reproduce: the tail follows host CPU speed ([evidence](docs/evidence/open5gs-scheduler-algorithms/README.md), [URLLC tail](docs/evidence/open5gs-urllc-tail/README.md), [ADR-021](docs/adr/ADR-021-scheduler-algorithms.md)) |
| URLLC reliability (TS 22.261 sense) | ⚠️ 660 s, 331 237 packets, **0 lost** (loss < 1·10⁻⁵ at 95 %), but only **99.32 %** arrived within the 10 ms budget: 99.999 % is not met on this laptop ([evidence](docs/evidence/open5gs-urllc-reliability/README.md)) |
| Registration benchmark | ✅ bursts of 1–200 UEs, 19 bursts, 0 failures; 100 UEs: 0.99 s per UE (median of runs). Mukute et al. (IEEE Access 2024) report ~2.9 s for registration + de-registration, which UERANSIM cannot time separately here ([evidence](docs/evidence/open5gs-registration-benchmark/README.md)) |
| MAC scheduling (simulation, ADR-022) | ✅ slot-level simulation of one cell with RR, Max-C/I, PF, M-LWDF, EXP/PF inside slices and static, NVS, priority, RadioSaber between them; 440 runs, 10 seeds, 95 % CIs. Lending idle resource blocks is worth +11.5 Mbps eMBB (+15 %); channel-aware inter-slice choice cuts URLLC deadline misses by 14–22 pp. **Simulation only** ([evidence](docs/evidence/mac-sim/README.md), [ADR-022](docs/adr/ADR-022-mac-scheduler-simulation.md)) |
| Standards and statistics (ADR-023) | ✅ a GSMA NG.116 v10.0 NEST per slice, checked against the deployment in CI; a 3GPP/IETF mapping with gaps stated; exact tests and CIs for every comparison, which show that 2-vs-2 runs cannot reach p < 0.05 ([mapping](docs/standards-mapping.md), [statistics](docs/evidence/open5gs-kpi/statistics.md), [ADR-023](docs/adr/ADR-023-standards-and-statistics.md)) |
| Per-slice logs, traces, fault diagnosis (ADR-024) | ✅ deployed: Loki + Alloy label every container's log with slice, component, level and event (19 kinds); the orchestrators, scheduler and KPI runner write decision traces; a diagnosis service correlates metrics, logs and traces and ranks causes. On 49 recorded incidents precision 1.00, recall 1.00, top-1 0.93 (**in-sample**: the rules were written knowing them). ~220 MiB ([runbook](docs/observability.md), [deployment](docs/evidence/open5gs-observability/README.md), [ADR-024](docs/adr/ADR-024-observability-logs-correlation-diagnosis.md)) |
| Multi-session UEs (ADR-020) | ✅ eMBB and URLLC devices hold a session on both slices (130 sessions); a demand placed on the other slice runs on the requester's own session; switching a slice off releases only the other devices' session on it ([evidence](docs/evidence/open5gs-multi-session/README.md), [ADR-020](docs/adr/ADR-020-multi-session-ues.md)) |
| KPI gate — Open5GS | ✅ 0 failed on the live stack (28 KPIs: 27 passed, 1 advisory, the URLLC tail; each slice at its own size; the cell drops no signalling). In CI every enforcing gate is seen to fail on its fault and every advisory to warn |
| Deploy with automatic rollback | ✅ `scripts/open5gs/deploy.sh` — static reject, gate-fail rollback, deploy all demonstrated; the three-slice topology itself was deployed through it |
| Per-slice KPIs (Phase 2b) | ✅ measured and scored against the team's targets: same day, scheduler off: URLLC 1.00 · eMBB 0.57–0.65 · mMTC 1.00 (overall 0.86–0.88); sliced at a 250 Mbps cell: 0.95–1.00 · 0.30–0.32 · 1.00 (0.75–0.77), eMBB capped by the cell; at 600 Mbps, the default since 2026-11-09: 0.80–0.85, confirmed by five interleaved rounds against 250 (eMBB doubled, p = 0.029; URLLC no different; [evidence](docs/evidence/open5gs-cell-capacity/README.md)); three one-parameter sweeps; Excel report ([results](docs/evidence/open5gs-kpi/README.md), [ADR-015](docs/adr/ADR-015-traffic-profiles-and-kpi-scorecard.md)) |
| Kubernetes — Open5GS | ✅ minikube, manifests generated from compose; three slices and 100 UEs, same KPI gate passes ([ADR-013](docs/adr/ADR-013-kubernetes-open5gs.md)) |
| Structured JSON logs | ⚠️ not configurable upstream — documented gap ([`docs/logging.md`](docs/logging.md)); the Open5GS stack's text logs are parsed into labelled events instead (ADR-024) |
| Phase 1 freeze (free5GC) | ❌ not possible without the user plane (ODE) |
| Open5GS baseline tag | ✅ `open5gs-baseline-20261108`, before the scheduler and multi-session changes |

Run the checks yourself: `tests/acceptance.sh` (currently PASS=12, FAIL=0, SKIP-ODE=7, GAP=1).
It refuses to report Phase 1 complete on a non-baseline host.
Open5GS stack: `make o5gs-test` (currently PASS=24, FAIL=0, SKIP-ODE=1, GAP=1, OWNER=0).

## Repository layout

```
SPEC.md                     source of truth (SPEC-000)
manifest.lock               SHA-pinned upstreams (ADR-002)
VERSIONS.lock               version matrix; environment values captured on the ODE
scripts/                    bootstrap, env verify, gtp5g validate, provisioning,
                            UE launch, slice exporter, evidence collection
deployments/compose/        Docker Compose baseline + per-NF config/
ran/config/                 UERANSIM gNB/UE configs
observability/              Prometheus + Grafana + slice-exporter
tools/traffic/              slice-aware traffic simulator (Phase-2+ tooling, ported)
k8s/                        Phase-2 reference manifests (ported), not used yet
tests/acceptance.sh         SPEC §5 acceptance criteria as executable checks
docs/                       ode.md, versions.md, logging.md, adr/, runbooks/, interfaces/
diagrams/                   Mermaid sequence diagrams (registration, PDU session)
.github/workflows/          CI integrity gate (manifest SHAs, drift, config validation)
external/                   fetched upstreams — GIT-IGNORED (ADR-002)
```

## Quick start

Easiest way — a Makefile wraps everything:

```bash
make up      # start the whole stack
make ues     # connect 20 devices across both slices
make status  # see what is running
make test    # run the acceptance checks
make down    # stop everything (keeps data)
make help    # list every command
```

Manual equivalents below.

Full detail in [the runbook](docs/runbooks/clean-install.md).

```bash
bash scripts/verify_env.sh                 # ODE conformance
bash scripts/bootstrap.sh                  # fetch upstreams at pinned SHAs
bash scripts/validate_gtp5g.sh             # ODE-ONLY: build + load gtp5g
```

Bring up the core, provision two slices, register 20 UEs:

```bash
cd deployments/compose && docker compose up -d db free5gc-nrf && sleep 10
docker compose up -d free5gc-amf free5gc-ausf free5gc-udm free5gc-udr \
                     free5gc-pcf free5gc-nssf free5gc-webui
docker compose up -d --no-deps free5gc-smf ueransim && cd ../..

bash scripts/provision_subscribers.sh --delete-all
COUNT=10 START=1  SD=010203 bash scripts/provision_subscribers.sh
COUNT=10 START=11 SD=112233 bash scripts/provision_subscribers.sh
COUNT=10 COUNT_B=10 TEMPO=600 bash scripts/start_ues.sh
```

Observability:

```bash
cd observability && docker compose up -d && cd ..
bash scripts/start_slice_exporter.sh
```

| Service | URL |
|---|---|
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (anonymous viewer — dev only) |
| free5GC WebUI | http://localhost:5000 |
| Slice exporter | http://localhost:9105/metrics |

## The finding that shapes this project

**free5GC's native metrics carry no slice identity.** Verified across all 629 exposed series
and 15 metric names — the only labels are `gmm_state, le, method, name, nf_type, path, state,
status, status_code, target_service_name, to_state`. None identify a slice.

That is risk **R-02** from the SPEC, confirmed empirically, and it blocks the ADR-006
slice-labeling contract the Slice Correlation Engine depends on.
[`observability/slice-exporter`](observability/slice-exporter/) closes the gap at ADR-004
rung 2 by deriving slice identity from AMF logs and MongoDB — no upstream patch. Its limits
are documented honestly in [`observability/README.md`](observability/README.md), including
that the AMF's own UE gauges *leak under churn* on v4.2.0: they match ground truth right
after a clean bring-up, then drift permanently once procedures abort or the AMF restarts.

## Provenance

Real configs (free5GC NF configs, UERANSIM gNB/UE, Compose, k8s manifests, CI, traffic sim)
were **ported from the prior `5g-devops-framework` project**, which remains intact as
reference. Note that project's "slices" were application-layer labels on HTTP requests, not
5G network slices — see the slice-consistency audit in the M0 report.
