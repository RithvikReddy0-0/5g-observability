# Cloud-Native Observability Plane for free5GC — Engineering Specification

**Document ID:** SPEC-000
**Status:** Accepted (ratified at inception)
**Version:** 0.5 (Project Inception + ADR-009 re-sequencing)
**Scope:** Phase 1 (Stable Baseline) + Phase 1.5 (multi-slice) with forward-looking architecture for Phases 2–5
**Source of truth:** *Master Project Bible v1* (handbook) + decisions ratified in this document
**Authority:** This specification governs architecture. Where it conflicts with the handbook, this document wins; where it is silent, the handbook wins.

**Phase model (ratified):** Phase 1 = single-slice functional baseline → **freeze** → Phase 1.5 = second slice + NSSF validation → **freeze** → Phase 2 = (a) migrate frozen baseline to Kubernetes, *then* (b) introduce the observability stack → Phases 3–5 = slice observability, analytics, AI.

**Changelog:** v0.5 — **ADR-009 added**, superseding the *sequencing* clause of ADR-001 only: the observability plane was built on Docker Compose **before** the Phase 2a Kubernetes migration, and the Phase 1.5 second slice was added **before** the Phase 1 freeze. Driven by the ODE being unavailable — the UPF needs gtp5g, so Phase 1 cannot be completed or frozen on the development host. Phase 1's acceptance criteria are **unchanged and explicitly not met**; `tests/acceptance.sh` reports the outstanding ones as `SKIP-ODE` and refuses to declare Phase 1 complete. ADR-001's Compose-first decision and K8s-portability guardrails remain in force. See `docs/adr/ADR-009-phase-resequencing.md`. v0.4 — Official Development Environment (ODE) ratified as the reference *platform* (Ubuntu 24.04 LTS, x86_64, bare-metal, ≥8 threads / ≥16 GB / ≥100 GB SSD); ADR-003 and M0 updated; VMs/cloud/CI demoted to future secondary targets. v0.3 — ADR-002 switched from Git submodules to a SHA-pinned `manifest.lock` + verifying `bootstrap.sh` into a git-ignored `external/`; repo layout, M1, and R-04 updated accordingly. v0.2 — second slice reframed as a named Phase 1.5 *after* the Phase 1 freeze (was M5 inside Phase 1); Phase 2 sequenced explicitly as K8s-migration-then-observability; per-component deployment model added to §3.

---

## 0. How to read this document

This is the project's inception blueprint. It contains no implementation. It records **what we have decided, why, and what tests prove we succeeded.** It is organized so that each part is independently referenceable:

1. Decision ledger (quick reference to the 10 decisions and where I refined them)
2. Architecture Decision Records (ADR-001 … ADR-008)
3. Observability Plane architecture (design only)
4. Repository layout
5. Phase 1 acceptance criteria (binary, testable)
6. Implementation roadmap (milestones M0–M5, then Phase 1.5)
7. Risk register

A note on my role: you asked me to challenge decisions I disagree with. I agree with the *direction* of all ten of your decisions. I have refined five of them where I think the stated form has a technical trap. Those refinements are marked **[REFINED]** in the ledger and argued in the relevant ADR. Nothing here silently overrides your intent.

---

## 1. Decision ledger

| # | Decision | Disposition | Where |
|---|----------|-------------|-------|
| 1 | Compose-first, Kubernetes-second | **Accepted, [REFINED]** — add K8s-portability guardrails to Phase 1; Phase 2 sequenced as **migrate-to-K8s then observability**; flag the user plane (UPF/gtp5g) as the non-uniform migration cost | ADR-001 |
| 2 | Everything version-pinned | **Accepted, [REFINED]** — pins are a *validated matrix*, not independent values; kernel↔gtp5g are coupled; Go pin only applies to build-from-source | ADR-003 |
| 3 | Prefer external instrumentation; minimal isolated source changes allowed | **Accepted, [REFINED]** — source changes maintained as a rebasable patch series, gated by an Instrumentation Change Record | ADR-004 |
| 4 | Structured JSON logging from the start | **Accepted, [REFINED]** — JSON to the collection path, human-readable console retained for interactive debugging | ADR-005 |
| 5 | Single slice first, add slices as a later milestone | **Accepted, [REFINED]** — single slice is *explicitly modeled* (real SST/SD), never defaulted; second slice is a named **Phase 1.5** after the Phase 1 freeze | ADR-007 |
| 6 | Fully design the observability plane before building | **Accepted** — designed in §3; OpenTelemetry adopted as the telemetry spine | ADR-006 |
| 7 | ADRs with a fixed template | **Accepted** — added a `Status` field (standard ADR practice) | §2 |
| 8 | Production-quality repo layout | **Accepted, [REFINED]** — repo owns only our code; upstreams (free5GC, UERANSIM, gtp5g) are pinned by a `manifest.lock` (exact SHAs) + bootstrap script into `external/`, kept outside our source tree | ADR-002 |
| 9 | Objective Phase 1 acceptance criteria | **Accepted** — defined as binary checks in §4 | §4 |
| 10 | Milestone-based roadmap, no code yet | **Accepted** — §5 | §5 |

**Three cross-cutting points I am elevating that were not in your ten:**

- **The UPF + gtp5g + kernel triad is the #1 project risk** and it touches version pinning, reproducibility, *and* the eventual K8s migration simultaneously. It gets its own risk entry (R-01) and shapes ADR-001 and ADR-003.
- **"Reproducible from a clean Ubuntu install" is undefined until we pin the reference machine** (bare metal vs VM vs cloud; distro kernel vs mainline; WSL2 explicitly excluded). This becomes a deliverable of Milestone 0.
- **"Understand every NF and interface" must produce artifacts, not a feeling.** The handbook stops before the detailed registration and PDU-session procedures, so those are net-new authored deliverables (interface catalog + two sequence diagrams), and they are acceptance criteria, not optional.

---

## 2. Architecture Decision Records

Template per ADR: **Status · Context · Problem · Decision · Alternatives Considered · Rationale · Consequences.**

---

### ADR-001 — Deployment Strategy: Docker Compose first, Kubernetes second

**Status:** Accepted

**Context.** The project's end state is a cloud-native observability plane whose Phase 2+ tooling (Prometheus, OpenTelemetry Collector, Grafana) is native to Kubernetes. The immediate need (Phase 1) is a stable, reproducible, fully-understood free5GC baseline. free5GC ships a Docker Compose reference deployment; its Kubernetes story (towards5gs / Helm, or hand-written manifests) is heavier and introduces CNI/Multus and privileged-pod concerns before we even have a working core.

**Problem.** Starting on Kubernetes front-loads the hardest networking problems (user-plane forwarding, kernel module on nodes) onto a team that does not yet have a working baseline to compare against. Starting on Compose risks making Phase-1 choices that are hostile to a later K8s port, forcing rework.

**Decision.** Phase 1 uses **Docker Compose exclusively**. Kubernetes is the Phase 2 target and appears in Phase 1 **only as planning documentation**. However, Phase 1 Compose artifacts must be written under **K8s-portability guardrails** (below), so migration is a port and not a redesign.

**Phase 2 is internally sequenced:** (a) migrate the *frozen* baseline to Kubernetes and re-establish the same end-to-end behavior (the frozen baseline is the regression oracle), **then** (b) introduce the observability stack on the working K8s deployment. Rationale: never debug three unknowns at once. Understanding free5GC (Phase 1), moving it to K8s (Phase 2a), and instrumenting it (Phase 2b) are kept strictly sequential so that when something breaks, only one variable changed.

K8s-portability guardrails for Phase 1:
- Reference peers by **service name / DNS**, never by hardcoded container IP (the handbook already mandates this; we make it a hard rule).
- All tunables (PLMN, TAC, S-NSSAI, addresses, log level) live in **mounted config / env**, never baked into images.
- Prefer **bridge networking** over host networking where the NF tolerates it; document every place host networking or a privileged capability is genuinely required (this is the UPF, primarily).
- One concern per container; no "sidecar of convenience" that a Pod could not reproduce.

**Alternatives considered.**
- *Kubernetes from day one.* Rejected for Phase 1: it couples "get 5G working" with "get 5G working on K8s," doubling the failure surface before we have a known-good reference. Reconsidered in Phase 2 from a position of understanding.
- *Compose as the permanent end state (no K8s).* Rejected: contradicts the project's cloud-native thesis and the handbook's Phase 2 tooling, which is K8s-shaped.
- *Bare-metal / systemd deployment of each NF.* Rejected: worst reproducibility, no clean migration path.

**Rationale.** Fastest path to a frozen, understood baseline; keeps the two hard problems (5G correctness vs cloud-native orchestration) sequential rather than simultaneous; the guardrails cap the migration tax without doing any K8s work now.

**Consequences.**
- (+) Phase 1 is achievable and debuggable; a frozen baseline becomes the regression oracle for the Phase 2 migration.
- (+) Guardrails mean the migration is mechanical for the control plane.
- (−) The **user plane does not migrate uniformly.** The UPF needs the gtp5g kernel module on the host/node, elevated capabilities, and a real data interface. On K8s this implies Multus (multiple interfaces), privileged pods, and the module present on every node that can schedule the UPF. This is the expensive part of Phase 2 and we accept that cost knowingly. It is logged as R-01.

---

### ADR-002 — Repository Structure & Dependency Management

**Status:** Accepted (revised v0.3 — manifest-based, replacing submodules)

**Context.** The repo must host our code and docs plus three large upstream projects (free5GC, UERANSIM, gtp5g) that must be **version-pinned** and **reproducible** without polluting our history. These upstreams are not libraries we import into our source — they are separately-built artifacts. It must also leave clean, non-rotting space for future K8s manifests and observability components that Phase 1 does not implement.

**Problem.** Three anti-patterns to avoid: (a) vendoring upstream source directly into our tree (bloats history, breaks upstream tracking, makes the pin invisible); (b) Git submodules (git-native pinning, but recurring contributor-workflow friction — detached HEADs, forgotten `--recurse-submodules`, CI clone complexity); (c) a dozen empty future directories that rot.

**Decision.** The repository **owns only our project's code.** Upstreams are treated as **pinned external dependencies**, declared in a `manifest.lock` at the repo root and materialized by a **bootstrap script** into an `external/` directory that is **git-ignored** (never committed). Specifically:
- `manifest.lock` lists, per upstream, the **repository URL and the exact commit SHA** — never a tag or branch (those can move; SHAs cannot).
- `scripts/bootstrap.sh` clones/fetches each upstream at its pinned SHA into `external/`, and **verifies** the resulting checkout matches the lock (fails loudly on mismatch).
- **CI runs bootstrap and asserts integrity**, so a stale or drifted `external/` cannot pass.
- Updating an upstream is a reviewed change to `manifest.lock` (new SHA) — a deliberate, auditable event, paired with re-validation of the version matrix (ADR-003) when the change is coupled (e.g. gtp5g).
- Future-phase directories exist but each contains only a `README.md` stub ("Phase N — not implemented; see SPEC") so the tree communicates intent without rotting.

**Alternatives considered.**
- *Git submodules.* Rejected — git-native and tooling-visible, but the workflow friction (onboarding, CI, accidental commits of moved pointers) outweighs the benefit for artifacts we only fetch-and-build. A SHA-pinned manifest plus a CI integrity check delivers equivalent reproducibility without the friction.
- *Vendored upstreams.* Rejected — bloats history, breaks upstream tracking, hides the pin.
- *Monorepo with everything flat / polyrepo.* Rejected — the former violates separation of responsibilities; the latter is premature during inception.

**Rationale.** SHA-pinned manifest + verifying bootstrap gives reproducibility as strong as submodules (exact commits, CI-enforced) while delivering the properties you prioritized: simpler onboarding (one `bootstrap.sh`), cleaner CI, easier updates (edit one file), and no submodule Git-workflow traps. Keeping `external/` git-ignored means our history contains only our work.

**Consequences.**
- (+) Reproducible (exact SHAs, CI-verified), clean history, trivial onboarding, easy auditable updates.
- (+) `external/` can be wiped and re-bootstrapped at any time — good for clean-machine reproducibility tests (ties directly to M0).
- (−) The pin lives in a file we must discipline ourselves to verify, rather than in git's own model — mitigated by the mandatory CI integrity check (a drifted checkout fails the build).
- (−) Bootstrap must handle upstream availability (a deleted/renamed upstream repo breaks fetch) — mitigated by optionally caching known-good archives; logged as R-04.

---

### ADR-003 — Version Pinning Strategy

**Status:** Accepted

**Context.** Reproducibility is a hard requirement: Phase 1 must reproduce from a clean Ubuntu install. Several pinned components are **not independent** — most critically the **Linux kernel and the gtp5g module**, which must compile and load against that exact kernel.

**Problem.** Pinning each component in isolation ("kernel X, gtp5g Y") gives a false sense of safety. gtp5g builds out-of-tree against kernel headers; a kernel bump can break it silently. Likewise, `docker compose` v2 vs the legacy `docker-compose` v1 changes file semantics, and the **Go pin only matters if we build free5GC from source** rather than using published images.

**Decision.** Maintain a single **validated version matrix** (one file: `VERSIONS.lock` + human-readable `docs/versions.md`) in which the combination is validated *together* on the reference machine, not component by component. The matrix pins:

| Component | Pin | Notes |
|---|---|---|
| Reference machine | **the ODE** (defined below) | bare-metal Ubuntu 24.04 LTS, x86_64; **WSL2, VMs, and cloud instances excluded from the baseline** (may be documented later as *secondary* targets) |
| Ubuntu | 24.04 LTS, exact point release | ratified; pin the exact point release in M0 |
| Linux kernel | exact `uname -r` | **coupled to gtp5g**; the pin is the *pair*. 24.04 ships a 6.8 GA kernel (and an HWE track that can bump it) — M0 pins the exact running kernel and proves gtp5g against it |
| gtp5g module | exact commit/tag | must build+load against the pinned kernel; validated in M0 |
| Docker Engine | exact version | |
| Docker Compose | v2, exact version | standardize on `docker compose` (plugin), not v1 |
| free5GC | exact tag/commit | confirm latest stable tag at M0 |
| UERANSIM | exact tag/commit | must be compatible with the pinned free5GC |
| MongoDB | exact version | as required by the pinned free5GC |
| Go | exact version | **only in the build-from-source path**; N/A if using published images |

**Alternatives considered.**
- *"Latest" everywhere.* Rejected — non-reproducible by definition.
- *Independent per-component pins.* Rejected — ignores the kernel↔gtp5g coupling, the exact failure mode that wrecks reproducibility.
- *Full image digests only (ignore host).* Rejected — the UPF's kernel dependency lives on the host, outside any image.

**Rationale.** The matrix treats the environment as one validated artifact. Exact values are deliberately deferred to M0 because they must be *proven together on the reference machine*; guessing them here would be theater.

**Consequences.** (+) True reproducibility. (+) Upgrades become a deliberate re-validation event, not a drift. (−) Upgrading any coupled component (esp. kernel) triggers re-validation of gtp5g; accepted.

#### Official Development Environment (ODE) — the ratified reference platform

The baseline is validated against a **reference *environment*, not a single physical machine.** Every milestone's validation is defined as "passes on the ODE." The ODE for the initial baseline is:

| Attribute | Requirement |
|---|---|
| OS | Ubuntu **24.04 LTS** |
| Architecture | **x86_64** |
| Installation | **Bare-metal** (not VM, not WSL2, not container-in-container) |
| CPU | Intel or AMD, **≥ 8 logical threads** |
| RAM | **≥ 16 GB** (32 GB recommended) |
| Storage | **≥ 100 GB free SSD** |
| Privileges | **Full sudo** |
| Container runtime | **Docker Engine** + **Docker Compose v2** (`docker compose` plugin) |

Rules: (1) the ODE **defines the baseline**; a milestone is not "done" until it passes on the ODE. (2) VMs, cloud instances, and CI runners may later be documented as **secondary targets** with their own caveats, but they never redefine the baseline. (3) The ODE is a *class* of machine, so the exact `VERSIONS.lock` (kernel point release, gtp5g SHA, Docker versions) is still captured from the specific box used in M0 and must reproduce on any conforming ODE.

---

### ADR-004 — Instrumentation Philosophy

**Status:** Accepted

**Context.** Default principle: prefer external instrumentation. But two capabilities we will want later are hard or impossible to obtain externally: **distributed traces across the SBI** (free5GC does not natively emit OpenTelemetry spans) and some **per-slice metrics** (not all NFs expose slice-labeled counters). So "external only" cannot be absolute.

**Problem.** Once we permit source changes, we risk (a) drifting from upstream, (b) uncontrolled scope creep of patches, and (c) accidentally altering functional behavior — which would poison the very telemetry we are trying to trust.

**Decision.** External instrumentation is the default and is always attempted first. A source change to free5GC is permitted **only** when the capability cannot reasonably be achieved externally, and only under all of these constraints:
- maintained as a **rebasable patch series** (`git format-patch` overlay, or a tracked fork with disciplined rebase) — never a silent modification of vendored code;
- **behavior-preserving** — instrumentation only; no change to protocol logic, timing, or output on the wire;
- **reversible and isolated** — removing the patch returns to stock upstream;
- **gated by an Instrumentation Change Record (ICR)** — a lightweight doc (context, why external failed, exact change, revert steps) reviewed before merge;
- **version-controlled and documented** in `docs/`.

The external-first ladder we will exhaust before any patch: (1) native NF metrics/config, (2) structured logs → log-derived metrics/events, (3) infra metrics (cAdvisor/node exporter), (4) SBI-path capture via a proxy/sidecar or eBPF, (5) *then* a gated source patch.

**Alternatives considered.**
- *Absolute external-only.* Rejected — makes cross-NF tracing effectively impossible and silently caps Phase 3.
- *Fork freely and instrument liberally.* Rejected — destroys upstream compatibility and telemetry trust.

**Rationale.** Preserves upstream compatibility and telemetry integrity while keeping the door open for the few capabilities (tracing, some slice labels) that genuinely require it. The ICR gate ensures a patch is a deliberate, reviewed event.

**Consequences.** (+) Upstream stays trackable; patches are auditable and revertible. (+) Clear escalation ladder prevents premature forking. (−) The ladder adds friction before a patch; that friction is intentional.

---

### ADR-005 — Logging Strategy

**Status:** Accepted

**Context.** free5GC NFs log via logrus; output format is configurable. Future observability should consume **structured** logs, not parse free-form text.

**Problem.** JSON logs are ideal for machines but noisier for a human debugging Phase 1 at 2 a.m. A single format cannot be optimal for both machine ingestion and interactive human reading.

**Decision.** Establish **structured JSON logging as the machine-facing default from Phase 1**, provided it can be enabled through configuration without invasive source changes (validated per NF in M0/M2 — the mechanism is config-driven; we confirm coverage rather than assume it). Where a version/NF does not support JSON via config, that gap is logged and handled through the ADR-004 ladder (log-shipping/parse as an interim, source patch only as last resort). During interactive debugging, a **human-readable console stream** may be retained in parallel; the JSON stream is the one future collectors consume. Log level defaults to INFO in a running baseline; DEBUG is opt-in.

**Alternatives considered.**
- *Text logs + regex later.* Rejected — the handbook's own "log parsing nightmare"; brittle and version-fragile.
- *JSON only, no human console.* Rejected — hurts Phase 1 debugging ergonomics for no benefit yet.

**Rationale.** Sets up Phase 2 log ingestion cleanly while keeping Phase 1 debugging humane. Confirming the config mechanism per NF avoids over-claiming a capability we have not verified on the pinned version.

**Consequences.** (+) Phase 2 log pipeline consumes clean structured records. (+) Humans keep a readable stream. (−) Two output shapes to manage during Phase 1; trivial cost.

---

### ADR-006 — Observability Plane Architecture

**Status:** Accepted (design); implementation deferred to Phase 2+

**Context.** The plane must be modular and loosely coupled from free5GC, and its crown jewel — slice-aware correlation — depends on telemetry carrying slice identity (S-NSSAI). Its full design is in §3.

**Problem.** If we do not fix a telemetry *spine* and a *labeling contract* now, each collector will invent its own transport and slice-labeling scheme, and correlation becomes impossible after the fact.

**Decision.** Adopt **OpenTelemetry as the telemetry spine** (collection + transport standard) for metrics, logs, and traces, with Prometheus-compatible metrics scraping and Grafana as the default dashboard layer — consistent with the handbook's Phase 2 tooling. Mandate a **slice-labeling contract**: wherever technically feasible, every metric/trace/log carries the S-NSSAI (SST/SD) it pertains to. Feasibility of that contract is gated by ADR-004 and is the single biggest determinant of whether the Slice Correlation Engine (§3) can exist as designed. No component is built in Phase 1.

**Alternatives considered.**
- *Prometheus-only (metrics), bolt on logs/traces later ad hoc.* Rejected — no unified spine; traces and correlation suffer.
- *Vendor APM.* Rejected — couples us to a vendor and contradicts the open, portable thesis.

**Rationale.** One spine, one labeling contract, decided before collectors exist, is what makes §3's correlation engine feasible rather than aspirational.

**Consequences.** (+) Every future collector targets one standard and one label contract. (−) Some NFs may not be able to emit slice-labeled telemetry externally, forcing ADR-004 escalation; this risk is known now, not discovered in Phase 3 (R-02).

---

### ADR-007 — Network Slice Strategy

**Status:** Accepted

**Context.** The project exists to observe slices, yet Phase 1 prioritizes a debuggable baseline.

**Problem.** A single-slice baseline that relies on *default* S-NSSAI values makes the later "add a slice" milestone a mini-redesign (subscriber provisioning, NSSF config, AMF/SMF supported-NSSAI all change at once).

**Decision.** **Phase 1** uses a **single slice**, but that slice is **explicitly modeled** end to end: real, chosen SST/SD values configured consistently across AMF supported-NSSAI, NSSF, SMF, subscriber provisioning (MongoDB/WebUI), and UERANSIM. The single-slice baseline is what gets **frozen** at the end of Phase 1. Adding a second slice is then an **additive config change**, not a schema change, and is done in a dedicated **Phase 1.5** — *after* the Phase 1 freeze and *before* the Phase 2 K8s migration. Phase 1.5 validates NSSF slice selection and produces its own frozen multi-slice baseline. No observability work occurs in either phase.

**Alternatives considered.**
- *Multi-slice from day one.* Rejected for Phase 1 — multiplies the debugging surface before the baseline is stable.
- *Single slice via defaults.* Rejected — see Problem; turns Phase 1.5 into rework.
- *Fold the second slice into Phase 1 (freeze only once, on a multi-slice baseline).* Rejected per your refinement — freezing the single-slice baseline first gives a simpler regression oracle and a cleaner debugging boundary; the multi-slice work then has its own clean freeze.

**Rationale.** Keeps Phase 1 debugging tractable while ensuring slice-awareness is a first-class, explicitly-modeled property from the start.

**Consequences.** (+) Clean, additive path to multi-slice and then to slice observability. (−) Slightly more config discipline in Phase 1; negligible.

---

### ADR-008 — Documentation Standards

**Status:** Accepted

**Context.** The project may become a publication, an open-source project, and a portfolio piece. Documentation is a first-class deliverable (handbook's "documentation-driven development"), and "understand every NF/interface" must yield artifacts.

**Problem.** "Understanding" and "documentation" are unfalsifiable unless we define what artifacts constitute done.

**Decision.** Adopt these standards:
- **Every deliverable ships with docs** covering: Purpose, Design, Dependencies, Configuration, Testing, Limitations (handbook's list).
- **ADRs** are the only place architectural decisions are made; they are immutable once Accepted (supersede, don't edit).
- **"Understanding" produces artifacts:** an **Interface Catalog** (N1, N2, N3, N4, N6, and the SBI, each: endpoints, protocol, purpose, which NFs) and **two sequence diagrams** (UE Registration; PDU Session Establishment) — authored by us, since the handbook stops before these procedures.
- **Reproducibility docs:** a from-clean-Ubuntu runbook that a stranger can follow to the frozen baseline.
- **Diagrams as versioned source** (e.g. Mermaid/PlantUML text in `diagrams/`), not opaque images.

**Alternatives considered.** *Docs as an afterthought.* Rejected — contradicts the project's stated purpose and value.

**Rationale.** Makes "done" objective and the output portfolio/research grade.

**Consequences.** (+) Understanding is provable; onboarding is fast. (−) Documentation effort is non-trivial and scheduled explicitly in the roadmap.

---

## 3. Observability Plane Architecture (design only — not implemented in Phase 1)

Design goal: a plane that is **loosely coupled** from free5GC (free5GC does not know it is being observed, beyond emitting telemetry), organized as a pipeline from **acquisition → transport → storage → serving → intelligence**. OpenTelemetry is the spine (ADR-006).

```
free5GC NFs + UERANSIM + host/infra
        │  (metrics / logs / traces / events, slice-labeled where feasible)
        ▼
┌───────────────── ACQUISITION LAYER ─────────────────┐
│ Metrics Collector  Log Collector  Trace Collector    │
│ Event Collector                                      │
└───────────────────────┬─────────────────────────────┘
                        ▼
              TELEMETRY PIPELINE (OTel Collector)
              enrich · slice-label · batch · route
                        ▼
              ┌──────── STORAGE LAYER ────────┐
              │ metrics TSDB · log store ·     │
              │ trace store                    │
              └──────────────┬─────────────────┘
                             ▼
                        API LAYER (query/abstraction)
                             ▼
            ┌──────── SERVING / INTELLIGENCE ────────┐
            │ Dashboard Layer  Slice Correlation      │
            │                  Engine                 │
            │  Future Analytics Layer (Phase 4) ──►   │
            │  Future AI Layer (Phase 5)              │
            └─────────────────────────────────────────┘
```

**Component responsibilities and interfaces**

- **Metrics Collector.** *Why:* answer "how much / how many" over time (active UEs, PDU sessions, registration latency, per-slice counts). *Responsibility:* scrape/receive numeric time series. *Interface in:* NF-native metrics endpoints, infra exporters (cAdvisor/node), or log-derived metrics where NFs lack native ones. *Out:* OTel/Prometheus exposition to the pipeline. *Depends on:* ADR-006 labeling contract for slice dimensions.

- **Log Collector.** *Why:* answer "what happened." *Responsibility:* ship the structured JSON logs (ADR-005) from every NF. *In:* container/file log streams. *Out:* structured records to the pipeline. *Note:* consumes JSON, does not parse free-form text (that's the whole point of ADR-005).

- **Event Collector.** *Why:* logs are a firehose; **events** are the discrete, meaningful state changes (UE registered, PDU session established/released, slice selected, auth failed). *Responsibility:* distill lifecycle/state-change signals — derived from structured logs or a dedicated event stream. *In:* structured logs / NF signals. *Out:* typed events. *Distinction from Log Collector:* logs = raw stream; events = curated semantic transitions used by correlation and later analytics.

- **Trace Collector.** *Why:* answer "where did time go" across a multi-NF procedure. *Responsibility:* assemble distributed traces of a request as it crosses AMF→AUSF→UDM→… *In:* OTel spans. *Critical dependency:* free5GC does not natively emit spans — this collector's viability rests on the ADR-004 ladder (SBI proxy/eBPF, or a gated instrumentation patch). This is flagged as the highest-uncertainty component.

- **Telemetry Pipeline (OTel Collector).** *Why:* one place to enrich, **slice-label**, batch, and route all three signals. *Responsibility:* transport + processing spine. *In:* all collectors. *Out:* storage. *This is where the slice-labeling contract is enforced.*

- **Storage Layer.** *Why:* durable, queryable retention per signal type. *Responsibility:* time-series store (metrics), log store, trace store. *In:* pipeline. *Out:* API layer. Retention/cardinality policy defined at Phase 2 design.

- **API Layer.** *Why:* decouple consumers (dashboards, correlation, future AI) from storage backends. *Responsibility:* a stable query/abstraction interface so backends can change without breaking consumers. *In:* storage. *Out:* dashboards, correlation engine, analytics.

- **Dashboard Layer.** *Why:* human situational awareness. *Responsibility:* visualize slice health, session counts, latency, throughput, registration metrics. *In:* API layer. Default tool: Grafana.

- **Slice Correlation Engine.** *Why:* the project's crown jewel — join metrics + logs + traces + events **by slice** to answer "which slice is failing, and where in the chain." *Responsibility:* correlate signals across NFs keyed on S-NSSAI + session/UE identifiers. *In:* API layer / event stream. *Hard dependency:* the ADR-006 slice-labeling contract; if telemetry cannot carry S-NSSAI, this engine cannot exist as designed (R-02). Its feasibility must be proven before Phase 3 promises anything.

- **Future Analytics Layer (Phase 4).** *Why:* correlation, root-cause, capacity, SLA validation. *Responsibility:* derive insight from stored+correlated telemetry. Designed-for now, built later.

- **Future AI Layer (Phase 5).** *Why:* slice optimization, prediction, closed-loop. *Responsibility:* recommendations/automation atop analytics. Explicitly out of scope until Phase 4 exists.

**Deployment model (finalized at Phase 2, sketched now).** Because the plane is built on the K8s deployment produced by Phase 2a, its components split cleanly into two deployment shapes:

- **Per-node agents (DaemonSet-shaped):** components that must sit next to what they observe — the **Log Collector** (reads each node's container logs), **infra metric agents** feeding the **Metrics Collector**, and any eventual **eBPF/SBI-capture** feeding the **Trace Collector**. One instance per node, privileged only as narrowly as required.
- **Central services (Deployment / StatefulSet-shaped):** the **Telemetry Pipeline (OTel Collector)**, **Storage Layer** (StatefulSet — persistent volumes), **API Layer**, **Dashboard Layer**, **Event Collector**, **Slice Correlation Engine**, and future **Analytics/AI** layers. These scale horizontally and hold no node affinity.

The plane runs in its **own namespace**, loosely coupled from free5GC: free5GC emits telemetry and is otherwise unaware of the plane. Data flow is strictly one-directional (free5GC → acquisition → pipeline → storage → serving); no observability component ever writes back into the core. Exact backends, replica counts, and retention are decided at Phase 2b design, not now.

**One honest architectural warning to carry forward:** everything valuable downstream (correlation, analytics, AI) is gated by whether we can attach **slice identity** and **trace context** to telemetry at the source. That is an ADR-004 problem, and it is the difference between a real slice-observability platform and a pile of disconnected dashboards. We decide the labeling contract now (ADR-006) precisely so Phase 3 is not a discovery of impossibility.

---

## 4. Repository layout

```
5g-observability-project/
├── README.md                      # project overview, links to SPEC + ADRs
├── SPEC.md                        # this document (source of truth)
├── VERSIONS.lock                  # machine-readable pinned matrix (ADR-003)
│
├── docs/
│   ├── adr/                       # ADR-001 … ADR-008 (immutable once Accepted)
│   ├── versions.md                # human-readable version matrix + rationale
│   ├── runbooks/                  # from-clean-Ubuntu reproducible deploy runbook
│   ├── interfaces/                # Interface Catalog: N1,N2,N3,N4,N6,SBI (ADR-008)
│   ├── procedures/                # authored: registration, PDU session (ADR-008)
│   └── instrumentation/           # ICRs (ADR-004), if/when any source patch exists
│
├── diagrams/                      # versioned diagram SOURCE (Mermaid/PlantUML)
│
├── manifest.lock                  # pinned upstream URLs + exact commit SHAs (ADR-002)
│
├── external/                      # fetched upstreams — GIT-IGNORED, never committed (ADR-002)
│   ├── free5gc/                   # cloned @ pinned SHA by bootstrap.sh
│   ├── ueransim/                  # cloned @ pinned SHA
│   └── gtp5g/                     # cloned @ pinned SHA (built against pinned kernel, M0)
│
├── deployments/
│   └── compose/                   # Phase 1 Docker Compose (K8s-portability guardrails)
│       ├── docker-compose.yaml
│       ├── config/                # per-NF YAML, env, slice/S-NSSAI config
│       └── .env.example
│
├── core/                          # our wrappers/overrides for free5GC (config, not source)
├── ran/                           # our UERANSIM config/wrappers
│
├── observability/                 # Phase 2+ — STUB in Phase 1
│   └── README.md                  # "Phase 2 — not implemented; see SPEC §3"
├── k8s/                           # Phase 2+ — STUB in Phase 1
│   └── README.md                  # "Phase 2 — not implemented; see ADR-001"
│
├── scripts/                       # bootstrap.sh (fetch+verify upstreams), gtp5g build, health checks
├── tests/                         # acceptance tests (§5) as executable checks
└── .github/ (or ci/)              # CI: bootstrap integrity check, lint/validate configs; expand in Phase 2
```

Principles: our code and config never live inside `external/` (which is git-ignored and disposable); upstreams are reproduced from `manifest.lock` via `bootstrap.sh`; future-phase dirs are stubs, not empty rot; the version matrix and ADRs sit beside the deployment during the phase where reproducibility is king.

---

## 5. Phase 1 acceptance criteria (binary, testable)

Phase 1 is **not complete** until every item passes and is documented. Each is expressed so the answer is yes/no.

**Environment & reproducibility**
- [ ] `VERSIONS.lock` exists and every listed component matches the running system exactly.
- [ ] The reference machine is defined and the deploy was reproduced **from a clean Ubuntu install** following `docs/runbooks/` by someone who did not build it.
- [ ] gtp5g builds and loads against the pinned kernel (`lsmod` shows it); documented.

**Core bring-up**
- [ ] `docker compose up` brings all NFs + MongoDB to running state with **zero crash-loops** (no container restarting).
- [ ] Every control-plane NF (AMF, SMF, NRF, AUSF, UDM, UDR, PCF, NSSF) shows **successful NRF registration** in logs.
- [ ] MongoDB is reachable and provisioned with the test subscriber(s); WebUI reachable.
- [ ] All NFs emit **structured JSON logs** on the machine-facing stream (or the gap is documented per ADR-005).

**End-to-end function (the real test)**
- [ ] A UERANSIM UE completes **Registration** (Registration Accept observed).
- [ ] The UE successfully **establishes a PDU Session** (session created; UE gets an IP).
- [ ] **End-to-end user-plane connectivity** verified: traffic from the UE's tunnel interface reaches the Data Network (e.g. a successful ping through the UPF), documented.
- [ ] The **explicitly-modeled single slice** (real SST/SD) is consistent across AMF supported-NSSAI, NSSF, SMF, subscriber DB, and UERANSIM — verified, not defaulted (ADR-007).

**Understanding & documentation**
- [ ] **Interface Catalog** for N1, N2, N3, N4, N6, SBI is authored and reviewed.
- [ ] **Two sequence diagrams** (Registration, PDU Session Establishment) are authored from the running system's behavior.
- [ ] A **teardown/rebuild** cycle returns to the identical working state (proves reproducibility, not luck).

**Freeze**
- [ ] The baseline is **tagged** in Git; `VERSIONS.lock` frozen; this becomes the regression oracle for Phase 2.

---

## 6. Implementation roadmap (milestones — no code executed yet)

Each milestone: **Objective · Deliverables · Dependencies · Validation · Exit criteria · Risks.**

### M0 — Official Development Environment & version matrix
- **Objective:** eliminate reproducibility ambiguity before anything is deployed; stand up the ODE and prove the empirical pairing.
- **Deliverables:** documented **ODE** (per ADR-003); validated `VERSIONS.lock` captured from the ODE; gtp5g-vs-kernel pairing **proven** (builds + loads); initial `manifest.lock` + working `bootstrap.sh`; `docs/versions.md`; from-clean-install runbook stub.
- **Dependencies:** none (this is the root milestone).
- **Validation:** on a conforming ODE, install pinned OS/kernel/Docker; `bootstrap.sh` fetches upstreams at pinned SHAs and verifies them; build+load gtp5g against the running kernel; record exact versions into `VERSIONS.lock`.
- **Exit:** gtp5g `lsmod`-visible on the pinned kernel; `bootstrap.sh` reproducible on a wiped `external/`; matrix frozen. *(Full detail in the M0 execution brief.)*
- **Risks:** R-01 (gtp5g/kernel — the primary M0 risk; 24.04's 6.x kernel line must be proven, not assumed), R-03 (non-ODE environment temptation), R-04 (upstream fetch availability).

### M1 — Repository & documentation scaffold
- **Objective:** stand up the repo skeleton, ADRs, and stubs (this SPEC lands here).
- **Deliverables:** repo layout (§4); ADR-001…008 committed; future-phase stubs; `manifest.lock` + `bootstrap.sh`; runbook skeleton; CI that runs bootstrap, verifies upstream SHAs, and lints/validates config files.
- **Dependencies:** M0 (to pin exact SHAs in the manifest).
- **Validation:** `bootstrap.sh` on a clean checkout materializes `external/` at the pinned SHAs; CI integrity check green.
- **Exit:** repo reproducible; ADRs Accepted.
- **Risks:** upstream fetch availability (R-04).

### M2 — Core control plane up (no RAN yet)
- **Objective:** all NFs running and NRF-registered, structured logging on.
- **Deliverables:** Phase 1 compose (guardrails); per-NF config; JSON logging verified; MongoDB + WebUI; subscriber provisioned.
- **Dependencies:** M0, M1.
- **Validation:** health layers 1–4 from the handbook (containers, process, NRF registration, DB).
- **Exit:** zero crash-loops; every NF registered; JSON logs confirmed.
- **Risks:** config consistency (PLMN/TAC/SBI), R-05.

### M3 — RAN + Registration
- **Objective:** UERANSIM gNB+UE register against the core.
- **Deliverables:** UERANSIM config aligned to core (PLMN/TAC/S-NSSAI/credentials); registration achieved; **Registration sequence diagram** authored.
- **Dependencies:** M2.
- **Validation:** Registration Accept observed; interface catalog entries for N1/N2 drafted.
- **Exit:** UE reaches REGISTERED.
- **Risks:** PLMN/TAC/slice/credential mismatch (handbook's top-5 mistakes), R-06.

### M4 — PDU Session + end-to-end connectivity
- **Objective:** establish a PDU session and prove user-plane connectivity.
- **Deliverables:** PDU session established; UE gets IP; ping-through-UPF to DN documented; **PDU Session sequence diagram**; N3/N4/N6 interface catalog entries.
- **Dependencies:** M3.
- **Validation:** all "end-to-end function" acceptance items (§4) pass.
- **Exit:** verified end-to-end data path.
- **Risks:** UPF/gtp5g data-path issues, UPF address/route config (R-01 resurfaces).

### M5 — Phase 1 freeze & baseline tag (single slice)
- **Objective:** lock the reproducible *single-slice* baseline as the regression oracle.
- **Deliverables:** full §4 checklist passed and documented; teardown/rebuild proven; Git tag (e.g. `baseline-phase1`); frozen `VERSIONS.lock`.
- **Dependencies:** M0–M4.
- **Validation:** a clean rebuild reproduces the tagged state exactly.
- **Exit:** **Phase 1 complete.** The single-slice baseline is frozen and immutable.
- **Risks:** hidden nondeterminism surfacing at rebuild (R-08).

---

*The following is a distinct phase, entered only after the Phase 1 freeze.*

### Phase 1.5 — Second slice & NSSF validation
- **Objective:** prove the baseline supports multiple slices — additively — before any migration or observability work.
- **Deliverables:** a second S-NSSAI added **by config only** (no schema change, per ADR-007); NSSF slice-selection behavior validated; both slices carry UEs/sessions; interface/procedure docs updated; a **frozen multi-slice baseline** tagged (e.g. `baseline-phase1.5`).
- **Dependencies:** M5 (Phase 1 must be frozen first); ADR-007 explicit modeling done in M2–M4.
- **Validation:** each slice registers and establishes a PDU session independently; NSSF selects the correct slice per subscriber/requested S-NSSAI; clean rebuild reproduces the multi-slice tag.
- **Exit:** **Phase 1.5 complete.** Multi-slice baseline frozen; still zero observability code. Phase 2 may begin.
- **Risks:** NSSF/supported-NSSAI drift (R-07).

*(Phase 2 — sequenced as **2a: migrate frozen baseline to Kubernetes**, then **2b: introduce the §3 observability plane** — is documented but not scheduled here; it begins post-Phase-1.5. The Phase 1 / Phase 1.5 tags are the regression oracles the migration is validated against.)*

---

## 7. Risk register

| ID | Risk | Impact | Likelihood | Mitigation |
|----|------|--------|-----------|-----------|
| R-01 | gtp5g fails to build/load against pinned kernel; UPF data path broken | High | Med | Validate the kernel↔gtp5g *pair* in M0; pin as a unit; UPF is the hard part of both Phase 1 and the Phase 2 K8s port |
| R-02 | Telemetry cannot carry S-NSSAI / trace context externally → Slice Correlation Engine infeasible as designed | High | Med | Decide labeling contract now (ADR-006); prove feasibility via ADR-004 ladder before Phase 3 commits |
| R-03 | Validation done on a non-ODE environment (WSL2/VM/cloud) → GTP-U/kernel behavior diverges, reproducibility breaks | High | Low | ODE ratified in ADR-003 as the only baseline; WSL2/VM/cloud excluded; non-ODE runs are secondary targets only |
| R-04 | Upstream repo unavailable/renamed → `bootstrap.sh` fetch fails; or `external/` drifts from `manifest.lock` | Med | Low | CI integrity check fails loudly on drift; optionally cache known-good source archives of pinned SHAs |
| R-05 | Config inconsistency (PLMN/TAC/SBI) causes silent registration failure | Med | High | Single source of config truth; validate consistency in M2; handbook's mistake list as a checklist |
| R-06 | Credential/slice mismatch blocks registration | Med | Med | Provision subscriber to match UERANSIM exactly; verify in M3 |
| R-07 | Adding slice #2 drifts NSSF/supported-NSSAI | Med | Low | ADR-007 explicit modeling makes Phase 1.5 additive |
| R-08 | Hidden nondeterminism only appears on rebuild | Med | Low | M5 (and Phase 1.5) teardown/rebuild test is a hard gate, not optional |

---

## 8. What we deliberately did NOT decide yet

To keep Phase 1 focused, these are consciously deferred (documented so they are not forgotten): exact storage backends and retention/cardinality policy for the plane; K8s CNI/Multus specifics for the UPF; the precise SBI-tracing mechanism (proxy vs eBPF vs patch) — chosen in Phase 2 once the baseline exists to test against; analytics and AI specifics (Phases 4–5).

---

*End of SPEC-000 v0.1. Change this document only by adding/superseding ADRs or bumping the version with a changelog entry.*
