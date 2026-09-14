# ADR-012 — Make the lab data path deliver what the slices promise

**Status:** Accepted, 2026-09-14
**Amends:** ADR-010, ADR-011

## Context

The slice orchestrator was changed to run every admitted demand as a real UDP flow at its
admitted bitrate. The first run
([`evidence/open5gs-orchestrator/1-before-fixes-shared-buffers`](../evidence/open5gs-orchestrator/1-before-fixes-shared-buffers/1-saturation.txt))
admitted up to 186 Mbps on eMBB — within its 200 Mbps policy capacity — and **0 of 78 eMBB
flows received their rate**. URLLC's 72 flows were all delivered. TCP had carried 270 Mbps on
the same path earlier, because TCP backs off when packets drop and UDP does not.

The laptop could not deliver the capacity the slice definition promised. Each bottleneck was
located by accounting for every lost packet, not by guessing.

## Decisions, in the order the measurements forced them

### 1. Size UERANSIM's UDP socket buffers (patch)

The eMBB gNB had dropped **128 382** datagrams to receive-buffer overflow; the URLLC gNB none.
UERANSIM never sets `SO_RCVBUF`, so its sockets get the kernel default of 212 992 bytes, and
`net.core.rmem_max` is not changeable from inside a container on this kernel.

[`deployments/open5gs/images/patches/ueransim-udp-socket-buffers.patch`](../../deployments/open5gs/images/patches/ueransim-udp-socket-buffers.patch)
adds one block to UERANSIM's `Socket` constructor: when `UERANSIM_UDP_BUFFER_BYTES` is set, size
both buffers with `SO_RCVBUFFORCE`/`SO_SNDBUFFORCE` (which exceed `rmem_max` given
`CAP_NET_ADMIN`), falling back to the capped options. The image build fails if the patch stops
applying, and the image is tagged `v3.3.0-udpbuf` so it is never mistaken for upstream. The
gNBs and the UE container run with 8 MiB and `NET_ADMIN`; `ss -m` confirms `rb16777216`.

After it: 0 gNB drops at 80 Mbps — but 8.5 % loss remained.

### 2. Lengthen the UPF's TUN queue

With the gNB clean, every remaining lost packet was a `tx_dropped` on the UPF's TUN device
(+9 638 against 9 439 lost by iperf3): the kernel's queue towards the UPF process, 500 packets by
default, overflowed under UDP bursts. `upf-entrypoint.sh` now sets `txqueuelen 10000`. The same
80 Mbps run: **0.00 % loss** (111 521 packets).

Deliverable eMBB capacity: 40 → 120 Mbps.

### 3. Generate test traffic in a data network, not inside the UPF

At 160 Mbps the UPF container ran at 973 % CPU. The top consumers were not the UPF but ten
iperf3 senders running *in the same container*, each spending 44–60 % of a core in UDP pacing
loops, starving the UPF they were meant to measure (51 582 TUN drops).

Each slice now has a data-network container — `o5gs-dn-embb` 10.53.0.51, `o5gs-dn-urllc`
10.53.0.52 — routed to its UE pool through its own UPF. Traffic to the DNs is exempt from the
UPF's NAT, so a DN sees each UE's real address. `traffic.sh`, `verify_user_plane.sh`,
`calibrate_capacity.sh` and the orchestrator all send traffic between devices and DNs.

Deliverable eMBB capacity: 120 → **200 Mbps** at 0.43 % loss. The laptop now delivers the full
policy capacity; `CAPACITY_OVERRIDE` exists in the orchestrator for hosts that cannot.

### 4. Never spill best-effort demand into a premium slice

With eMBB full, the orchestrator admitted **video onto URLLC** — 16 of its 20 Mbps — because the
code accepted any latency-qualified slice with room. That is precisely what the earlier "cheapest
adequate slice" fix set out to prevent, and it only appears when the natural slice is full. A
demand is now placed only on the least specialised slices that meet its latency need, and
refused if those are full; `SPILLOVER=1` restores the old behaviour deliberately.

### 5. Identify a UE by its session address, not its interface name

After a UPF restart, two UEs believed they owned the same `uesimtunN`. When a session drops its
TUN device is destroyed, and the next session anywhere may reuse the name. The supervisor checked
by name, pinged successfully through *another* UE's session, and kept two dead UEs marked healthy
(the KPI gate's UE-side probe caught it: 9/10 reachable). It now resolves each UE's interface from
its SMF-allocated address, which is unique. Reproduced with the same trigger: 2 UEs detected
("session address is on no interface"), restarted, 20/20 back within 60 s.

## Result

Same orchestrator experiment before and after
([`evidence/open5gs-orchestrator/`](../evidence/open5gs-orchestrator/README.md)):

| | Flows met | eMBB | URLLC | Refused (capacity) |
|---|---|---|---|---|
| Before | 72 / 150 | 0 / 78 | 72 / 72 | 2 |
| After, 3 demands/s | **164 / 165** | 68 / 69 | 96 / 96 | 15 — both slices at exactly 200/200 and 20/20 Mbps |

## Consequences

- One local patch to UERANSIM. It is small, off unless its environment variable is set, and
  verified at build time; it changes buffer sizes, not protocol behaviour.
- Test traffic now flows UE ↔ gNB ↔ UPF ↔ DN, which is also the shape of a real deployment.
- An unmanaged TCP flood reached 542 Mbps on eMBB — above its 200 Mbps slice capacity. The gNB
  enforces each *session's* AMBR, but nothing enforces a *slice* aggregate; the orchestrator's
  measured-load admission is what keeps admitted traffic within it.
- Any UPF restart drops some sessions; the supervisor restores them (6 UEs in ~10 s observed).
