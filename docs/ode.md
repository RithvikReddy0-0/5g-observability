# Official Development Environment (ODE)

**Deliverable D1 (Milestone 0).** Mirrors SPEC-000 ADR-003. The ODE is the *only*
baseline against which milestones are validated. VMs, cloud instances, and **WSL2
are explicitly excluded** from the baseline (they may later be documented as
*secondary* targets with their own caveats, but never redefine the baseline).

## ODE requirements

| Attribute         | Requirement |
|-------------------|-------------|
| OS                | Ubuntu **24.04 LTS** |
| Architecture      | **x86_64** |
| Installation      | **Bare-metal** (not VM, not WSL2, not container-in-container) |
| CPU               | Intel or AMD, **≥ 8 logical threads** |
| RAM               | **≥ 16 GB** (32 GB recommended) |
| Storage           | **≥ 100 GB free SSD** |
| Privileges        | **Full sudo** |
| Container runtime | **Docker Engine** + **Docker Compose v2** (`docker compose` plugin) |
| Kernel ↔ gtp5g    | Pinned as a **pair** and proven together in M0 (see `docs/versions.md`) |

## Rules

1. The ODE **defines the baseline**; a milestone is not "done" until it passes on the ODE.
2. VMs / cloud / CI runners may later be documented as **secondary targets**, but never
   redefine the baseline.
3. The ODE is a *class* of machine, so the exact `VERSIONS.lock` (kernel point release,
   gtp5g SHA, Docker versions) is captured from the specific box used in M0 and must
   reproduce on any conforming ODE.

## How to check a machine

```bash
scripts/verify_env.sh
```

Exit 0 + `VERDICT: CONFORMING ODE` means the machine is baseline-eligible. Any FAIL, or
virtualization ≠ none, prints a `NON-BASELINE ENVIRONMENT` banner and exits non-zero.

> The machine used to author the repo skeleton (this commit) is **WSL2 with ~7.6 GB RAM**,
> which is **non-baseline** by rules above. All build/load validation (gtp5g) and the M0
> sign-off must be performed on a conforming ODE. See `docs/runbooks/m0-report.md`.
