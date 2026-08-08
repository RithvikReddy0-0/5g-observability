# Interface Catalog — N1, N2, N3, N4, N6, SBI

**Deliverable per ADR-008.** Every row is marked with how it was established:

- **OBSERVED** — seen in logs/traffic on this deployment.
- **DESIGN** — from config and the pinned source, but **not observable here** because the
  user plane needs gtp5g, which WSL2 cannot provide (ADR-003, risk R-01).

DESIGN rows must be re-confirmed on the ODE before Phase 1 acceptance.

Deployment context: PLMN **208/93**, TAC **000001**, slices **SST 1/SD 010203** and
**SST 1/SD 112233**, DNN `internet`.

---

## N1 — UE ↔ AMF (NAS) · OBSERVED

| Property | Value |
|---|---|
| Endpoints | UE (UERANSIM `nr-ue`) ↔ AMF |
| Protocol | 5G NAS (5GMM / 5GSM), TS 24.501 |
| Transport | Not carried directly — encapsulated in NGAP over N2 |
| Security | NAS integrity **NIA2**, ciphering **NEA0** (from `amfcfg.yaml` security order) |
| Purpose | Registration, authentication, security mode, PDU session signalling |

Messages observed: Registration Request (SUCI) · Authentication Request/Response ·
Security Mode Command/Complete · Registration Accept · Registration Complete ·
UL NAS Transport · Configuration Update Command.

Failure modes seen: `Authentication Failure due to SQN out of range`,
`Network failing the authentication check`, T3510 / T3520 / T3580 expiry.

---

## N2 — gNB ↔ AMF (NGAP) · OBSERVED

| Property | Value |
|---|---|
| Endpoints | gNB `gnb.free5gc.org` ↔ AMF `10.100.200.16:38412` |
| Protocol | NGAP over **SCTP**, TS 38.413 |
| Purpose | NG interface management, UE context, NAS transport, PDU session resource setup |

Observed: `SCTP connection established` → `NG Setup Request` → `NG Setup Response` →
`NG Setup procedure is successful`; Initial UE Message; Initial Context Setup Request;
Error Indication.

Note: the gNB resolves the AMF by **name** at startup. If the AMF container is down the gNB
exits with the misleading `ERROR: Field 'address' must be a valid IP address, or FQDN…`.

---

## N3 — gNB ↔ UPF (GTP-U) · DESIGN ONLY

| Property | Value |
|---|---|
| Endpoints | gNB `gtpIp` ↔ UPF N3 endpoint `upf.free5gc.org` (from `smfcfg.yaml` `interfaces`) |
| Protocol | **GTP-U** over UDP/2152 |
| Purpose | User-plane tunnelling of UE traffic between RAN and core |
| Why unobserved | Requires the **gtp5g kernel module**; not loadable on WSL2 |

---

## N4 — SMF ↔ UPF (PFCP) · OBSERVED AS A FAILURE

| Property | Value |
|---|---|
| Endpoints | SMF `smf.free5gc.org` ↔ UPF `upf.free5gc.org` |
| Protocol | **PFCP** over UDP/8805, TS 29.244 |
| Purpose | Establish/modify/release user-plane sessions; install PDRs, FARs, QERs |

The association attempt **is** observable and fails by design here:

```
[SMF][Main] Failed to setup an association with UPF[upf.free5gc.org(0.0.0.0)],
            error: no destination IP address is specified
```

Consequence: `POST /nsmf-pdusession/v1/sm-contexts` returns **500**, so
`free5gc_amf_business_active_pdu_session_current_count` stays at 0.

---

## N6 — UPF ↔ Data Network · DESIGN ONLY

| Property | Value |
|---|---|
| Endpoints | UPF ↔ external DN |
| Protocol | Plain IP; NAT/iptables on the UPF (`config/upf-iptables.sh`, not ported) |
| UE address pools | slice A `10.60.0.0/16` · slice B `10.61.0.0/16` (`smfcfg.yaml`) |
| Why unobserved | No UPF, therefore no UE IP allocation and no egress |

---

## SBI — Service Based Interface · OBSERVED

| Property | Value |
|---|---|
| Transport | HTTP/2, `scheme: http` (TLS certs auto-generated but unused at this scheme) |
| Port | **8000** on every NF |
| Discovery | All NFs register with the **NRF** at `http://nrf.free5gc.org:8000` |
| Security | **OAuth2 enforced** — direct `curl` to NRF returns `verify OAuth Authorization header invalid` |
| Metrics | Separate port **9091** per NF (`metrics.enable: true`) |

Service endpoints observed in traffic:

| NF | Service | Path observed |
|---|---|---|
| NRF | Nnrf_NFManagement | `/nnrf-nfm/v1/nf-instances` |
| AUSF | Nausf_UEAuthentication | `/nausf-auth/v1/ue-authentications` |
| UDM | Nudm_UEAuthentication | `/nudm-ueau/v1/{supiOrSuci}/security-information/generate-auth-data` |
| NSSF | Nnssf_NSSelection | `/nnssf-nsselection/v2/network-slice-information` |
| SMF | Nsmf_PDUSession | `/nsmf-pdusession/v1/sm-contexts` |
| WebUI | subscriber provisioning | `/api/subscriber/{ueId}/{plmnId}` (port 5000) |

**The NSSF query string is the only place S-NSSAI appears on the wire in a form we can
capture externally** — it carries the requested slice and TAI as URL-encoded JSON. This is
the basis for a future ADR-004 rung-4 (SBI capture) approach; see `docs/logging.md`.

---

## Interfaces intentionally not deployed

`N9` (UPF↔UPF, ULCL only) · `N3IWF`/`TNGF` non-3GPP access · `CHF` charging · `NEF`
exposure. Their configs are present but the services are not started, keeping the Phase 1
baseline minimal.
