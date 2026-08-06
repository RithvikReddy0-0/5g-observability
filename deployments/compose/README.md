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

## Subscriber provisioning

```bash
COUNT=20 scripts/provision_subscribers.sh   # provision imsi-2089300000000NN
scripts/provision_subscribers.sh --list     # show what exists
scripts/provision_subscribers.sh --delete-all
```

Goes through the WebUI REST API so free5GC owns the document schema (it stores the
credentials as `encPermanentKey` / `encOpcKey`, which is **not** the shape the POST body
uses — writing MongoDB by hand gets this wrong). Slice/credentials default to the values in
`ran/config/free5gc-ue.yaml`; GPSI is derived per-subscriber because free5GC rejects
duplicates with `{"cause":"duplicate gpsi"}`.

## RAN: gNB + UEs

```bash
docker compose up -d --no-deps ueransim   # nr-gnb; expect "NG Setup procedure is successful"
COUNT=20 scripts/start_ues.sh             # launch UEs, report how many REGISTERED
scripts/start_ues.sh --status
scripts/start_ues.sh --stop
```

UEs run inside the `ueransim` container (radio-link sim on 127.0.0.1) rather than as 20
separate containers. Verify from the RAN side with
`docker compose exec ueransim ./nr-cli imsi-208930000000001 -e status` — expect
`mm-state: MM-REGISTERED/NORMAL-SERVICE`.

**PDU session establishment fails here by design** (T3580 retransmit): the UPF needs the
gtp5g kernel module, which WSL2 does not provide. Registration is the success criterion on
a non-baseline host; the data path is validated on the ODE.

## Phase 1.5 — two slices

Two explicitly-modeled slices, added **additively** (config only, no schema change) as
ADR-007 requires:

| Slice | S-NSSAI | Subscribers | UE config |
|---|---|---|---|
| A | SST 1 / SD `010203` | `imsi-2089300000000 01..10` | `config/uecfg.yaml` |
| B | SST 1 / SD `112233` | `imsi-2089300000000 11..20` | `config/uecfg-slice-b.yaml` |

```bash
scripts/provision_subscribers.sh --delete-all
COUNT=10 START=1  SD=010203 scripts/provision_subscribers.sh
COUNT=10 START=11 SD=112233 scripts/provision_subscribers.sh
COUNT=10 COUNT_B=10 TEMPO=600 scripts/start_ues.sh
```

Validated: slice B **10/10 registered**, and NSSF performed slice selection independently
for each — 60 requests carrying `sd=010203` and 60 carrying `sd=112233`. Both slices appear
in `free5gc_slice_provisioned_subscribers`.

**Gotcha:** configs are bind-mounted **file by file**, not as a directory. A new config is
invisible inside the container until it is listed explicitly in `docker-compose.yaml`, and
`nr-ue` fails with `ERROR: bad file: ./config/uecfg-slice-b.yaml`.

### Three config fixes that registration depends on

Registration failed until all three were corrected — each produced a misleading symptom:

1. **Subscriber SQN** — free5GC's stock `16f3b3f70fc2` is far beyond the 5G-AKA
   acceptance window for a fresh UE (`SQN-MS` starts at 0), so every UE answered
   "Authentication Failure due to SQN out of range", and AUTS re-sync then failed
   (`Re-Sync MAC failed`). Provisioning now uses a low starting SQN.
2. **NSSF `taList`** — the serving TAI (208/93, TAC 000001) was absent, so NSSF logged
   `No TA ... in NSSF configuration`. The TAC must be the quoted 6-hex-digit string
   `"000001"`; an integer `1` does not match.
3. **UE requested NSSAI** — the stock `uecfg.yaml` requested a second slice (`112233`)
   that subscribers are not provisioned for. The AMF then treated the NSSAI as unservable
   and failed with `AMF can not select an target AMF by NRF`, so registration died on
   T3510 expiry. The UE now requests only the modeled slice (ADR-007).

## NOT ported (needed before M2 bring-up — flagged, not silently added)
The ported `docker-compose.yaml` also references files that were outside the `*cfg.yaml` port scope:
- `cert/` (per-NF TLS certs) — provided by free5GC bootstrap / generated at M2.
- `config/uerouting.yaml` (SMF UE routing), `config/upf-iptables.sh` (UPF), `config/prometheus.yml`.
These must be supplied/reconciled at M2. Source is pinned to **v4.2.3** while images are **v4.2.0**
(see `VERSIONS.lock` / M0 report) — reconcile at M2.
