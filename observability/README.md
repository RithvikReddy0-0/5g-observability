# observability — Phase 2+ (NOT implemented in Phase 1)

See SPEC **§3** (Observability Plane Architecture) and **ADR-006** (OpenTelemetry spine +
slice-labeling contract). No collectors, pipeline, storage, dashboards, or correlation engine
exist yet. Phase 1 deliberately ships **zero observability code** (single-slice baseline first).

`alert-rules.yml` is **ported reference** from the prior project, staged for the Phase-2
Prometheus/alerting setup. It is not active in Phase 1.
