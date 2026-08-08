# ADR-009 — Phase re-sequencing: observability before the Kubernetes migration

**Status:** Accepted

**Supersedes:** the *sequencing* clause of ADR-001 only. ADR-001's decision that Phase 1 is
Compose-only, and its K8s-portability guardrails, remain in force. Nothing else changes.

---

## Context

SPEC-000 sequences the work as: Phase 1 (single-slice baseline) → **freeze** → Phase 1.5
(second slice) → Phase 2a (migrate the frozen baseline to Kubernetes) → Phase 2b (introduce
the observability plane). The stated rationale is *"never debug three unknowns at once."*

Work actually proceeded differently. On a non-baseline WSL2 host the project owner directed
that the observability plane be built on **Compose**, before any Kubernetes migration, and
that Phase 1.5's second slice be added before the Phase 1 freeze.

This ADR records that decision so the deviation is explicit rather than implied by commit
history.

## Problem

The ODE-blocking constraint is real and not going away in the short term: the UPF requires
the gtp5g kernel module, which WSL2 cannot provide. That makes **Phase 1 impossible to
complete or freeze** on the available machine — M4 (PDU session, end-to-end connectivity)
and M5 (freeze, tag) are unreachable.

Following the SPEC order strictly would therefore mean doing *nothing further* until
bare-metal hardware is available. The alternative is to bring forward work that does not
depend on the user plane.

## Decision

Re-sequence as follows, for this project only:

1. Build the observability plane on **Compose**, not on Kubernetes, and do it **before**
   Phase 2a.
2. Add the Phase 1.5 second slice **before** the Phase 1 freeze rather than after it.
3. Keep Phase 1's acceptance criteria unchanged. Phase 1 is **not** declared complete; the
   criteria that need the ODE are tracked as explicitly unmet (`tests/acceptance.sh` reports
   them as `SKIP-ODE` and refuses to report Phase 1 complete on a non-baseline host).

The observability plane must still honour the SPEC §3 design constraints that do not depend
on the platform, and does:

- it is a **separate compose project**, so free5GC remains unaware it is observed;
- data flow is **one-directional** (core → scrape → storage → dashboards); nothing writes
  back into the core;
- it consumes NF-native telemetry first (ADR-004 rung 1), escalating only to log-derived
  metrics (rung 2) where native telemetry is insufficient.

## Alternatives considered

- **Wait for the ODE.** Rejected: it stalls all progress indefinitely on a hardware
  dependency, and the observability findings below are more valuable discovered early.
- **Migrate to Kubernetes first, as written.** Rejected: the UPF is the expensive part of
  the K8s port (Multus, privileged pods, per-node kernel module) and it is exactly the part
  that cannot be validated on this host. Migrating an unverifiable user plane is worse than
  not migrating.
- **Declare Phase 1 complete without the user plane.** Rejected outright — it would make the
  frozen baseline a false regression oracle, which is the one thing the freeze exists to
  prevent.

## Rationale

The two hard problems ADR-001 wanted kept sequential — *"get 5G working"* and *"get 5G
working on K8s"* — are still sequential. What moved is instrumentation, which is additive,
externally coupled, and does not alter the core's behaviour.

Doing observability early also produced a finding that materially affects Phase 3 and would
otherwise have surfaced far later: **free5GC's native metrics carry no slice identity at
all** (verified across all 629 series and 15 metric names). That is risk **R-02** confirmed
empirically rather than theoretically, and it is the single biggest determinant of whether
the Slice Correlation Engine in SPEC §3 can exist as designed. Discovering this before
committing to Phase 3 is worth more than the ordering it cost.

## Consequences

- (+) Progress continues on non-baseline hardware; the slice-labeling problem is understood
  now, with a working rung-2 mitigation, rather than at Phase 3.
- (+) Two slices exist and are validated, so slice-labeled telemetry has something real to
  distinguish.
- (−) **The Phase 1 freeze never happened**, so there is no tagged regression oracle. The
  Phase 2a migration will have to be validated against a baseline that is frozen *later*, on
  the ODE. This is a genuine loss of safety and is accepted knowingly.
- (−) The observability stack is built against Compose networking (`compose_privnet`, a host
  gateway address for the slice exporter). Porting it to Kubernetes is now additional Phase
  2b work that the original ordering would have avoided.
- (−) The dashboards and exporter target a deployment whose user plane has never run, so
  every data-path metric is unexercised and unverified.

## Follow-up required on the ODE

1. Complete M4, then freeze and tag the Phase 1 baseline (M5), then re-freeze Phase 1.5.
2. Re-author `diagrams/pdu-session-establishment.mmd` from real logs — its data-path half is
   marked DESIGN-ONLY.
3. Re-run `tests/acceptance.sh` and confirm every `SKIP-ODE` item converts to `PASS`.
4. Re-validate the observability plane against the data path, including the per-slice
   traffic metrics that cannot exist today.
