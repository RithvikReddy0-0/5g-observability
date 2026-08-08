# Deployment evidence — 20260808-080726Z

Captured by `scripts/collect_evidence.sh` on a **non-baseline** host (WSL2; see
`01-environment.txt`). The UPF/data path is intentionally absent — gtp5g requires a
conforming ODE (SPEC ADR-003). Registration is the success criterion here.

| Metric | Value | Source |
|---|---|---|
| NF profiles registered in NRF | 48 | MongoDB `NfProfile` |
| Subscribers provisioned | 20 | MongoDB `amData` |
| **UEs RM-REGISTERED (ground truth)** | **20** | `nr-cli … -e status` |
| UEs logging "Initial Registration is successful" | 10 | UE log |
| Prometheus targets healthy | 9 | Prometheus API |
| AMF `ue_connectivity{3GPP}` | 20 | AMF exporter (unreliable — see below) |
| AMF `ue_cm_gmm_state_count{cm-connected}` | 21 | AMF exporter (unreliable — see below) |

## Files

| File | Contents |
|---|---|
| `01-environment.txt` | host, versions, ODE conformance report |
| `02-containers.txt` | running containers + status |
| `03-nrf-registrations.txt` | every NF's NRF registration status |
| `04-subscribers.txt` | subscriber counts, PLMN, slice, SUPI list |
| `05-ue-registration.txt` | gNB NG setup, registered SUPIs, per-UE `nr-cli` state |
| `06-metrics-<nf>.txt` | raw Prometheus exposition from each NF's `:9091/metrics` |

| `07-prometheus.txt` | target health + selected dashboard queries |
| `07b-slice-metrics.txt` | **slice-labeled metrics** (sst/sd) from the slice exporter |
| `08-grafana.txt` | Grafana health, provisioned dashboards, datasources |

## Notes

- **The AMF business gauges do not match ground truth on free5GC v4.2.0.**
  `ue_connectivity` undercounts and `ue_cm_gmm_state_count{cm-connected}` overcounts; both
  leak when procedures abort (T3510/T3580) or the AMF restarts. Use the `nr-cli` figure in
  `05-ue-registration.txt` as the UE population. Counter series
  (`*_msg_received_total`, `*_sbi_inbound_request_total`) and `up` are reliable.
  See `observability/README.md`.
- PDU session establishment fails by design here (T3580) — no UPF, so
  `/nsmf-pdusession/v1/sm-contexts` returns 500. Expected on a non-baseline host.
