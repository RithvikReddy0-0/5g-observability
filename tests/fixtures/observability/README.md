# Log fixtures for the observability tools (ADR-024)

Real lines from the running Open5GS stack, captured read-only with `docker logs --tail 200 <container>`
and `docker exec o5gs-ue grep … /tmp/ue-*.log` on 2026-11-08, then cut down to a few lines per kind of
message by a script. Used by `tools/observability/test_observability.py`.

| File | Source | What it exercises |
|---|---|---|
| `o5gs-amf.log` | `o5gs-amf` (ANSI colour codes kept, as Docker stores them) | colour stripping, PDU session requests with S-NSSAI per slice, NAS MAC failure, registration |
| `o5gs-smf.log` | `o5gs-smf` | sessions with DNN and address per slice; the URLLC UPF's PFCP association loss and restoration (09:09–09:24) and the mMTC one (09:37) |
| `o5gs-upf-mmtc.log` | `o5gs-upf-mmtc` | IP pool exhaustion, PFCP de-/re-association |
| `o5gs-upf-urllc.log` | `o5gs-upf-urllc` | session add/remove, APN per slice |
| `o5gs-nrf.log` | `o5gs-nrf` | NF registered / de-registered, NF shutdown and start |
| `o5gs-gnb-urllc.log` | `o5gs-gnb-urllc` | RRC/NGAP procedures, UE signal lost |
| `o5gs-gnb-urllc-n2-refused.log` | quoted in `docs/evidence/open5gs-deploy/deploy-20260914-092251Z.txt` | the gNB that could not reach the AMF (port typo), the deploy the gate rolled back |
| `o5gs-ue-supervisor.log` | `o5gs-ue` stdout (`ran/ue-entrypoint.sh`) | UE restarted after losing its data path, per slice |
| `o5gs-scheduler.log` | `o5gs-scheduler` | scheduler re-applies per mode |
| `ue-208930000000001.log` | `/tmp/ue-208930000000001.log` in `o5gs-ue` | a UE's registration, both PDU sessions, a barred access attempt |

Anonymised: NF instance UUIDs are replaced by `00000000-0000-4000-8000-0000000000NN`. IMSIs are the
lab's test PLMN 208/93 and the addresses are the private lab network; nothing else identifies anyone.

No "Radio link failure detected" line was in the UE logs at capture time (the UE container had been
recreated 27 minutes before). The tests build that line from the pinned UERANSIM source string
(`src/ue/nas/mm/radio.cpp:260`, `m_logger->err("Radio link failure detected")`) and say so.
