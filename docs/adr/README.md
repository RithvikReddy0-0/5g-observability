# docs/adr — Architecture Decision Records

ADR-001 … ADR-008 are ratified **inside [`SPEC.md`](../../SPEC.md) §2** (Accepted at inception,
immutable — supersede, don't edit). This directory holds any **future superseding ADRs**
(ADR-009+) as individual files.

| ADR | Decision |
|---|---|
| [009](ADR-009-phase-resequencing.md) | Phase re-sequencing (observability before the Phase 1 freeze) |
| [010](ADR-010-open5gs-core.md) | Open5GS as a second core with a working user plane |
| [011](ADR-011-dedicated-user-plane-per-slice.md) | A gNB, UPF and data network per slice |
| [012](ADR-012-ueransim-udp-buffers.md) | UERANSIM UDP buffers and delivered capacity |
| [013](ADR-013-kubernetes-open5gs.md) | Open5GS on Kubernetes, manifests generated from Compose |
| [014](ADR-014-mmtc-slice-and-100-ues.md) | mMTC slice and 100 UEs |
| [015](ADR-015-traffic-profiles-and-kpi-scorecard.md) | Traffic profiles and the KPI scorecard |
| [016](ADR-016-dynamic-slice-allocation.md) | Dynamic allocation: ARP priority, feedback control, planning |
| [017](ADR-017-network-orchestrator.md) | Network orchestrator: slice lifecycle |
| [018](ADR-018-algorithm-comparison.md) | Algorithms compared; the winners made default |
| [019](ADR-019-packet-scheduler.md) | Packet scheduler: classify every radio packet, schedule slices on a shared cell |
| [020](ADR-020-multi-session-ues.md) | Multi-session UEs: a session on every permitted slice |
