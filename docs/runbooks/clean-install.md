# From-clean-Ubuntu runbook

Every command below has been **executed and verified** on this project except where marked
**ODE-ONLY** — those require a conforming Official Development Environment (bare-metal
Ubuntu 24.04) because they depend on the gtp5g kernel module.

Target: a working free5GC control plane, 20 registered UEs across two slices, and the
observability plane. Full user-plane connectivity is ODE-only.

---

## 0. Prerequisites

A machine matching the ODE (`docs/ode.md`): Ubuntu 24.04 LTS, x86_64, ≥8 threads,
≥16 GB RAM, ≥100 GB free SSD, full sudo.

```bash
sudo apt update
sudo apt install -y git python3 curl build-essential make gcc
```

`jq` is **optional** — `bootstrap.sh` falls back to `python3`, which Ubuntu ships by default.

Docker Engine + Compose v2 plugin must be installed and the daemon running.

## 1. Clone and verify the environment

```bash
git clone https://github.com/RithvikReddy0-0/5g-observability.git
cd 5g-observability
bash scripts/verify_env.sh
```

Expect `VERDICT: CONFORMING ODE` (exit 0). On a non-baseline host it prints a
`NON-BASELINE ENVIRONMENT` banner and exits non-zero — file work is still fine, but the M0
sign-off checklist is not satisfiable.

## 2. Materialize the pinned upstreams

```bash
bash scripts/bootstrap.sh              # clone at pinned SHAs; expect BOOTSTRAP: PASS
bash scripts/bootstrap.sh --verify-only  # integrity gate, no network needed
```

Takes ~4–5 minutes (free5GC pulls 15 submodules; ~113 MB total).
`external/` is git-ignored and disposable — `--clean` wipes and re-fetches it, and is proven
to reproduce byte-identical SHAs.

## 3. **ODE-ONLY** — prove the gtp5g ↔ kernel pair

```bash
bash scripts/validate_gtp5g.sh
```

This is the central M0 objective and the one genuinely host-coupled step. It **refuses to
run on WSL2** by design. On failure it HARD-STOPS — escalate for a human re-pin; do not
switch kernel or gtp5g version autonomously (brief §8.6).

On success it records the kernel and module version into `VERSIONS.lock`.

## 4. Bring up the core

```bash
cd deployments/compose
docker compose up -d db free5gc-nrf
sleep 10
docker compose up -d free5gc-amf free5gc-ausf free5gc-udm free5gc-udr \
                     free5gc-pcf free5gc-nssf free5gc-webui
docker compose up -d --no-deps free5gc-smf
```

`--no-deps` on the SMF matters: it declares `depends_on: free5gc-upf`, and without the flag
Compose drags the UPF in. **On the ODE, start the UPF too** (`docker compose up -d free5gc-upf`)
before the SMF, so the PFCP association succeeds.

TLS certs are auto-generated on first start into `cert/` — that directory is git-ignored
because it contains private keys.

Verify every NF registered (the NRF's own API enforces OAuth2, so read MongoDB):

```bash
docker compose exec -T db mongo free5gc --quiet \
  --eval 'db.NfProfile.distinct("nfType").sort().join(" ")'
```

Expect: `AF AMF AUSF NSSF PCF SMF UDM UDR`.

## 5. Provision subscribers (two slices)

```bash
cd ../..
bash scripts/provision_subscribers.sh --delete-all
COUNT=10 START=1  SD=010203 bash scripts/provision_subscribers.sh
COUNT=10 START=11 SD=112233 bash scripts/provision_subscribers.sh
```

Goes through the WebUI REST API so free5GC owns the schema. Two things this handles that
hand-written MongoDB writes get wrong: credentials persist as `encPermanentKey`/`encOpcKey`
(not the POST body's shape), and **GPSI must be unique per subscriber** or the API returns
`{"cause":"duplicate gpsi"}`.

## 6. Start the RAN and register UEs

```bash
cd deployments/compose && docker compose up -d --no-deps ueransim && cd ../..
COUNT=10 COUNT_B=10 TEMPO=600 SETTLE=80 bash scripts/start_ues.sh
```

Expect `UEs REGISTERED: 20 / 20`.

**Always re-provision immediately before launching UEs.** Subscriber SQN advances on every
authentication while a restarted `nr-ue` begins again at `SQN-MS 0`; after enough retry
cycles they desynchronise and authentication fails in a way that looks like a wrong key.

Verify from the RAN side:

```bash
docker compose exec -T ueransim ./nr-cli imsi-208930000000001 -e status
```

Expect `rm-state: RM-REGISTERED`, `mm-state: MM-REGISTERED/NORMAL-SERVICE`.

## 7. Observability plane

```bash
cd observability && docker compose up -d && cd ..
bash scripts/start_slice_exporter.sh
```

| Service | URL |
|---|---|
| Prometheus | http://localhost:9090 — expect 10 healthy targets |
| Grafana | http://localhost:3000 — anonymous viewer, dev only |
| Slice exporter | http://localhost:9105/metrics |

The slice exporter runs on the **host** (it shells out to `docker`) and Prometheus scrapes it
at the compose gateway `10.100.200.1:9105`; `host.docker.internal` does not resolve on this
user-defined network.

## 8. **ODE-ONLY** — prove the user plane

Not yet authored, because it has never been executed. On the ODE, with the UPF running:
establish a PDU session, confirm the UE gets an IP from the slice pool, and ping through the
UPF to the data network. Then re-author
`diagrams/pdu-session-establishment.mmd` from real logs — its design-only section is
explicitly marked as unverified.

## 9. Capture evidence and freeze

```bash
bash scripts/collect_evidence.sh          # timestamped bundle under docs/evidence/
```

Then fill the remaining `TO_BE_FILLED_ON_LAB_ODE` fields in `VERSIONS.lock`, record the run
in `docs/runbooks/m0-report.md`, and tag the baseline.

---

## Teardown / rebuild

```bash
cd deployments/compose && docker compose stop && cd ../..
cd observability && docker compose stop && cd ..
bash scripts/start_slice_exporter.sh --stop
```

`stop` (not `down`) preserves MongoDB data, Grafana settings and Prometheus history.

**Verified stop/start cycle** — all 13 containers stopped, then restarted, and state compared:

```
BEFORE:  NF=[AF AMF AUSF NSSF PCF SMF UDM UDR] subscribers=20 slices=[010203,112233]
AFTER:   NF=[AF AMF AUSF NSSF PCF SMF UDM UDR] subscribers=20 slices=[010203,112233]
         IDENTICAL
```

That proves the *warm* cycle. The **stronger** acceptance item — `docker compose down -v`
(destroying volumes) then rebuilding from step 4 including re-provisioning — has **not** been
run, and belongs on the ODE alongside the user-plane validation.

All core services now carry `restart: unless-stopped`. This matters: Docker Desktop bounced
several times during development, exiting every core container with code 255 at once; without
a restart policy only the observability stack recovered.
