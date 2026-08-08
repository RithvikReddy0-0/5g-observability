# Screenshots

Drop image captures in this directory. The text evidence in [`../evidence/`](../evidence/) is
the authoritative record — screenshots are for the report/presentation, and every figure
below has a raw-text counterpart so nothing rests on an image alone.

Save as `NN-short-name.png` so ordering is stable.

## What to capture

### 01 — Grafana dashboard
`http://localhost:3000` → **free5GC — control plane overview**
(anonymous viewer is enabled, so no login is needed)

Shows NF exporters up, UE state distribution, NAS message rates, SBI traffic and errors, and
the per-slice row.

> Two panels will look "wrong" and that is expected and documented:
> **Active PDU sessions = 0** (no UPF on this host), and
> **UEs observed registered** may read 0 once UEs go idle — it is an activity signal, not a
> population count. See `observability/README.md`.

### 02 — Prometheus targets
`http://localhost:9090/targets` — expect **10 UP** (8 NFs + Prometheus + slice-exporter).

### 03 — Slice-labeled metrics
`http://localhost:9105/metrics` or in Prometheus query `free5gc_slice_provisioned_subscribers`.
This is the R-02 result: metrics carrying `sst` / `sd` labels, which free5GC cannot produce
natively.

### 04 — free5GC WebUI subscribers
`http://localhost:5000` (login `admin` / `free5gc`) → Subscribers.
Expect 20 across two slices.

### 05 — UE registration
```bash
bash scripts/start_ues.sh --status
docker exec -i ueransim ./nr-cli imsi-208930000000001 -e status
docker exec -i ueransim ./nr-cli imsi-208930000000011 -e status
```
One UE from each slice; expect `RM-REGISTERED` / `MM-REGISTERED/NORMAL-SERVICE`.

### 06 — Acceptance test run
```bash
bash tests/acceptance.sh
```
Expect `PASS=12  FAIL=0  SKIP-ODE=7  GAP=1`. Worth capturing in full — it shows honestly
which criteria still need the ODE.

### 07 — Bootstrap determinism
```bash
bash scripts/bootstrap.sh --verify-only
```
Expect `BOOTSTRAP: PASS`.

### 08 — NSSF selecting both slices
```bash
docker logs --since 10m nssf 2>&1 | grep -oE 'sd%22%3A%22[0-9]+%22' | sort | uniq -c
```
Expect counts against **both** `010203` and `112233` — the Phase 1.5 proof.

## Getting the stack running first

See [`../runbooks/clean-install.md`](../runbooks/clean-install.md). For a clean set of
figures, re-provision subscribers and relaunch the UEs immediately before capturing — the AMF
gauges only match ground truth right after a clean cycle.
