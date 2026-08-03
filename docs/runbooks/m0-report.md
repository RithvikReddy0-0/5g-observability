# Milestone 0 — Run Report (D10)

**Status:** Partial — all M0 **file/authoring** deliverables produced on a non-baseline
machine (WSL2). The empirical, ODE-only steps (gtp5g build+load, freezing the environment
half of `VERSIONS.lock`, bootstrap determinism proof) are **deferred to the lab ODE** and
are called out below. No architectural decision was made autonomously.

Authoring machine (this commit): `DESKTOP-6I3KSV1`, WSL2 Ubuntu 24.04.4,
kernel `6.6.87.2-microsoft-standard-WSL2`, 12 threads, ~7.6 GiB RAM.

---

## 1. Environment verification (`verify_env.sh`)

Run once here to confirm it correctly classifies this machine as **NON-BASELINE**:

```
  OS                     : PASS  Ubuntu 24.04 LTS
  Arch                   : PASS  x86_64
  Not-WSL (hard)         : FAIL  WSL kernel signature present in /proc/version
  Virtualization         : WARN  wsl (baseline requires bare-metal)
  CPU threads            : PASS  12 (>=8)
  RAM                    : FAIL  7 GiB (<16 GB)
  Free disk              : PASS  261 GiB (>=100 GB)
  sudo                   : WARN  could not confirm non-interactive sudo
  Docker Engine          : PASS  29.1.3
  Docker Compose v2      : FAIL  'docker compose' plugin not available
  summary: PASS=5  WARN=2  FAIL=3  ->  NON-BASELINE ENVIRONMENT
```

The script reaches `exit 1` on this machine (verified via `bash -x`; the exit code cannot
be observed through the `wsl -- bash -c` interop layer, which is a display artifact, not a
script defect). On a conforming ODE it prints `VERDICT: CONFORMING ODE` and exits 0.

**Three FAIL-class items here are expected and acceptable for file work:** WSL (hard),
RAM < 16 GB, and no `docker compose` plugin. They block *M0 sign-off*, not authoring.

---

## 2. Pinned upstreams (`manifest.lock`)

Tags resolved to commit SHAs via `git ls-remote --tags` on 2026-08-02. All three are
**lightweight** tags, so each SHA is the commit itself (bootstrap's `HEAD == pin` check holds).

| Component | Tag | Commit | Submodules |
|-----------|-----|--------|------------|
| free5GC   | v4.2.3  | `3b34a08e93a9b334f0f4005d3a3a9f79b66d59b9` | recurse **true** |
| UERANSIM  | v3.3.0  | `6bf5a1a96aaef6ae8778b9d8b477ac6e2bbf8156` | false |
| gtp5g     | v0.10.2 | `952fb419130f5fc44cac1874e8183312006b746c` | false |

### gtp5g coupling decision (why v0.10.2, not the compose's v0.9.5)

- **free5GC v4.2.3 README** — the pinned release — gives **no** explicit gtp5g version,
  which triggers the brief's fallback rule (§5): *pin gtp5g's latest stable tag and note it*.
- **free5gc-compose README (master)** still says *"UPF only supports GTP5G versions 0.9.5"*,
  but (a) that is a **separate helper repo**, not the pinned `free5gc/free5gc` release, and
  (b) it is **stale** — v0.9.5 predates Linux 6.x and would not build on the ODE kernel line
  (Ubuntu 24.04 ships a 6.x kernel), which would itself force the §8 build hard-stop.
- **free5gc.org install guide** for a version-specific pin was **unreachable** (HTTP 404) at
  check time.

→ Pinned **gtp5g v0.10.2** (latest stable). Classified as an **implementation decision within
spec** (brief §5). It does not modify upstream, change the baseline, or alter the dependency
approach. **Escalation trigger:** if v0.10.2 fails to build/load on the ODE kernel, that is the
§8 hard-stop — STOP and request a human re-pin; do not auto-switch versions (risk R-01).

### Container-image vs source drift (note, resolve at M2)

The ported `deployments/compose/docker-compose.yaml` references **`free5gc/*:v4.2.0`** published
images, while source is pinned to **v4.2.3**. M0 only pins source; reconcile at M2 (bump image
tags or build-from-source). Recorded in `VERSIONS.lock.container_images`.

---

## 3. Deferred to the lab ODE (NOT done here)

- `scripts/bootstrap.sh` was **written but not executed** here (not requested for this pass; a
  full free5GC + submodules clone is heavy and `external/` is git-ignored). Network *was*
  confirmed available (ls-remote succeeded), so bootstrap is not blocked — it simply runs on
  the ODE. Determinism proof (`--clean` then re-run → identical SHAs) is an ODE step.
- `scripts/validate_gtp5g.sh` was **written but deliberately not run** — it refuses to run on
  WSL2 unless `ALLOW_NON_BASELINE=1`. gtp5g build+load is the central ODE objective.
- Environment half of `VERSIONS.lock` (exact point release, `uname -r`, Docker/Compose versions,
  gtp5g module version + load result) remain `TO_BE_FILLED_ON_LAB_ODE`.

---

## 4. Slice-consistency audit

See section 5 below (full table). Summary: the modeled single slice
**S-NSSAI (SST 1 / SD 010203)** on **PLMN 208/93**, **TAC 1** is consistent end-to-end across
AMF, NSSF, SMF, gNB, and UE. Findings F1–F5 are observations/gaps to resolve before the Phase-1
freeze; none were silently changed.

---

## 5. Slice-consistency audit — detail

### Per-file values

| File | Role | PLMN (MCC/MNC) | TAC | S-NSSAI (SST/SD) |
|------|------|----------------|-----|------------------|
| `deployments/compose/config/amfcfg.yaml` | AMF | 208/93 (guami, tai, plmnSupport) | `000001` = 1 | plmnSupportList: **(1, 010203)**, (1, 112233) |
| `deployments/compose/config/nssfcfg.yaml` | NSSF | 208/93 (supportedPlmnList) | serving TAI absent (taList uses 466/92 etc.) | supportedNssaiInPlmn(208/93): **(1, 010203)**, (1,112233), (1,000003), (2,000001), (2,000002) |
| `deployments/compose/config/smfcfg.yaml` | SMF | 208/93 (plmnList) | — | snssaiInfos: **(1, 010203)**, (1,112233); UPF: (1,010203)→10.60.0.0/16, (1,112233)→10.61.0.0/16 |
| `deployments/compose/config/udrcfg.yaml` | UDR | — (Mongo conn only) | — | none in file (subscriber S-NSSAI lives in MongoDB) |
| `ran/config/free5gc-gnb.yaml` | gNB | 208/93 | 1 | **(1, 010203)** |
| `ran/config/free5gc-ue.yaml` | UE | 208/93 (supi `imsi-208930000000001`) | — | session **(1, 010203)**; configured-nssai **(1, 010203)**; default-nssai (1, 1) |

### Findings (reported, NOT fixed)

- **F1 — PASS (primary slice consistent).** (SST 1 / SD 010203) @ 208/93, TAC 1 matches across
  AMF, NSSF, SMF, UPF info, gNB, and UE. This is the ADR-007 explicitly-modeled single slice.
- **F2 — Stock extra slices present in the core, unused by the RAN.** AMF & SMF also advertise
  (1, 112233); NSSF advertises (1,112233), (1,000003), (2,000001), (2,000002). The gNB/UE use only
  (1, 010203). These are free5gc-compose defaults. ADR-007 wants a *clean* single slice, not
  defaults-plus-extras. **Decision needed** (potentially architectural re: ADR-007): prune to
  exactly one slice for the Phase-1 baseline, or keep extras. Not changed here.
- **F3 — NSSF stock example topology. [CORRECTED — was NOT harmless; it blocked registration.]**
  `nssfcfg` `nsiList` / `amfSetList` / `taList` / `mappingListFromPlmn` are populated with
  unrelated PLMNs (466/92, 310/560, 440/10) and TACs (33456–33459), and the serving TAI
  (208/93, TAC 000001) was **absent** from `taList`.
  This report originally judged that "harmless for the basic single-slice path." **That was
  wrong.** At M2 bring-up it proved to be a hard blocker: NSSF logged
  `No TA {208/93, tac 000001} in NSSF configuration`, the AMF then failed with
  `AMF can not select an target AMF by NRF`, and every UE registration died on T3510 expiry.
  Fixed by adding the serving TAI to `taList` — the TAC must be the quoted 6-hex-digit string
  `"000001"`; an integer `1` does not match. The remaining foreign-PLMN sample data is still
  present and still matters for **Phase 1.5 NSSF validation**.
- **F4 — Subscriber↔slice not file-verifiable.** UDR config is DB-connection only; the subscriber's
  provisioned S-NSSAI is in MongoDB (set via WebUI at M2). The "subscriber/UDR" leg of the audit
  cannot be checked from files now — it **must** be verified at M2 to equal (1, 010203).
- **F5 — UE default-nssai differs (minor). [RESOLVED at M2.]** `default-nssai` was (1, sd 1)
  ≠ `configured-nssai` (1, 010203). Aligned to the modeled slice when `uecfg.yaml` was reduced
  to a single requested S-NSSAI (see F2 note below).

- **F2 follow-up [PARTIALLY ACTIONED at M2].** The extra stock slice (1, 112233) was removed
  from the **UE** request (`uecfg.yaml`) because requesting a slice the subscriber is not
  provisioned for makes the AMF fail with `AMF can not select an target AMF by NRF`. The extra
  slices still present in **AMF / SMF / NSSF** config were left untouched — pruning those
  remains the open ADR-007 decision for you.

---

## 6. Deliverables produced this pass

| ID | Deliverable | Path | State |
|----|-------------|------|-------|
| D1 | ODE definition | `docs/ode.md` | done |
| D2 | Env verify script | `scripts/verify_env.sh` | done + run (NON-BASELINE) |
| D3 | Manifest | `manifest.lock` | done (SHAs pinned) |
| D4 | Bootstrap | `scripts/bootstrap.sh` | written (run on ODE) |
| D5 | Git ignore of external/ | `.gitignore` | done |
| D6 | gtp5g validation | `scripts/validate_gtp5g.sh` | written (ODE-only; guarded) |
| D7 | Version matrix (machine) | `VERSIONS.lock` | template + SHAs; env values pending ODE |
| D8 | Version rationale (human) | `docs/versions.md` | done |
| D9 | Clean-install runbook | `docs/runbooks/clean-install.md` | stub |
| D10 | This report | `docs/runbooks/m0-report.md` | done |
