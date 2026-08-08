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

## Caveat: the AMF business gauges LEAK under churn on v4.2.0

They are not simply broken — they are accurate on a clean run and drift badly once
procedures abort or the AMF restarts. Both states were measured on this deployment:

| Source | After heavy churn | After a clean cycle |
|---|---|---|
| `nr-cli` per-UE status (ground truth) | **20/20** registered | **20/20** registered |
| `free5gc_amf_business_ue_connectivity{3GPP}` | **3** (undercounts) | **20** ✅ matches |
| `free5gc_amf_business_ue_cm_gmm_state_count{cm-connected}` | **63** (overcounts) | **20** ✅ matches |

The gauges are adjusted on state transitions, so aborted procedures (T3510/T3580 timeouts,
failed PDU session creation) and AMF restarts leave them unbalanced — and they never
self-correct. Wrong in *both* directions depending on which transitions were lost.
`ue_gmm_state_count` additionally accumulates despite its "current number" HELP text.

**Consequence:** these gauges are only trustworthy immediately after a clean bring-up, which
is precisely when you least need them. Do not use them as the UE population figure in a
long-running deployment. Ground truth is
`docker exec ueransim ./nr-cli <supi> -e status`, captured by
`scripts/collect_evidence.sh`. The counter-based series (`*_msg_received_total`,
`*_sbi_inbound_request_total`) and `up` behave correctly throughout.

For a clean reading: drop the stale NRF registry, restart the NFs, re-provision, and
relaunch the UEs — the recipe in `scripts/` does this end to end.

This is exactly the kind of gap the observability plane exists to expose, and it is a concrete
input to the ADR-004 decision about whether NF-native telemetry is sufficient or an
instrumentation patch is eventually warranted.

## Slice-labeled metrics (closes R-02)

free5GC's native metrics carry **no slice identity** — verified across all 629 series and
15 metric names. That blocks the ADR-006 labeling contract the Slice Correlation Engine
depends on (risk **R-02**).

`slice-exporter/slice_exporter.py` closes the gap at **ADR-004 rung 2 (log-derived)**, with
no changes to free5GC source:

```bash
scripts/start_slice_exporter.sh            # start (also --stop, --status)
```

| Metric | Labels | Source |
|---|---|---|
| `free5gc_slice_provisioned_subscribers` | `sst`, `sd` | MongoDB `amData.nssai.defaultSingleNssais` |
| `free5gc_slice_ues_observed_registered` | `sst`, `sd` | AMF log `GMM State[Registered]`, SUPI joined to its provisioned slice |
| `free5gc_slice_smf_selection_total` | `sst`, `sd`, `dnn` | AMF log `Select SMF [snssai: {Sst:1 Sd:010203}, dnn: internet]` |

Slice identity is real, taken from these AMF log lines (present at **INFO**, no debug needed):

```
[AMF][Gmm][supi:SUPI:imsi-208930000000004] Select SMF [snssai: {Sst:1 Sd:010203}, dnn: internet]
```

**Honesty notes.**
- These are **derived** metrics, not NF-native slice labels. The upstream gap is unchanged;
  this makes slice-aware observability possible without patching free5GC, and is the
  evidence base for the eventual ADR-004 decision on whether a patch is warranted.
- `free5gc_slice_ues_observed_registered` is **log-window derived, not a live gauge**, and
  this matters in practice: it is an *activity* signal, not a population count. UERANSIM UEs
  give up after ~5 PDU-session retries and then go silent, at which point they emit no
  further AMF log lines and **this metric decays to 0 even though all 20 UEs are still
  RM-REGISTERED** (observed in `docs/evidence/run-20260803-093841Z`). For UE population use
  `free5gc_slice_provisioned_subscribers` together with the `nr-cli` ground truth captured
  by `scripts/collect_evidence.sh`. `free5gc_slice_smf_selection_total` is a cumulative
  counter and does not have this problem.
- The exporter runs on the **host**, not in a container, because it shells out to `docker`.
  Prometheus reaches it at `10.100.200.1:9105` (the compose network gateway);
  `host.docker.internal` does **not** resolve on this user-defined network.
- It deliberately avoids `nr-cli`: one `docker compose exec` per UE took >180s for 20 UEs
  on this memory-constrained host and timed out every scrape. A single `docker compose logs`
  read returns in under a second and carries the same slice identity.

## Alerts

`prometheus/alert-rules.yml` defines NFExporterDown, NoConnectedUEs, SbiServerErrors and
SbiLatencyHigh. `SbiServerErrors` is **expected to fire** on a non-baseline host for
`/nsmf-pdusession/v1/sm-contexts` — PDU session creation cannot succeed without the UPF.

The `alert-rules.yml` ported from the previous project was an empty 0-byte file (as was its
`prometheus.yml`); these rules are newly written.
