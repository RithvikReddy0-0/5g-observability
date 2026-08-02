# deployments/compose — Phase-1 Docker Compose baseline

Ported from the upstream `free5gc-compose` deployment (via the prior `5g-devops-framework`
project). `config/` holds the per-NF free5GC configuration (`*cfg.yaml`). Brought up in **M2**
(not part of M0) under K8s-portability guardrails (ADR-001): DNS/service names not container
IPs, tunables in env/config.

## Ported now
- `docker-compose.yaml` — the free5gc-compose stack (references `free5gc/*:v4.2.0` images).
- `config/*cfg.yaml` — AMF, SMF, NRF, AUSF, UDM, UDR, PCF, NSSF, CHF, NEF, TNGF, N3IWF, UPF, WebUI, plus UE/gNB test configs.

## NOT ported (needed before M2 bring-up — flagged, not silently added)
The ported `docker-compose.yaml` also references files that were outside the `*cfg.yaml` port scope:
- `cert/` (per-NF TLS certs) — provided by free5GC bootstrap / generated at M2.
- `config/uerouting.yaml` (SMF UE routing), `config/upf-iptables.sh` (UPF), `config/prometheus.yml`.
These must be supplied/reconciled at M2. Source is pinned to **v4.2.3** while images are **v4.2.0**
(see `VERSIONS.lock` / M0 report) — reconcile at M2.
