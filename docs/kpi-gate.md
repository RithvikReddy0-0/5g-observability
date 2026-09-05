# KPI gate — turning observability into enforcement

Everything built before this point *showed* whether a deployment worked. A human still had
to look at a dashboard and decide. The KPI gate makes that decision automatically, and makes
it blocking.

```bash
make gate          # run it against the live stack
make gate-test     # prove the gate can fail, without needing a stack
```

| | |
|---|---|
| Definitions | [`deployments/kpi-gates.json`](../deployments/kpi-gates.json) |
| Implementation | [`tools/kpi-gate/kpi_gate.py`](../tools/kpi-gate/kpi_gate.py) |
| Pipeline entry point | [`ci/scripts/collect-kpis.sh`](../ci/scripts/collect-kpis.sh) |
| CI job | `kpi-gate` in [`.github/workflows/integrity.yml`](../.github/workflows/integrity.yml) |

## Why it exists

Objective 5 of the project asked for continuous observability; objective 8 asks for KPI
validation *inside the pipeline*. The difference matters. A pipeline that deploys a 5G core
and reports success because the containers started is asserting something it never checked.
Under this gate a deployment is accepted only if the network's own telemetry says it works.

## What is enforced

Twelve KPIs are declared, ten of which are runnable on a host without the user plane.

| KPI | Threshold | Severity |
|---|---|---|
| All 8 control-plane NFs are scrapeable | `>= 8` | gate |
| No configured target is down | `== 0` | gate |
| Expected UE population is registered | `>= 20` | gate |
| Both slices have registered UEs | `>= 2` | gate |
| Subscriber records exist for both slices | `>= 20` | gate |
| SBI server-error rate | `<= 0.2 req/s` | gate |
| SBI p95 latency | `<= 1 s` | gate |
| Both slices publish capacity | `>= 2` | gate |
| No slice is saturated | `<= 0.95` | advisory |
| No demand refused for want of a low-latency slice | `== 0` | advisory |
| PDU session establishment succeeds | `>= 0.99` | **gate, ODE-only** |
| Per-slice throughput matches provisioned AMBR | `>= 1 Mbps` | **gate, ODE-only** |

Each definition carries a `why` field explaining what failure it is meant to catch. A
threshold without a rationale is a number somebody guessed, so the self-test warns when one
is missing.

## Three decisions that make it a gate rather than a placebo

**Missing data fails.** If a query returns nothing, the KPI has *not* been met — it has not
been measured. The easy alternative, treating an empty result as "no problem found", is
exactly how a gate ends up passing a deployment where the exporter itself has died.

**ODE-only KPIs are skipped, never passed.** The two user-plane KPIs need gtp5g, which
cannot load on this host (R-01). They are reported `SKIP-ODE` in the same vocabulary as
[`tests/acceptance.sh`](../tests/acceptance.sh). A green run here does not mean those KPIs
were met, and the summary line says so explicitly.

**An unreachable Prometheus is exit 2, not exit 0.** A gate that cannot run has not passed.

## Which metrics are trusted, and which are not

The gate deliberately does **not** use `free5gc_amf_business_ue_connectivity` or
`free5gc_amf_business_ue_cm_gmm_state_count`, even though they look like the obvious source
for "how many UEs are attached".

Measured against `nr-cli` ground truth at the same instant, with 20/20 UEs genuinely
registered, those gauges read **3** and **63** — wrong in opposite directions. They are
adjusted on state transitions and leak whenever a procedure aborts or the AMF restarts.

UE population is therefore taken from `slice_ues_observed_registered`, which the slice
exporter derives from AMF logs. Counter series (`*_total`) and `up` are reliable and are
used freely. This is a concrete instance of the ADR-004 question of whether NF-native
telemetry is sufficient: here, it is not.

## Verification

The gate's own correctness is tested, because a gate nobody has watched reject anything is
indistinguishable from one that always passes.

`--self-test` drives the decision logic over ten known inputs — each operator satisfied and
breached, missing data, advisory downgrade, and ODE skip — and validates the definitions
file. It needs no 5G core, so CI runs it on every push.

The HTTP path was additionally exercised against a stub Prometheus returning known values:

| Scenario | Expected | Observed |
|---|---|---|
| All KPIs conforming | exit 0, `GATE PASSED` | exit 0 |
| Two NFs down, 5xx at 1.7 req/s | exit 1, `GATE FAILED` | exit 1, 2 gates failed |
| Slice at 0.99 utilisation | advisory `WARN`, does not fail the build | `WARN`, build not failed |
| Prometheus unreachable | exit 2 | exit 2 |

## Changing a threshold

Edit `deployments/kpi-gates.json` only. Nothing reads a threshold from anywhere else — the
gate, the CI job and the evidence snapshot all load that one file, so they cannot disagree.

Adding a KPI needs `id`, `title`, `expr`, `op`, `threshold`, `severity` and `why`; the CI job
rejects a definition missing any of them, a duplicate id, or an unknown operator.

## What is still missing

Automatic **rollback** on a failed gate. At present a failure stops the pipeline and reports
why; it does not yet revert to the previous known-good deployment. That is the next step, and
it is honest to say the loop is closed on detection but not yet on remediation.
