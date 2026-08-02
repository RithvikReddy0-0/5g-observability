# Cloud-Native Observability Plane for free5GC

Foundation layer for a slice-aware observability plane built on a reproducible free5GC +
UERANSIM baseline. This repository owns **only our code and config**; the upstream projects
(free5GC, UERANSIM, gtp5g) are pinned by SHA in [`manifest.lock`](manifest.lock) and fetched
into a git-ignored `external/` by [`scripts/bootstrap.sh`](scripts/bootstrap.sh).

- **Source of truth:** [`SPEC.md`](SPEC.md) (SPEC-000) — architecture, ADR-001…008, acceptance criteria.
- **Current milestone brief:** [`docs/briefs/MILESTONE_0_BRIEF.md`](docs/briefs/MILESTONE_0_BRIEF.md).
- **M0 run report:** [`docs/runbooks/m0-report.md`](docs/runbooks/m0-report.md).

> **Baseline environment (ODE):** bare-metal **Ubuntu 24.04 LTS, x86_64, ≥16 GB RAM** (SPEC ADR-003).
> **WSL2 / VMs / cloud are non-baseline.** gtp5g build+load and M0 sign-off run only on a conforming ODE.

## Repository layout

```
SPEC.md                     source of truth (SPEC-000)
manifest.lock               SHA-pinned upstreams (ADR-002)
VERSIONS.lock               validated version matrix (env values captured on the ODE)
scripts/                    bootstrap.sh, verify_env.sh, validate_gtp5g.sh
deployments/compose/        Phase-1 Docker Compose baseline + per-NF config/ (ported)
ran/config/                 UERANSIM gNB/UE configs (ported)
tools/traffic/              slice-aware traffic simulator (Phase-2+ tooling, ported)
observability/              Phase-2+ plane — STUB (see SPEC §3)
k8s/                        Phase-2+ K8s — reference manifests (ported), not used in Phase 1
ci/                         CI/CD reference (ported); Phase-1 gate wired in M1
docs/                       ode.md, versions.md, adr/, runbooks/, interfaces/, procedures/, ...
external/                   fetched upstreams — GIT-IGNORED, never committed (ADR-002)
```

## Quick start (on a conforming ODE)

```bash
scripts/verify_env.sh          # must print: VERDICT: CONFORMING ODE
scripts/bootstrap.sh           # fetch free5GC / UERANSIM / gtp5g at pinned SHAs
scripts/bootstrap.sh --verify-only   # integrity gate (no network)
scripts/validate_gtp5g.sh      # build + load gtp5g against the running kernel (central M0 step)
```

## Provenance

Real configs (free5GC NF configs, UERANSIM gNB/UE, Compose, k8s manifests, CI, traffic sim)
were **ported (copied) from the prior `5g-devops-framework` project (Review 2)**, which
remains intact as reference. See the slice-consistency audit in the M0 report.
