# k8s — Phase 2 (NOT implemented in Phase 1)

See SPEC **ADR-001**. Kubernetes is the Phase-2 target; Phase 2a migrates the *frozen* Compose
baseline to K8s, then Phase 2b introduces the observability plane.

The `*.yaml` manifests here are **ported reference** from the prior `5g-devops-framework`
project (Review 2). They are **not used in Phase 1**, are not wired to the pinned versions, and
will be revisited/rewritten during Phase 2a. They are kept only so the migration starts from a
known reference rather than a blank page.
