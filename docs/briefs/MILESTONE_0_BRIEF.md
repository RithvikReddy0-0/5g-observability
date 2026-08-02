# Milestone 0 — Execution Brief for Claude Code

**Governing contract:** SPEC-000 v0.4 (`ENGINEERING_SPEC_Phase1.md`). This brief operationalizes Milestone 0 only. It does not supersede the SPEC; where this brief is silent, the SPEC governs.

**Prime directive for the executor:** Execute within the agreed specification. Make **implementation** decisions freely (script structure, error-message wording, helper functions, idempotency mechanics). Do **not** make **architectural** decisions — anything that changes *what* we pin, *which* environment is the baseline, *whether* to modify upstream, or *how* dependencies are managed. If a required choice is architectural (examples: "the pinned kernel can't build gtp5g, should we change the kernel or the gtp5g version?"), **stop and report** for a human decision. Do not improvise around a blocked architectural decision.

---

## 1. Objectives

1. Stand up and **document** the Official Development Environment (ODE) and verify the current machine conforms to it.
2. Establish the **manifest-based dependency system**: a SHA-pinned `manifest.lock` and a verifying `bootstrap.sh` that materializes upstreams into a git-ignored `external/`.
3. **Prove the gtp5g ↔ kernel pairing** empirically: gtp5g builds against, and loads on, the ODE's running kernel. This is the milestone's central de-risking objective.
4. Produce a frozen, machine-readable **`VERSIONS.lock`** capturing the exact validated versions, plus human-readable `docs/versions.md`.
5. Produce a **from-clean-install runbook stub** and the reproducibility guarantee that `external/` can be wiped and rebuilt identically.

Success = a reviewer on a conforming ODE can clone the repo, run one bootstrap command, and arrive at a verified dependency tree with gtp5g proven-loadable, with every version recorded.

---

## 2. Scope

**In scope (M0 does this):**
- ODE definition + environment verification tooling.
- `manifest.lock`, `bootstrap.sh`, `external/` (git-ignored), and the CI-style integrity check.
- Fetch **all three** upstreams (free5GC, UERANSIM, gtp5g) at pinned SHAs and verify them.
- **Build and load gtp5g** against the running kernel; record the result.
- `VERSIONS.lock`, `docs/versions.md`, runbook stub, `.gitignore` for `external/`.

**Out of scope (do NOT do in M0 — later milestones own these):**
- Do **not** deploy free5GC, start any Network Function, or run `docker compose up` for the core (that is M2).
- Do **not** build or run free5GC or UERANSIM binaries, configure NFs, provision subscribers, or touch UE/gNB (M2–M4). M0 only *fetches and pins* them; functional validation is deferred.
- Do **not** write any observability, Kubernetes, or slice code.
- Do **not** modify any upstream source. (If instrumentation ever becomes necessary, that is the ADR-004 ICR process, not M0.)

**Rationale for deferring free5GC/UERANSIM build to M2:** M0 de-risks the one host-coupled unknown (gtp5g/kernel). free5GC/UERANSIM functional issues surface cheaply at M2 and are resolved by re-pinning a SHA — no need to conflate them with M0.

---

## 3. Deliverables

| # | Deliverable | Path |
|---|---|---|
| D1 | ODE definition doc | `docs/ode.md` (mirrors SPEC ADR-003 ODE table) |
| D2 | Environment verification script | `scripts/verify_env.sh` |
| D3 | Dependency manifest | `manifest.lock` (repo root) |
| D4 | Bootstrap script | `scripts/bootstrap.sh` |
| D5 | Git ignore for fetched deps | `.gitignore` (contains `external/`) |
| D6 | gtp5g build+load validation | `scripts/validate_gtp5g.sh` |
| D7 | Frozen version matrix (machine-readable) | `VERSIONS.lock` |
| D8 | Version matrix rationale (human-readable) | `docs/versions.md` |
| D9 | From-clean-install runbook stub | `docs/runbooks/clean-install.md` |
| D10 | M0 report (results of the run) | `docs/runbooks/m0-report.md` |

Scripts must be POSIX-friendly bash, `set -euo pipefail`, non-interactive by default, and print a clear final PASS/FAIL summary. Implementation style is the executor's choice; the **contracts below are binding**.

---

## 4. Repository changes

Create/modify only these paths. Do not scaffold future-phase directories beyond what M0 needs (M1 owns the full skeleton).

```
repo-root/
├── manifest.lock                     # D3
├── VERSIONS.lock                     # D7
├── .gitignore                        # D5 — must include: external/
├── docs/
│   ├── ode.md                        # D1
│   ├── versions.md                   # D8
│   └── runbooks/
│       ├── clean-install.md          # D9
│       └── m0-report.md              # D10
├── scripts/
│   ├── verify_env.sh                 # D2
│   ├── bootstrap.sh                  # D4
│   └── validate_gtp5g.sh             # D6
└── external/                         # created by bootstrap; GIT-IGNORED, never committed
```

`external/` must never be committed. Nothing our project authors goes inside `external/`.

---

## 5. `manifest.lock` specification

**Format:** JSON (unambiguous, `jq`-parseable). **Schema:**

```json
{
  "schema_version": "1",
  "dependencies": [
    {
      "name": "free5gc",
      "url": "https://github.com/free5gc/free5gc.git",
      "commit": "<full 40-char git SHA>",
      "ref_hint": "<human tag the SHA came from, e.g. v4.x.y — NON-authoritative>",
      "dest": "external/free5gc",
      "recurse_submodules": true
    },
    {
      "name": "ueransim",
      "url": "https://github.com/aligungr/UERANSIM.git",
      "commit": "<full 40-char git SHA>",
      "ref_hint": "<tag>",
      "dest": "external/ueransim",
      "recurse_submodules": false
    },
    {
      "name": "gtp5g",
      "url": "https://github.com/free5gc/gtp5g.git",
      "commit": "<full 40-char git SHA>",
      "ref_hint": "<tag>",
      "dest": "external/gtp5g",
      "recurse_submodules": false
    }
  ]
}
```

**Binding rules:**
- `commit` is the **authoritative pin** and MUST be a full 40-character SHA. Tags/branches are forbidden as the pin (they move). `ref_hint` records the human-facing origin only and is never used for checkout.
- **free5GC uses git submodules for its NFs**, so `recurse_submodules: true` for free5gc; the superproject SHA deterministically fixes the NF submodule SHAs. gtp5g and UERANSIM are standalone (`false`).
- **Version-selection rule (implementation decision, within spec):** pin each upstream to the commit of its **latest stable tagged release** (never `main`/`master`/dev). Resolve the tag to its SHA and record both. **Coupling constraint:** the **gtp5g** version MUST be the one the pinned free5GC release recommends for its UPF (check that free5GC release's install/README guidance) — do **not** independently pick "latest gtp5g" if it diverges from what free5GC expects. If free5GC gives no explicit gtp5g recommendation, pin gtp5g's latest stable tag and note the assumption in `m0-report.md`.
- If two upstreams appear mutually incompatible at pin-time, record the conflict in `m0-report.md` and proceed (functional validation is M2) — unless the conflict blocks gtp5g loading, which is a hard failure (§9).

---

## 6. `bootstrap.sh` responsibilities

Binding behavioral contract (mechanics are the executor's choice):

1. **Read** `manifest.lock` (via `jq`); iterate dependencies.
2. **Materialize** each into its `dest`: clone if absent, else fetch; then checkout the exact pinned `commit`; recurse submodules when flagged.
3. **Verify** after checkout: `git -C <dest> rev-parse HEAD` MUST equal the pinned `commit`; for `recurse_submodules: true`, submodule states MUST match the superproject. Any mismatch → non-zero exit with a clear message naming the dependency.
4. **Idempotent:** safe to re-run; converges to the pinned state regardless of prior `external/` contents.
5. **Modes:**
   - default → fetch + checkout + verify;
   - `--clean` → remove `external/` entirely, then fetch fresh (this backs the "disposable `external/`" reproducibility guarantee);
   - `--verify-only` → do **not** fetch; assert existing `external/` matches `manifest.lock` (this is the CI/integrity gate). Missing or drifted `external/` → non-zero exit.
6. **Isolation:** writes only under `external/`. Never writes into our source tree. Never commits anything.
7. **Output:** per-dependency status lines and a final `BOOTSTRAP: PASS/FAIL`.
8. **Network failure handling:** if an upstream URL is unreachable, fail clearly (name the dep + URL). Optionally support an archive cache, but do not require network for `--verify-only`.

---

## 7. Dependency verification (integrity contract)

- **What "verified" means:** for every dependency, the checked-out `HEAD` equals the manifest `commit` exactly (and submodules match for free5gc). This is the equivalent-to-submodules reproducibility guarantee.
- **CI gate:** `scripts/bootstrap.sh --verify-only` MUST be runnable as a CI step and MUST fail the build on any drift. (Wiring CI into an actual runner is M1; M0 delivers the verify-only mode and demonstrates it locally.)
- **Determinism proof:** `bootstrap.sh --clean` followed by a fresh run MUST yield byte-identical pinned SHAs. Record this in `m0-report.md`.

---

## 8. gtp5g build and validation (`validate_gtp5g.sh`) — the central objective

Binding contract:

1. **Preconditions:** kernel headers for the *running* kernel present (`linux-headers-$(uname -r)`) and build toolchain available (`build-essential`, `make`, `gcc`). The script installs/points to these as needed via `apt` (sudo).
2. **Build:** build gtp5g from `external/gtp5g` against the running kernel (its documented `make` flow). Capture full build output to a log.
3. **Load:** install/load the module (`make install` + `modprobe gtp5g`, or `insmod` the built `.ko`). Capture `dmesg` output around the load.
4. **Validate — all of these MUST hold:**
   - `lsmod | grep -q gtp5g` → module present;
   - `dmesg` shows a clean load with no error/taint related to gtp5g;
   - module metadata readable (`modinfo gtp5g`), and its version/SHA recorded.
5. **Record** into `VERSIONS.lock` and `m0-report.md`: `uname -r`, gtp5g commit SHA, gtp5g module version, build result, load result, timestamp.
6. **Hard-stop rule:** if gtp5g **fails to build or load** on the ODE's running kernel, this is a **blocking architectural condition**. STOP. Write full diagnostics (build log, `dmesg`, kernel version, gtp5g SHA) to `m0-report.md` and report for a human decision. Do **NOT** autonomously: switch kernels, pick a different gtp5g version, patch gtp5g source, or downgrade Ubuntu — every one of those is an architectural re-pin that requires human ratification (it changes `VERSIONS.lock`).

---

## 9. Environment verification (`verify_env.sh`)

Assert the current machine against the ODE (SPEC ADR-003). Emit a report; classify each check PASS / WARN / FAIL.

| Check | Method (guidance) | On failure |
|---|---|---|
| OS = Ubuntu 24.04 LTS | `lsb_release -a` / `/etc/os-release` | FAIL |
| Arch = x86_64 | `uname -m` | FAIL |
| **Not WSL** | grep `microsoft`/`WSL` in `/proc/version` | **FAIL (hard)** |
| Virtualization | `systemd-detect-virt` (`none` = bare-metal) | **WARN** — bare-metal is the baseline; a VM run is NON-BASELINE, not a hard stop for dev iteration |
| CPU threads ≥ 8 | `nproc` | FAIL |
| RAM ≥ 16 GB | `/proc/meminfo` | FAIL |
| Free SSD ≥ 100 GB | `df` on the target path | FAIL |
| sudo available | `sudo -n true` or prompt | FAIL |
| Docker Engine present | `docker version` | FAIL |
| Docker Compose v2 | `docker compose version` (plugin, not `docker-compose`) | FAIL |

- Record every discovered version (Docker, Compose, kernel, Ubuntu point release) — these feed `VERSIONS.lock`.
- **Baseline verdict:** M0 sign-off requires a fully conforming ODE (no FAILs, virtualization = none). If run on a non-conforming machine, the script must print a prominent `NON-BASELINE ENVIRONMENT` banner and still produce its report, but the official M0 exit checklist (§11) is only satisfiable on a conforming ODE.

---

## 10. Acceptance criteria (binary)

M0 is complete only when **all** are true and documented in `m0-report.md`:

- [ ] `verify_env.sh` reports a **conforming ODE** (all FAIL-class checks pass; virtualization = none).
- [ ] `manifest.lock` exists, schema-valid, all three deps pinned to full 40-char SHAs; gtp5g pin respects the free5GC coupling rule (§5).
- [ ] `bootstrap.sh` (default) materializes `external/` and **verifies** all SHAs (and free5gc submodules) — final `BOOTSTRAP: PASS`.
- [ ] `bootstrap.sh --verify-only` passes on the materialized tree and **fails** on an intentionally drifted tree (demonstrated once).
- [ ] `bootstrap.sh --clean` then re-run yields **identical** pinned SHAs (determinism proof recorded).
- [ ] `external/` is git-ignored and confirmed **not** tracked by git.
- [ ] `validate_gtp5g.sh` shows gtp5g **built and loaded** on the running kernel: `lsmod` visible, clean `dmesg`, `modinfo` recorded.
- [ ] `VERSIONS.lock` frozen with: Ubuntu point release, exact `uname -r`, Docker + Compose versions, free5GC/UERANSIM/gtp5g SHAs, gtp5g module version.
- [ ] `docs/ode.md`, `docs/versions.md`, `docs/runbooks/clean-install.md`, `docs/runbooks/m0-report.md` present and accurate.
- [ ] No upstream source was modified; nothing under `external/` was committed.

---

## 11. Failure conditions (when to STOP and report vs. self-correct)

**Self-correct (implementation-level — fix and continue):** missing headers/toolchain (install them), a bootstrap script bug, a wrong path, non-idempotent behavior, a `jq` parsing mistake, a `.gitignore` omission.

**STOP and report (architectural — do not improvise):**
- gtp5g fails to build or load on the ODE kernel (§8 hard-stop).
- The latest stable free5GC and its recommended gtp5g are mutually incompatible in a way that blocks the gtp5g load.
- The ODE as specified cannot satisfy a requirement (e.g., 24.04's kernel line is fundamentally unsupported by any recommended gtp5g) — this is a request to re-ratify ADR-003/`VERSIONS.lock`, which is a human decision.
- Any situation that would require modifying upstream source, changing the pinned OS/kernel, or altering the dependency-management approach.

When stopping: write complete diagnostics to `m0-report.md`, state the exact decision needed, and do not proceed past the block.

---

## 12. Exit checklist

M0 is signed off when:

1. [ ] All §10 acceptance criteria pass on a conforming ODE.
2. [ ] `m0-report.md` records the full run: env report, pinned SHAs, gtp5g build+load evidence, determinism proof.
3. [ ] `VERSIONS.lock` is frozen and committed.
4. [ ] Deliverables D1–D10 exist at their specified paths; `external/` is untracked.
5. [ ] No architectural decision was made autonomously; any encountered were escalated per §11.

On sign-off, M0 output becomes the foundation every later milestone builds on. **M1** (full repo skeleton, ADRs, CI wiring) proceeds next.

---

*End of Milestone 0 execution brief. Executor: build within this contract; escalate architecture, decide implementation.*
