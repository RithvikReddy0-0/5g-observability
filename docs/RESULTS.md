# Results — what was built, measured, and found

Consolidated record of the project's outcomes. Every number here was measured on a running
system and is reproducible from the raw captures in [`docs/evidence/`](evidence/).

**Environment:** WSL2 Ubuntu 24.04.4, kernel `6.6.87.2-microsoft-standard-WSL2`, 12 threads,
7.6 GiB RAM. This is a **non-baseline** host under SPEC ADR-003 — bare metal with ≥16 GB is
the ratified ODE. That distinction determines what could and could not be proven here.

---

## 1. Headline results

| Metric | Result | How verified |
|---|---|---|
| Upstreams pinned + reproducible | **PASS** | `bootstrap.sh --clean` → byte-identical SHAs |
| Control-plane NFs registered with NRF | **8 / 8** | MongoDB `NfProfile` |
| Subscribers provisioned | **20** | MongoDB `amData` |
| Network slices modeled end-to-end | **2** | config audit + NSSF selection |
| **UEs reaching REGISTERED** | **20 / 20** | `nr-cli` per-UE, ground truth |
| UEs CM-CONNECTED | **20 / 20** | `nr-cli` per-UE |
| Prometheus targets healthy | **10 / 10** | Prometheus API |
| Slice-labeled metrics | **working** | `sst`/`sd` labels queryable |
| Phase-1 acceptance (runnable subset) | **PASS=12, FAIL=0** | `tests/acceptance.sh` |
| KPI gate enforces thresholds in CI | **10 enforced, 2 ODE-only** | `make gate`, verified to reject breaches |
| PDU session / user-plane data path | **BLOCKED** | needs gtp5g — ODE only |

---

## 2. What was built

**Reproducible dependency system.** `manifest.lock` pins free5GC `v4.2.3`, UERANSIM `v3.3.0`
and gtp5g `v0.10.2` to full 40-char SHAs. `bootstrap.sh` materializes them into a git-ignored
`external/` and verifies every checkout.

**free5GC control plane on Docker Compose** — AMF, SMF, NRF, AUSF, UDM, UDR, PCF, NSSF, plus
MongoDB and the WebUI. The UPF is deliberately excluded on this host.

**RAN and 20 UEs** — one UERANSIM gNB over NGAP/SCTP, and 20 UEs split 10/10 across two
slices, each authenticating with real 5G-AKA.

**Two network slices**, modeled consistently across AMF, SMF, NSSF, gNB, UE configs and
subscriber provisioning:

| Slice | S-NSSAI | 5QI | ARP | Session AMBR | Subscribers | UE IP pool |
|---|---|---|---|---|---|---|
| A — eMBB | SST **1** / SD `010203` | 9 | 8 | 200 Mbps | `imsi-…001`–`010` | `10.60.0.0/16` |
| B — URLLC | SST **2** / SD `112233` | 82 | 2 | 20 Mbps | `imsi-…011`–`020` | `10.61.0.0/16` |

Both slices are defined once in [`deployments/slices.env`](../deployments/slices.env), which
provisioning and the orchestrator both read, so they cannot drift apart. Slice B was
originally SST 1 as well — two labels over one service. It was changed to SST 2 with genuinely
different QoS when it became clear that identical service types are not two slices in any
meaningful sense. Evidence bundles captured before that change still show `sst="1"` for
SD `112233`; that is the older state, not a discrepancy.

**Observability plane** — Prometheus + Grafana as a separate compose project attached to the
core's network, so free5GC stays unaware it is observed and data flows one way only.

**Slice-labeled metrics exporter** — closes the gap described in §4.

**Automation** — provisioning, UE launch, evidence collection, acceptance tests, CI gate.

---

## 3. Measured results

### 3.1 Bootstrap reproducibility

```
--clean re-clone:                   exit=0, 262s, BOOTSTRAP: PASS
BEFORE vs AFTER SHA comparison:     IDENTICAL (18/18 lines)
--verify-only after clean rebuild:  BOOTSTRAP: PASS
drift injected (gtp5g HEAD~1):      [FAIL] + BOOTSTRAP: FAIL  ← gate works
```

The 18 lines are 3 top-level pins **plus all 15 free5GC submodule SHAs** — reproducibility
proven at submodule granularity, not just top level.

### 3.2 NF registration

```
AF  AMF  AUSF  NSSF  PCF  SMF  UDM  UDR      (8 profiles, no duplicates)
```

### 3.3 UE registration

```
slice A (sd 010203): 10 / 10
slice B (sd 112233): 10 / 10
RM-REGISTERED : 20 / 20
CM-CONNECTED  : 20 / 20
```

Per-UE state via `nr-cli` — e.g. `rm-state: RM-REGISTERED`,
`mm-state: MM-REGISTERED/NORMAL-SERVICE`, `5u-state: 5U1-UPDATED`.

### 3.4 NSSF slice selection

Proof both slices are selected **independently**, not just configured:

```
60 requests carrying  sd=010203
60 requests carrying  sd=112233
```

### 3.5 Slice-labeled metrics

```
free5gc_slice_provisioned_subscribers{sst="1",sd="010203"} 10
free5gc_slice_provisioned_subscribers{sst="1",sd="112233"} 10
free5gc_slice_smf_selection_total{sst="1",sd="010203",dnn="internet"} 569
```

### 3.6 Acceptance

```
PASS=12  FAIL=0  SKIP-ODE=7  GAP=1
RESULT: PASS (non-baseline) — every runnable criterion passed.
        Phase 1 is NOT complete: 7 criteria require the ODE, 1 documented gap.
```

### 3.7 KPI gate

Twelve KPIs are declared in [`deployments/kpi-gates.json`](../deployments/kpi-gates.json) and
enforced by `make gate`. Ten are runnable on this host; the two user-plane KPIs report
`SKIP-ODE` rather than passing.

The gate's own behaviour was verified against a stub Prometheus returning known values —
because a gate that has never been observed rejecting anything is indistinguishable from one
that always passes:

| Scenario | Expected | Observed |
|---|---|---|
| All KPIs conforming | exit 0, `GATE PASSED` | exit 0 |
| 2 NFs unscrapeable, 5xx at 1.7 req/s | exit 1, `GATE FAILED` | exit 1, 2 gates failed |
| Slice at 0.99 utilisation (advisory) | `WARN`, build not failed | `WARN`, build not failed |
| Prometheus unreachable | exit 2 | exit 2 |

The same four checks run in CI on every push, using
[`tools/kpi-gate/stub_prometheus.py`](../tools/kpi-gate/stub_prometheus.py). Detail in
[`docs/kpi-gate.md`](kpi-gate.md).

---

## 4. Key findings

### 4.1 free5GC's native metrics carry NO slice identity — risk R-02 confirmed

Checked across **all 629 exposed series and 15 metric names**. The complete set of label keys:

```
gmm_state · le · method · name · nf_type · path · state · status · status_code
target_service_name · to_state
```

None identify a slice. This blocks the ADR-006 slice-labeling contract that the Slice
Correlation Engine in SPEC §3 depends on — the project's crown-jewel feature.

**Resolved** at ADR-004 rung 2 without patching upstream. Slice identity is available in AMF
logs at INFO level:

```
[AMF][Gmm][supi:SUPI:imsi-208930000000004] Select SMF [snssai: {Sst:1 Sd:010203}, dnn: internet]
```

`observability/slice-exporter` parses this and joins it with MongoDB to emit real
`sst`/`sd`-labeled metrics. These are **derived**, not NF-native — the upstream gap is
unchanged, and that is stated plainly wherever the metrics appear.

### 4.2 The AMF's own UE gauges leak under churn

| | after churn | after a clean cycle |
|---|---|---|
| ground truth (`nr-cli`) | 20/20 | 20/20 |
| `ue_connectivity{3GPP}` | **3** | **20** ✅ |
| `ue_cm_gmm_state_count{cm-connected}` | **63** | **20** ✅ |

Accurate immediately after a clean bring-up; drifts once transitions are lost to aborted
procedures or AMF restarts, and never self-corrects. Wrong in *both* directions. Ground truth
for UE population is `nr-cli`, not these gauges.

### 4.3 Three config faults blocked registration — each with a misleading symptom

1. **SQN too high.** free5GC's stock `16f3b3f70fc2` is far beyond the 5G-AKA acceptance window
   for a fresh UE (`SQN-MS` starts at 0). Every UE rejected it and AUTS re-sync also failed.
   *Looked exactly like a wrong key — the keys were correct throughout.*
2. **NSSF missing the serving TAI.** The TAC must be the quoted 6-hex-digit string `"000001"`;
   an integer `1` silently fails to match and produces the identical error.
3. **UE requesting an unprovisioned slice.** A partially-unsubscribed requested NSSAI makes
   the AMF attempt an AMF re-selection and fail with
   `AMF can not select an target AMF by NRF`.

Finding 2 was originally recorded in the M0 audit as *"harmless for the basic single-slice
path."* That was **wrong** — it was a hard blocker. The report was corrected rather than
quietly fixed.

### 4.4 Structured JSON logging is impossible by configuration

free5GC v4.2.3's logger config exposes exactly three fields
(`NFs/amf/pkg/factory/config.go:114`): `Enable`, `Level`, `ReportCaller`. No `format` key
exists in any NF; the formatter is fixed inside the external `free5gc/util/logger` module.

Recorded as a documented gap under ADR-005's own escape clause — no upstream patch. See
[`docs/logging.md`](logging.md).

### 4.5 The prior project's "slices" were not 5G slices

In `5g-devops-framework`, `ue_client.py` sent plain HTTP to a Flask app which looked the type
up in a Python dictionary and incremented a counter labeled `slice_type`. No PDU session, no
S-NSSAI, no UPF — free5GC and the traffic simulator never interacted. The slice work in this
project is real 5G slice selection performed by the NSSF.

---

## 5. What is NOT done, and why

**Seven acceptance criteria are blocked on hardware.** The UPF requires the gtp5g kernel
module, which needs a standard Linux kernel; WSL2's kernel cannot load it.

| Blocked item | Consequence |
|---|---|
| gtp5g build + load | M0 cannot be signed off |
| PDU session establishment | no UE IP address |
| End-to-end user-plane connectivity | no ping through the UPF |
| `VERSIONS.lock` environment freeze | kernel/Docker values unrecorded |
| PDU-session diagram from real logs | its data-path half is marked DESIGN-ONLY |
| `down -v` teardown/rebuild proof | only the warm stop/start cycle is proven |
| **Baseline tag (M5 freeze)** | **no regression oracle for Phase 2** |

Plus one accepted gap: structured JSON logging (§4.4).

**KPI enforcement is half-closed.** The gate now detects a failing deployment and stops the
pipeline (§3.7), but nothing yet reverts to the last known-good deployment. The loop is
closed on detection, not on remediation.

**Phase 1 is therefore not complete and has not been frozen.** `tests/acceptance.sh` enforces
this — it refuses to print "Phase 1 acceptance complete" while any `SKIP-ODE` remains.

---

## 6. Where the evidence lives

[`docs/evidence/`](evidence/) holds timestamped bundles, each with environment + ODE
conformance, container status, NRF registrations, subscriber counts and SUPIs, per-UE
`nr-cli` state, raw Prometheus exposition from all 8 NFs, target health, and slice metrics.

Most complete bundle: **`run-20260808-081135Z`**.

Regenerate any time with `bash scripts/collect_evidence.sh`.

---

## 7. Reproducing these results

```bash
bash scripts/verify_env.sh                 # environment conformance
bash scripts/bootstrap.sh                  # fetch upstreams at pinned SHAs
# bring up the core (see docs/runbooks/clean-install.md)
bash scripts/provision_subscribers.sh --delete-all
COUNT=10 START=1  SD=010203 bash scripts/provision_subscribers.sh
COUNT=10 START=11 SD=112233 bash scripts/provision_subscribers.sh
COUNT=10 COUNT_B=10 TEMPO=600 bash scripts/start_ues.sh
bash scripts/start_slice_exporter.sh
bash tests/acceptance.sh
bash scripts/collect_evidence.sh
```

**Always re-provision subscribers immediately before launching UEs** — SQN advances on every
authentication while a restarted `nr-ue` restarts at 0, and the resulting desync looks like a
wrong key.
