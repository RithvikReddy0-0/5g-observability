# ADR-011 — A dedicated gNB and UPF per slice

**Status:** Accepted, 2026-09-14
**Amends:** ADR-010 (Open5GS stack topology)

## Context

On the Open5GS stack both slices shared one UERANSIM gNB and one UPF. The slice-isolation test
([`scripts/open5gs/isolation_test.sh`](../../scripts/open5gs/isolation_test.sh)) showed that
saturating eMBB made URLLC lose probes — up to 4 of 10 UEs unanswered per 5 s round — while
URLLC's own load was nothing but pings.

The cause was measured rather than assumed. With eMBB saturated, per container:

| | CPU | UDP receive-buffer drops |
|---|---|---|
| UE processes (3 loaded) | 358 % | — |
| **shared gNB** | 183 % | **12 817 in 60 s** (0 at idle) |
| shared UPF | 72 % | 0 |

The UPF had headroom. The gNB's single UDP socket overflowed, and a full receive buffer drops
packets regardless of which slice they belong to. A dedicated UPF alone would therefore not
have fixed it.

## Decision

Each slice gets its own gNB **and** its own UPF:

| | eMBB | URLLC |
|---|---|---|
| gNB | `o5gs-gnb-embb` 10.53.0.30, advertises SST 1/010203 | `o5gs-gnb-urllc` 10.53.0.32, advertises SST 2/112233 |
| UPF | `o5gs-upf-embb` 10.53.0.7, pool 10.45/16 on `ogstun` | `o5gs-upf-urllc` 10.53.0.8, pool 10.46/16 on `ogstun2` |
| Selection | UE `gnbSearchList` → its slice's gNB | SMF `pfcp.client.upf[].dnn` → its slice's UPF |

The control plane (AMF, SMF, NRF, NSSF, AUSF, UDM, UDR, PCF) stays shared, as it normally is
across slices. UPF selection by DNN is native Open5GS behaviour
(`compare_ue_info()` in `src/smf/context.c`); each slice already had its own DNN.

This mirrors how slices are isolated in practice: slice-specific RAN resources and a
slice-specific user plane over a common core.

## Result

Same test, same instrument, 60 s per phase.
Evidence: [`docs/evidence/open5gs-isolation/`](../evidence/open5gs-isolation/).

| Topology | URLLC min reachable, eMBB saturated | URLLC worst RTT, eMBB saturated | gNB drops, eMBB saturated |
|---|---|---|---|
| Shared (4 runs) | 6, 8, 9, 10 / 10 | 7.4 – 10.0 ms | 12 817 (shared gNB) |
| **Dedicated (3 runs)** | **10, 10, 10 / 10** | **5.5 – 7.0 ms** | eMBB gNB 14 000 – 17 800 · **URLLC gNB 0** |

The eMBB flood still overflows a gNB — its own. Congestion now stays inside the slice that
caused it: eMBB probes still miss (7–9 / 10 under its own load), URLLC does not.

## Consequences

- **What is not isolated:** CPU. Both slices' processes share the laptop's 12 cores, and URLLC's
  worst-case RTT still exceeds 10 ms at idle in some runs (8.1 – 11.5 ms). CPU pinning was not
  needed to remove the measured loss and was not attempted.
- **The UPF's native metrics become per-slice.** With one UPF per slice, its session gauge and
  volume counters describe one slice each; a gate checks that each slice's UPF holds sessions.
  (The session gauge still over-counts after UE restarts — presence only.)
- **Operational rule — restart the UPFs after the SMF.** Restarting only the SMF left the
  already-running eMBB UPF with stale PFCP association state: it rejected every session
  (`Cannot find PFCP-Node`) and answered user packets with GTP Error Indications, while the
  URLLC UPF, started after the SMF, worked. Restarting the eMBB UPF fixed it. An engine restart
  restarts everything together and is unaffected. The UE supervisor detected this outage within
  ~30 s (10 eMBB restarts logged) but cannot repair a UPF-side fault.
- **More containers:** 2 gNBs and 2 UPFs instead of one each. Idle cost is small
  (UERANSIM ~0.6 %, UPF ~0.1 % CPU at idle).
