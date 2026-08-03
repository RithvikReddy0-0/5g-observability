# Deployment evidence — 20260803-054820Z

Captured by `scripts/collect_evidence.sh` on a **non-baseline** host (WSL2; see
`01-environment.txt`). The UPF/data path is intentionally absent — gtp5g requires a
conforming ODE (SPEC ADR-003). Registration is the success criterion here.

| Metric | Value |
|---|---|
| NF profiles registered in NRF | 7 |
| Subscribers provisioned | 20 |
| UEs reaching REGISTERED | 20 |
| AMF `ue_cm_gmm_state_count{cm-connected}` | 63 |

## Files

| File | Contents |
|---|---|
| `01-environment.txt` | host, versions, ODE conformance report |
| `02-containers.txt` | running containers + status |
| `03-nrf-registrations.txt` | every NF's NRF registration status |
| `04-subscribers.txt` | subscriber counts, PLMN, slice, SUPI list |
| `05-ue-registration.txt` | gNB NG setup, registered SUPIs, per-UE `nr-cli` state |
| `06-metrics-<nf>.txt` | raw Prometheus exposition from each NF's `:9091/metrics` |

## Notes

- `cm-connected` may exceed the UE count if the AMF holds stale contexts from earlier
  attempts; restart the AMF and gNB before capturing for a clean figure.
- PDU session establishment fails by design here (T3580) — no UPF.
