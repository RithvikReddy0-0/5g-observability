# ADR-019 — Packet scheduler: every radio packet classified by its headers, slices scheduled on a shared cell

**Status:** Accepted, 2026-11-08
**Builds on:** ADR-011 (a gNB, UPF and data network per slice), ADR-012 (UERANSIM UDP buffers),
ADR-016 (slice orchestrator: admission, ARP pre-emption, feedback controller)
**Answers:** the guide's question after the Phase 2b review — "is there a scheduler that looks at
the packets and divides the traffic, like the MAC layer does?" Until now, no.

## Context

The slice orchestrator decides per **flow**: which slice serves a demand, whether it fits, which
flow to pre-empt. Once a flow is admitted, nothing decided which **packet** goes first:

- the Open5GS UPF forwards in arrival order; it stores 5QI and ARP but does not schedule by them;
- UERANSIM has no MAC scheduler — its "radio" is UDP between the UE container and the gNBs;
- every queue on the path was first-in, first-out.

In a real network the gNB's MAC scheduler does this job every slot: it knows each packet's QoS
flow, and it gives the radio to the flows that need it first. Slicing in the RAN is exactly this:
one cell, several slices, a scheduler that keeps them apart.

On this testbed the slices do not share a gNB or a UPF (ADR-011), but they do share one link:
every UE runs in one container, so all their radio traffic — every slice, both directions —
crosses that container's network interface. That link is where the cell is.

## Decisions

### 1. Classify by reading the packet

Every UERANSIM radio packet is an RLS message over UDP port 4997 with a fixed layout
(`src/lib/rls/rls_pdu.cpp`, pinned v3.3.0):

| Bytes of the UDP payload | Field |
|---|---|
| 0 / 1–3 | 0x03 / version 3.3.0 |
| 4 | message type: 4 heartbeat, 5 heartbeat ack, 6 PDU transmission, 7 PDU ack |
| 5–12 | sender id |
| 13 | PDU type: 1 RRC (signalling, NAS inside), 2 user data |
| 18–21 | PDU session id (user data) or RRC channel |
| 26 → | the UE's own IP packet |

Linux `tc` u32 filters match fixed offsets from the outer IP header (20 bytes, no options, as Docker
sends them, plus 8 of UDP: the RLS message starts at 28). So the classifier reads:

1. **signalling** — heartbeats, acks, and RRC PDUs (which carry registration, session setup, NAS);
2. **user data** — then the UE's inner address: its pool gives the slice (10.45 eMBB, 10.46 URLLC,
   10.47 mMTC), and for URLLC its exact address gives the subscriber, and with it the ARP tier
   (the three critical devices of ADR-016).

The slice is taken from the packet, not from the gNB the packet came through: a URLLC session
carried by the eMBB gNB (ADR-020) is still scheduled as URLLC. Inner addresses sit at offsets 66
and 70, which are not 4-byte aligned, so an address is matched as two 16-bit keys and a /16 pool as
one.

The critical devices' addresses are not configured: the scheduler reads them from the UEs' own logs
(the three IMSIs of the ARP-1 tier in `slices.env`) and re-applies when they change.

### 2. Schedule with HTB, one class per slice and per ARP tier

| Class | HTB prio | Guaranteed (% of cell) | Queue |
|---|---|---|---|
| signalling | 0 | 2 | short FIFO |
| URLLC critical (ARP 1) | 1 | 10 × 3/10 | short FIFO (64) |
| URLLC standard (ARP 2) | 2 | 10 × 7/10 | short FIFO (64) |
| eMBB | 3 | 60 | fq_codel |
| mMTC | 4 | 5 | fq_codel |
| anything else | 6 | 3 | fq_codel |

Every class may borrow up to the whole cell. HTB serves classes inside their guarantee in priority
order and lends idle capacity to the highest-priority class that wants more, so URLLC is served
first, eMBB uses whatever is left, and no class is starved below its guarantee. A delay-critical
slice (latency budget ≤ 20 ms) gets a short FIFO — a long queue is the problem, not the cure; the
others get fq_codel, which is fair per flow and keeps their own queues short (RFC 8290).

The shares and priorities are the **tuning knobs**: `SLICE_<X>_SCHED_SHARE` and
`SLICE_<X>_SCHED_PRIO` in `slices.env`, changeable at run time with `POST /config`.

### 3. A cell with a capacity

On an unshaped link the bottleneck is wherever the CPU runs out first, which moves between runs
(eMBB delivered 255–455 Mbps on identical runs, ADR-015). A queue that never builds cannot be
scheduled. The cell therefore has a capacity, `SCHED_CELL_DL_MBPS` = 250 and
`SCHED_CELL_UL_MBPS` = 100. Downlink is shaped on the host side of the UE container's veth (before
packets enter the container), uplink on the container's own `eth0`.

### 4. Three modes, so that like is compared with like

- `sliced` — the tree above (default);
- `fifo` — one drop-tail FIFO at the same capacity: a shared cell **without** a scheduler;
- `off` — no shaping, the stack as it was.

### 5. Where it runs

`tools/packet-scheduler/scheduler.py`, in a small container (`o5gs/scheduler:1`: pinned Python plus
iproute2 and nsenter) on the host's network and PID namespaces, privileged, code mounted read-only
(`scripts/open5gs/start_scheduler.sh`, `make o5gs-scheduler`). It finds the UE container through
any `nr-ue` process, its host-side veth through the interface index, and re-applies when the UE
container is recreated. Prometheus scrapes it at `10.53.0.1:9114`; Grafana has a row for it.

## Verification

- **Unit tests** (`test_scheduler.py`, in CI): packets encoded byte for byte as
  `EncodeRlsMessage` does, classified by the same filter data that becomes the tc commands; the
  class tree, the modes, the configuration checks.
- **Real traffic**: `tests/fixtures/rls-sample.pcap`, 3 000 packets captured on the UE container's
  link while every slice carried traffic. Every radio packet in it is classified into a slice or
  signalling; none falls through.
- **Live counters**: with the stack idle, URLLC uplink split 2 277 : 5 306 between the ARP tiers —
  3 : 7, exactly the ratio of critical to standard devices.
- **Gate**: a new KPI, "the cell scheduler drops no signalling" (gate severity).

## Results

Measured on the live stack, two rounds per comparison, the second in reverse order
([evidence](../evidence/open5gs-scheduler/README.md)). RTT is ping from the device's own
session to its UPF; a radio-link failure leaves a UE without a data path, so the stack is restored
before each run.

**A — eMBB offers 400 Mbps into a 200 Mbps cell** (the CPU saturates: load average ~10 on 12 threads)

| Mode | Radio-link failures | URLLC loss (crit / std) | URLLC p99 (crit) | eMBB delivered |
|---|---|---|---|---|
| idle | 0 | 0 / 0 % | 6.3 ms | — |
| off (no shaping) | 0, 0 | 0 / 0 % | 27.7, 30.5 ms | 387, 392 Mbps |
| fifo (cell, no scheduler) | **95, 99** | **100 / 77–100 %** | — | 98, 76 Mbps |
| sliced | **0, 0** | **0 / 0 %** | 27.0, 48.4 ms | 192, 191 Mbps |

**B — six standard URLLC devices flood a 100 Mbps cell** (the cell is the bottleneck)

| Mode | Radio-link failures | Critical p99 / loss | Standard p99 / loss |
|---|---|---|---|
| fifo | 94, 93 | 106, 235 ms / 26 % | 105, 109 ms / 82–91 % |
| sliced | **0, 0** | **3.2, 7.8 ms / 0 %** | 9.2, 13.7 ms / 24–28 % |

**C — the guaranteed share as a knob** (eMBB 160 + mMTC 40 Mbps offered into a 60 Mbps cell)

| mMTC share | 5 % | 10 % | 15 % | 20 % | 25 % |
|---|---|---|---|---|---|
| guaranteed | 3.0 | 6.0 | 9.0 | 12.0 | 15.0 Mbps |
| delivered | 3.7 | 5.6 | 8.4 | 11.2 | 14.0 Mbps |

**D — eMBB offers 150 Mbps into a 100 Mbps cell** (the cell, not the CPU, is the bottleneck)

| Mode | Radio-link failures | URLLC p99 (crit / std) | URLLC loss | mMTC p99 | Unloaded eMBB device p99 | eMBB delivered |
|---|---|---|---|---|---|---|
| fifo | 97, 99 | 107 ms (std) / — | 11-100 % | 108-110 ms | 110-126 ms, 37-72 % lost | 50-58 Mbps |
| **sliced** | **0, 0** | **4.3-4.6 / 4.6-5.0 ms** | **0 %** | **4.3-5.5 ms** | **10-18 ms** | **90-93 Mbps** |
| off (no cell: not overloaded) | 0, 0 | 18.8, 4.6 / 16.6, 4.8 ms | 0 % | 9.7, 4.3 ms | 506, 8.6 ms | 150 Mbps |

`off` has no cell capacity, so at 150 Mbps nothing is overloaded and it varies with the host's other
load; the like-for-like comparison is fifo against sliced, at the same cell.

What this says:

1. A shared cell **without** a scheduler fails catastrophically under overload: almost every UE
   loses its radio link, because heartbeats and RRC are dropped with the data. With the scheduler,
   no radio-link failure and no URLLC loss in any run.
2. Where the cell is the bottleneck, the scheduler wins on every measure (D): URLLC 4-5 ms and mMTC
   4-5 ms against FIFO's 100+ ms and 97-99 radio-link failures, and even the overloaded slice's own
   idle device stays at 10-18 ms, because fq_codel serves its sparse flow first. ARP works per
   packet too (B): the critical device stays at single-digit milliseconds while the standard tier it
   outranks is flooded.
3. The guaranteed share is a working knob: the delivered rate follows it.
4. Where the **CPU** is the bottleneck (A), URLLC's tail is CPU contention and the scheduler does
   not improve it; admission control (ADR-016/018) is the layer that keeps eMBB from being offered
   beyond capacity in the first place.
5. Unresolved: an overloaded slice's *own* devices. At 2× overload an unloaded eMBB device's p99 was
   12.5 s sliced against 1.9–2.7 s unshaped; every kernel queue on its path was near-empty, so the
   delay builds inside the overloaded eMBB gNB process. Other slices were unaffected.

## Consequences

- (+) The guide's question has a measured answer: packets are classified from their headers and
  scheduled per slice and per ARP tier, and the difference from a scheduler-less cell is measured.
- (+) Shares and priorities are knobs with a measured effect, so "tuning the characteristics" is a
  sweep, not a guess.
- (+) The slice of a packet comes from the packet, so the scheduler keeps working when a gNB carries
  more than one slice (ADR-020).
- (−) It schedules at the IP layer on an emulated cell, not on radio resources: UERANSIM has no
  PRBs, slots or channel. Radio-level scheduling needs a real RAN (OpenAirInterface or srsRAN).
- (−) The offsets assume the UERANSIM 3.3.0 RLS layout and an outer IPv4 header without options.
  The unit tests pin both; a UERANSIM upgrade must re-run them against a fresh capture.
- (−) The cell capacity is a declared number, set below what this laptop can push so that the queue
  is the bottleneck. With the scheduler on, eMBB is capped at the cell (250 Mbps downlink).
- (−) The scheduler is privileged and outside Compose (like the orchestrators, it is optional: the
  gate only advises when it is down). On Kubernetes it would be a privileged DaemonSet; not built.
