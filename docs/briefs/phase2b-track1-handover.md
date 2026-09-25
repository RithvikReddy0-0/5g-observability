# Phase 2b — Track 1 handover: the 100-UE, three-slice stack

For Tracks 2, 3 and 4 ([work division](phase2b-work-division.md)). Track 1 is done. This is the
stack every other track measures against, and what you need to use it.

## Bring it up

```bash
make o5gs-build     # once: images from the pinned commits (~6 min cold)
make o5gs-up        # core, three slices, 100 UEs with PDU sessions (~1 min)
make o5gs-gate      # is every slice up and serving its UEs? (24 KPIs)
make o5gs-ping      # user plane through each slice's own UPF
```

Keep a WSL terminal open while it runs: WSL stops the Docker engine when the Ubuntu distro goes
idle.

## What is where

| | eMBB | URLLC | mMTC |
|---|---|---|---|
| UEs · IMSIs | 20 · `208930000000001–020` | 10 · `…021–030` | 70 · `…031–100` |
| S-NSSAI | 1 / 010203 | 2 / 112233 | 3 / 334455 |
| UE address pool · gateway | 10.45.0.0/16 · 10.45.0.1 | 10.46.0.0/16 · 10.46.0.1 | 10.47.0.0/16 · 10.47.0.1 |
| Session AMBR · latency budget | 200 Mbps · 300 ms | 20 Mbps · 10 ms | 1 Mbps · 1000 ms |
| Data network (put your servers here) | `o5gs-dn-embb` 10.53.0.51 | `o5gs-dn-urllc` 10.53.0.52 | `o5gs-dn-mmtc` 10.53.0.53 |
| UPF · its TUN device | `o5gs-upf-embb` · ogstun | `o5gs-upf-urllc` · ogstun2 | `o5gs-upf-mmtc` · ogstun3 |

**Sending traffic from a UE.** All 100 UEs run inside `o5gs-ue`. Each UE has its own interface,
`uesimtun<last four IMSI digits>0` — IMSI …0042 is `uesimtun00420`. Bind to it:

```bash
docker exec o5gs-ue ping -I uesimtun00420 10.47.0.1                 # one mMTC device
# ten bytes of UDP from that device to the mMTC data network (bind to the UE's own address):
docker exec o5gs-ue python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('10.47.0.13', 0)); s.sendto(b'0123456789', ('10.53.0.53', 5000))"
```

The UE and DN images have `python3` and `iperf3`, not `nc`. The address of each interface is in
`docker exec o5gs-ue ip -4 -o addr show`. Traffic to a slice's
data network is routed, not NATed, so the DN sees each UE's own address. It can tell devices
apart.

**Do not generate test traffic inside a UPF container.** It starves the UPF you are measuring
(ADR-012). Run servers and collectors in the `o5gs-dn-*` containers.

## Metrics already available (Prometheus, http://localhost:9091)

| Metric | Meaning | Trust |
|---|---|---|
| `ue_probe_reachable{slice}` | UEs that answered through their UPF in the last 5 s round | exact, UE side |
| `ue_probe_rtt_ms{slice,stat=min\|avg\|max}` | round-trip UE → gNB → UPF gateway | measured |
| `upf_slice_{uplink,downlink}_{bytes,packets}_total{slice}` | traffic per slice at its UPF | exact |
| `fivegs_smffunction_sm_sessionnbr{snssai}` | PDU sessions per slice | exact |
| `ran_ue`, `gnb` | UEs and gNBs the AMF sees | exact |
| `fivegs_amffunction_rm_reginit{req,succ,fail}` | registrations | counters |
| `slice_*` (orchestrator, :9111) | capacity, admitted, measured, delivered flows | measured |

Some Open5GS gauges read wrong. Before using any metric not listed above, check
[docs/open5gs.md — Which metrics can be trusted](../open5gs.md#which-metrics-can-be-trusted).

## For each track

- **Track 2 (traffic and instruments).**
  - The UEs, their interfaces and a data network per slice are ready.
  - Nothing sends mMTC reports yet. The 10-byte beacons and their collector in `o5gs-dn-mmtc`
    are yours.
  - The UE probes ping every UE every 5 s. That is liveness, not the mMTC traffic profile.
- **Track 3 (KPIs, targets, scoring).**
  - `deployments/open5gs/kpi-gates.json` is the **deployment** gate: is every slice up, at its
    size.
  - The per-slice service KPIs against industry targets need their own definitions file, which
    Track 3 owns. Do not add them to the gate file.
  - Session AMBR is enforced by the gNB, not the core; measured mMTC throughput caps at 1.0 Mbps.
- **Track 4 (sweeps and report).**
  - `make o5gs-scale` writes a CSV (`docs/evidence/open5gs-scale/`) that opens in Excel. It
    covers attach time and control-plane CPU against batch size, and the cost of 100 → 300 UEs.
    It is the pattern for other sweeps: vary one parameter, fixed settle time, same instruments.
  - To vary the UE counts, use `O5GS_UES` in `deployments/slices.env` together with the UE
    container's `UE_PLAN`. CI rejects them if they disagree.

## Known limits of this laptop

- URLLC's worst-case RTT goes over 10 ms at times. A software UPF and a simulated RAN share one
  CPU. The average stays within budget.
- Watching 100 UEs costs CPU: the UE container uses ~39 % of a core at rest, mostly probe pings.
- Every registration includes one authentication resynchronisation (SQN). Provisioning resets
  the network's sequence number while each UE process starts fresh, so registration latency
  here is one AUSF/UDM round-trip longer than a real device's.
- The UERANSIM patches (UDP buffers, monotonic clock) are recorded in ADR-012 and ADR-014. The
  image is `o5gs/ueransim:v3.3.0-udpbuf-mono`.
