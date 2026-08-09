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

> **Baseline environment (ODE):** bare-metal **Ubuntu 24.04 LTS, x86_64, ≥16 GB RAM** (ADR-003).
> **WSL2 / VMs / cloud are non-baseline.** The user plane (UPF/gtp5g) runs only on a conforming ODE.

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
| **User plane (PDU session, N3/N6)** | 🔒 **ODE-only** — needs the gtp5g kernel module |
| Structured JSON logs | ⚠️ not configurable upstream — documented gap ([`docs/logging.md`](docs/logging.md)) |
| Phase 1 freeze / baseline tag | ❌ not possible without the user plane |

Run the checks yourself: `tests/acceptance.sh` (currently PASS=12, FAIL=0, SKIP-ODE=7, GAP=1).
It refuses to report Phase 1 complete on a non-baseline host.

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
