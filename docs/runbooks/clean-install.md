# From-clean-Ubuntu runbook (STUB)

**Deliverable D9 (Milestone 0).** This is a skeleton to be completed and *proven* on a
conforming ODE (a stranger must be able to follow it to the frozen baseline). It is a stub
in this commit because the authoring machine is non-baseline (WSL2); the empirical steps
are validated on the lab ODE.

## 0. Prerequisites
- A machine matching the ODE (`docs/ode.md`): bare-metal Ubuntu 24.04 LTS, x86_64,
  ≥ 8 threads, ≥ 16 GB RAM, ≥ 100 GB free SSD, full sudo.

## 1. Verify the environment
```bash
scripts/verify_env.sh      # must print: VERDICT: CONFORMING ODE
```

## 2. Install base tooling  (TO BE DETAILED ON ODE)
- Docker Engine + Docker Compose v2 plugin
- git, jq, build-essential, make, gcc
- `linux-headers-$(uname -r)`

## 3. Materialize pinned upstreams
```bash
scripts/bootstrap.sh              # clones free5GC / UERANSIM / gtp5g at pinned SHAs
scripts/bootstrap.sh --verify-only  # integrity gate (no network)
```

## 4. Prove the gtp5g ↔ kernel pair  (central M0 objective)
```bash
scripts/validate_gtp5g.sh   # build + load gtp5g against the running kernel
```
On success it records the kernel + gtp5g module version into `VERSIONS.lock`.
On failure it HARD-STOPS (brief §8.6) — escalate, do not auto-switch versions.

## 5. Freeze the matrix
- Fill remaining `TO_BE_FILLED_ON_LAB_ODE` fields in `VERSIONS.lock`.
- Record the full run in `docs/runbooks/m0-report.md`.

## 6. (Later milestones) bring up the core — NOT part of M0
- M2 brings up the control plane with `deployments/compose/`. Deferred here.
