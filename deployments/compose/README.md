# deployments/compose — Phase-1 Docker Compose baseline

Ported from the upstream `free5gc-compose` deployment (via the prior `5g-devops-framework`
project). `config/` holds the per-NF free5GC configuration (`*cfg.yaml`). Brought up in **M2**
(not part of M0) under K8s-portability guardrails (ADR-001): DNS/service names not container
IPs, tunables in env/config.

## Ported now
- `docker-compose.yaml` — the free5gc-compose stack (references `free5gc/*:v4.2.0` images).
- `config/*cfg.yaml` — AMF, SMF, NRF, AUSF, UDM, UDR, PCF, NSSF, CHF, NEF, TNGF, N3IWF, UPF, WebUI, plus UE/gNB test configs.

## Control-plane bring-up (works on a non-baseline machine, e.g. WSL2)

The UPF needs the gtp5g kernel module, so on WSL2 the control plane is brought up
**without** UPF / N3IWF / TNGF. `free5gc-smf` declares `depends_on: free5gc-upf`, so it
must be started with `--no-deps` or Compose will drag the UPF in.

```bash
docker compose up -d db free5gc-nrf
docker compose up -d free5gc-amf free5gc-ausf free5gc-udm free5gc-udr \
                     free5gc-pcf free5gc-nssf free5gc-webui
docker compose up -d --no-deps free5gc-smf
```

Verify every NF registered with the NRF (the NRF's SBI API enforces OAuth2, so read the
registry from MongoDB instead):

```bash
docker compose exec -T db mongo free5gc --quiet --eval "db.NfProfile.find({},{_id:0,nfType:1,nfStatus:1}).toArray()"
```

Expect `REGISTERED` for AMF, SMF, AUSF, UDM, UDR, PCF, NSSF (plus AF from the WebUI).
TLS certs are **auto-generated** by free5GC on first start into `cert/` — that directory is
git-ignored (it contains private keys) and regenerates on a clean run.

WebUI (subscriber provisioning) is published on `http://localhost:5000`.

## NOT ported (needed before M2 bring-up — flagged, not silently added)
The ported `docker-compose.yaml` also references files that were outside the `*cfg.yaml` port scope:
- `cert/` (per-NF TLS certs) — provided by free5GC bootstrap / generated at M2.
- `config/uerouting.yaml` (SMF UE routing), `config/upf-iptables.sh` (UPF), `config/prometheus.yml`.
These must be supplied/reconciled at M2. Source is pinned to **v4.2.3** while images are **v4.2.0**
(see `VERSIONS.lock` / M0 report) — reconcile at M2.
