# Screenshots

PNG captures of the live system. Regenerate any time with:

```bash
bash scripts/capture_screenshots.sh
```

They are rendered by **headless Chromium in a throwaway container** attached to the core's
compose network — no desktop session, X server or host browser needed. Grafana runs with
anonymous viewer access, so no credentials are involved.

The text captures in [`../evidence/`](../evidence/) remain the authoritative record; every
figure here has a raw-text counterpart, so no claim rests on an image alone.

## Captured figures

| File | Shows | Key numbers |
|---|---|---|
| `01-grafana-dashboard.png` | full control-plane dashboard | NF exporters **8**, UEs connected **20**, active PDU sessions **0**, registration requests **20** |
| `02-prometheus-targets.png` | Prometheus target health | free5gc **8/8 UP**, prometheus **1/1**, slice-exporter **1/1** = 10 |
| `03-prometheus-slice-metrics.png` | **the R-02 result** | `free5gc_slice_provisioned_subscribers{sd="010203"}` = 10, `{sd="112233"}` = 10 |
| `04-grafana-slice-row.png` | per-slice panel | subscribers per S-NSSAI |
| `05-prometheus-nf-up.png` | `up{job="free5gc"}` | all 8 NF exporters reporting 1 |
| `06-webui-login.png` | free5GC WebUI | service reachable on :5000 |

## Two figures look "wrong" — both are correct and documented

- **Active PDU sessions = 0.** There is no UPF on this host: it needs the gtp5g kernel
  module, which WSL2 cannot load. The control plane is fully working; only the user plane is
  absent. See [`../RESULTS.md`](../RESULTS.md) §5.
- **UEs observed registered, per slice** may read 0 while 20 UEs are genuinely registered.
  That metric is an *activity* signal derived from a log window — idle UEs stop emitting log
  lines and it decays. Population comes from `free5gc_slice_provisioned_subscribers` and
  `nr-cli`. See [`../../observability/README.md`](../../observability/README.md).

## Terminal output worth capturing yourself

These are commands rather than pages, so they are not auto-rendered:

```bash
bash tests/acceptance.sh                 # PASS=12 FAIL=0 SKIP-ODE=7 GAP=1
bash scripts/bootstrap.sh --verify-only  # BOOTSTRAP: PASS
docker exec -i ueransim ./nr-cli imsi-208930000000001 -e status   # slice A UE
docker exec -i ueransim ./nr-cli imsi-208930000000011 -e status   # slice B UE
docker logs --since 10m nssf 2>&1 | grep -oE 'sd%22%3A%22[0-9]+%22' | sort | uniq -c
```

The last one is the Phase 1.5 proof — NSSF selecting **both** slices independently.
