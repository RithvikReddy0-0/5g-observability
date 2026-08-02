# Version matrix (human-readable)

**Deliverable D8 (Milestone 0).** Human-readable companion to the machine-readable
`VERSIONS.lock` and `manifest.lock`. Per ADR-003 the matrix is validated *together* on
the ODE, not component-by-component. The kernel ↔ gtp5g pair is the critical coupling.

## Pinned upstream sources (`manifest.lock`)

| Component | Tag (ref_hint) | Pinned commit (authoritative) | Submodules |
|-----------|----------------|-------------------------------|------------|
| free5GC   | `v4.2.3`  | `3b34a08e93a9b334f0f4005d3a3a9f79b66d59b9` | recurse **true** |
| UERANSIM  | `v3.3.0`  | `6bf5a1a96aaef6ae8778b9d8b477ac6e2bbf8156` | false |
| gtp5g     | `v0.10.2` | `952fb419130f5fc44cac1874e8183312006b746c` | false |

All three tags are **lightweight**, so each SHA above is the *commit* the tag points to;
`git rev-parse HEAD` after checkout equals the pin (bootstrap verifies this).

Selection rule (brief §5): each upstream pinned to the commit of its **latest stable
tagged release** (never `main`/`master`). Resolved via `git ls-remote --tags` on
2026-08-02.

## Environment matrix (captured on the ODE — pending)

| Component        | Pin | Status |
|------------------|-----|--------|
| Reference machine | ODE: bare-metal Ubuntu 24.04 LTS x86_64 | defined (`docs/ode.md`) |
| Ubuntu point release | exact `24.04.x` | **TO BE CAPTURED ON ODE** |
| Linux kernel     | exact `uname -r` (coupled to gtp5g) | **TO BE CAPTURED ON ODE** |
| gtp5g module     | `v0.10.2` build+load against the pinned kernel | **TO BE PROVEN ON ODE** |
| Docker Engine    | exact version | **TO BE CAPTURED ON ODE** |
| Docker Compose   | v2, exact version | **TO BE CAPTURED ON ODE** |
| MongoDB          | `mongo:4.4` (from ported compose) | re-confirm at M2 |
| Go               | only in build-from-source path | N/A if using published images |

## gtp5g ↔ free5GC coupling — decision & reasoning

The brief (§5) requires gtp5g to be "the version the pinned free5GC release recommends
for its UPF," and, "if free5GC gives no explicit gtp5g recommendation, pin gtp5g's latest
stable tag and note the assumption."

What was checked (2026-08-02):

1. **free5GC v4.2.3 README** (the pinned release): gives **no** explicit gtp5g version.
   → triggers the brief's fallback rule → pin gtp5g's latest stable tag.
2. **free5gc-compose README (master)**: still says *"UPF only supports GTP5G versions
   0.9.5"*. This is a **separate helper repo** (not the pinned `free5gc/free5gc` release),
   and the recommendation is **stale**: gtp5g v0.9.5 predates Linux 6.x and would not build
   against the ODE kernel line (Ubuntu 24.04 ships a 6.x kernel). Following it would
   guarantee the §8 build hard-stop.
3. **free5gc.org install guide** for a version-specific gtp5g pin: not reachable
   (HTTP 404 at the expected path) at check time.

**Decision:** pin **gtp5g `v0.10.2`** (latest stable tagged release).

- It satisfies the brief's fallback rule (free5GC v4.2.3 gives no explicit pin).
- It is the version most likely to build/load on the ODE's modern kernel, directly
  addressing risk **R-01** (kernel ↔ gtp5g).
- This is an **implementation decision within spec** (brief §5 labels version-selection
  as such). It does **not** modify upstream, change the baseline, or alter the dependency
  approach — so it is not an escalated architectural decision.

**Residual risk / escalation trigger:** the empirical build+load proof is deferred to the
lab ODE (`scripts/validate_gtp5g.sh`, brief §8). **If v0.10.2 fails to build or load on the
ODE kernel, that is the §8 hard-stop** — stop and escalate for a human re-pin (do not
auto-switch versions). This is exactly how R-01 is meant to be handled.
