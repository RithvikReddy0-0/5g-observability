# ci — CI/CD (ported reference)

GitHub Actions (`github-actions/`), Jenkins (`jenkins/`), and helper `scripts/` ported from the
prior `5g-devops-framework` project as reference. The **Phase-1 CI contract** (SPEC / brief) is
narrower and wired in **M1**: run `scripts/bootstrap.sh --verify-only` as an integrity gate that
fails on upstream SHA drift, plus config/lint validation. Treat the ported pipelines as a starting
point, not the Phase-1 pipeline.

## KPI gate (objective 8)

`scripts/collect-kpis.sh` was a 0-byte stub in the ported tree. It is now the pipeline entry
point for KPI validation: it delegates to `tools/kpi-gate/kpi_gate.py`, which evaluates
`deployments/kpi-gates.json` against live Prometheus and exits non-zero when a threshold is
breached, so a failing deployment stops the pipeline instead of being reported green.

```bash
make gate         # verdict for the running deployment
make gate-test    # prove the gate can fail, no stack needed
```

The workflow job `kpi-gate` runs the gate against a stub Prometheus in both a healthy and a
broken configuration, and fails the build if the gate accepts the broken one. See
[`docs/kpi-gate.md`](../docs/kpi-gate.md).
