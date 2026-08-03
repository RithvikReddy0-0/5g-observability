# observability — Prometheus + Grafana

Scrapes free5GC's **native** metrics exporters. No source changes were required, which keeps
this at rung 1 of the ADR-004 external-instrumentation ladder.

> **Sequencing note.** SPEC §3 / ADR-001 place the observability plane in Phase 2b, *after*
> the Kubernetes migration. This stack was built earlier, on Compose, at the project owner's
> direction. The design constraints still hold: it is a separate compose project, data flow is
> one-directional (core → scrape → storage → dashboards), and nothing here writes back into
> the core, so free5GC remains unaware it is being observed.

## Running it

Bring the core up first (`deployments/compose`), then:

```bash
cd observability && docker compose up -d
```

| Service | URL | Notes |
|---|---|---|
| Prometheus | http://localhost:9090 | targets under Status → Target health |
| Grafana | http://localhost:3000 | dashboard "free5GC — control plane overview" |

Grafana runs with **anonymous Viewer access enabled** so dashboards open without credentials.
That is a local-development convenience — do not carry it into any shared or
internet-reachable deployment.

It joins the core's network (`compose_privnet`) as an *external* network, which is how it
resolves `amf.free5gc.org:9091` and friends.

## What is scraped

Eight NFs expose `:9091/metrics` (`metrics.enable: true` in each `*cfg.yaml`):
amf, smf, nrf, nssf, pcf, ausf, udm, udr.

Useful series (verified against real captures in `docs/evidence/`, not guessed):

| Metric | Meaning |
|---|---|
| `up{job="free5gc"}` | exporter reachability — the most trustworthy signal here |
| `free5gc_nas_msg_received_total{name=...}` | NAS messages by type (RegistrationRequest, RegistrationComplete, …) |
| `free5gc_ngap_msg_received_total` | NGAP messages |
| `free5gc_sbi_inbound_request_total{nf_type,path,status_code}` | SBI traffic and errors per NF |
| `free5gc_sbi_inbound_request_duration_seconds_bucket` | SBI latency histogram |
| `free5gc_amf_business_ue_connectivity` | connected UEs — **see caveat below** |
| `free5gc_amf_business_ue_gmm_state_count` | UEs per 5GMM state — **see caveat below** |

## Caveat: the AMF business gauges are not trustworthy on v4.2.0

Measured simultaneously on this deployment:

| Source | Value |
|---|---|
| `nr-cli` per-UE status (ground truth) | **20/20** RM-REGISTERED, 20/20 CM-CONNECTED |
| `free5gc_amf_business_ue_connectivity{3GPP}` | **3** (undercounts) |
| `free5gc_amf_business_ue_cm_gmm_state_count{cm-connected}` | **63** (overcounts) |

Both are wrong, in opposite directions. These gauges are adjusted on state transitions, and
aborted procedures (T3510/T3580 timeouts, failed PDU session creation) plus AMF restarts leave
them unbalanced — they leak. `ue_gmm_state_count` also accumulates despite its "current
number" HELP text.

**Consequence:** do not use these as the UE population figure. Ground truth is
`docker compose exec ueransim ./nr-cli <supi> -e status`, captured by
`scripts/collect_evidence.sh`. The counter-based series (`*_msg_received_total`,
`*_sbi_inbound_request_total`) and `up` behave correctly.

This is exactly the kind of gap the observability plane exists to expose, and it is a concrete
input to the ADR-004 decision about whether NF-native telemetry is sufficient or an
instrumentation patch is eventually warranted.

## Alerts

`prometheus/alert-rules.yml` defines NFExporterDown, NoConnectedUEs, SbiServerErrors and
SbiLatencyHigh. `SbiServerErrors` is **expected to fire** on a non-baseline host for
`/nsmf-pdusession/v1/sm-contexts` — PDU session creation cannot succeed without the UPF.

The `alert-rules.yml` ported from the previous project was an empty 0-byte file (as was its
`prometheus.yml`); these rules are newly written.
