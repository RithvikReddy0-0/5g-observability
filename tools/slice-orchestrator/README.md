# Slice orchestrator — demand-based slice allocation

This is the piece the project originally set out to build: **traffic arrives, and it is placed
on whichever slice can actually serve it — refusing it when no suitable slice has capacity
left.**

```bash
make traffic      # generate demands and watch them allocated
make slices       # current capacity, utilisation and decisions
```

## What is real 3GPP, and what is not

This distinction matters more than anything else here, so it is stated first.

| Layer | Who does it | Real? |
|---|---|---|
| Subscriber is permitted on a set of slices | free5GC UDR/UDM | ✅ real, provisioned in MongoDB |
| Slice QoS: 5QI, ARP, AMBR | free5GC subscriber record | ✅ real 3GPP parameters |
| UE requests a slice, NSSF authorises it | free5GC NSSF | ✅ real, over the SBI |
| Choosing *which* permitted slice a demand goes to | **this service** | ⚠️ orchestration, above the core |
| Refusing demands when a slice is full | **this service** | ⚠️ models NSACF-style admission control |
| Carrying the actual user packets | UPF | ❌ **absent** — needs gtp5g |

**Standard 5G does not pick a slice for you based on load.** The UE requests a slice from the
set it is subscribed to, and the network authorises or refuses it. The closest thing in the
specification to what this service does is **NSACF** (Network Slice Admission Control
Function), which caps how many UEs and PDU sessions a slice will accept — free5GC v4.2.3 does
not implement it. Anything more dynamic belongs in orchestration/SMO, which is exactly where
this sits.

**No user traffic flows.** These are demands for capacity, not packets. Capacity is modelled
from the AMBR genuinely provisioned into each slice's subscribers, not measured from a live
user plane, because there is no UPF on a host without gtp5g.

## What keeps it honest

- Slice parameters are read from [`deployments/slices.env`](../../deployments/slices.env) —
  **the same file that provisions the subscribers**, so the orchestrator cannot drift from
  what the core actually has.
- The UE → permitted-slices mapping is read from the **live subscriber database**. A UE can
  only be placed on a slice it is genuinely provisioned for.
- Every refusal names a real reason: the slice was full, or no permitted slice met the
  latency requirement.

## The two slices are genuinely different services

Not two labels — different service types with different QoS:

| | Slice A | Slice B |
|---|---|---|
| Name | eMBB | URLLC |
| S-NSSAI | SST **1** / SD 010203 | SST **2** / SD 112233 |
| 5QI | 9 (non-GBR, best effort) | 82 (delay-critical GBR) |
| ARP priority | 8 (low) | 2 (high, may preempt) |
| Session AMBR | 200 Mbps | 20 Mbps |
| Latency budget | 300 ms | 10 ms |

Both are advertised by the AMF, SMF, NSSF and gNB, and provisioned into real subscriber
records. Verify with:

```bash
docker exec -i mongodb mongo free5gc --quiet --eval \
  'printjson(db["subscriptionData.provisionedData.smData"].findOne({ueId:"imsi-208930000000011"}))'
```

## How a demand is allocated

1. **Look up the traffic class** — what bandwidth and latency does it need?
2. **Which slices is this subscriber permitted on?** (from the live core, default + additional)
3. **Which of those can meet the latency budget?**
4. **Which of those still has capacity?**
5. **Pick the cheapest adequate slice** — the loosest latency guarantee that still works.

Step 5 is the important one. Video tolerates 300 ms, so it belongs on the 200 Mbps eMBB
slice; putting it on the 20 Mbps URLLC slice would burn scarce low-latency capacity that
control traffic genuinely needs. An earlier version of this code minimised spare headroom
instead, which packed video onto URLLC and starved control traffic — a real bug, fixed.

Traffic classes are defined in `slices.env`, not hardcoded here:

```
video:8:300    file:15:300    blog:1:20    control:1:10
        │  └ max latency ms
        └ Mbps needed
```

## Metrics

| Metric | Meaning |
|---|---|
| `slice_capacity_mbps{sst,sd,slice}` | capacity, from provisioned AMBR |
| `slice_allocated_mbps{sst,sd,slice}` | currently admitted bandwidth |
| `slice_utilization_ratio{sst,sd,slice}` | allocated ÷ capacity (1.0 = full) |
| `slice_active_flows{sst,sd,slice}` | demands currently holding capacity |
| `slice_admitted_total{sst_sd,traffic_class}` | admissions, by class and slice |
| `slice_rejected_total{sst_sd,traffic_class,reason}` | refusals — `capacity`, `latency`, … |

Prometheus scrapes it at `10.100.200.1:9110` (compose network gateway — `host.docker.internal`
does not resolve there). Dashboard row: **"Slice allocation"**.

Admitted demands hold capacity for `FLOW_TTL` seconds (default 45) and then expire, so
utilisation rises and falls rather than only climbing.

## Example

```
ADMIT  file     0018 -> eMBB   (sst 1/010203)  15.0/200 Mbps
ADMIT  control  0002 -> URLLC  (sst 2/112233)   1.0/20 Mbps
ADMIT  blog     0003 -> URLLC  (sst 2/112233)   2.0/20 Mbps
ADMIT  video    0009 -> eMBB   (sst 1/010203)  53.0/200 Mbps
REFUSE video    0013 -> slice 1/010203 (eMBB) full: 193.0/200.0 Mbps used, video needs 8.0
```

Bulk traffic on the big slice, latency-sensitive traffic on the small one, and refusals once
a slice fills. That is demand-based allocation against real, finite resources.

## Open5GS mode — measured capacity, real traffic

On the Open5GS stack ([`docs/open5gs.md`](../../docs/open5gs.md)) the same decision logic runs
with a working user plane:

```bash
make o5gs-orchestrate    # demands at RATE/s for DURATION s, each admitted one run as a real flow
make o5gs-slices         # capacity, admitted, MEASURED load, delivered flows
scripts/open5gs/orchestrator_demo.sh   # the two experiments below, saved as evidence
```

| | free5GC mode | Open5GS mode |
|---|---|---|
| Subscribers and permitted slices | free5GC database | Open5GS database |
| Capacity counted | admitted demand | **max(admitted, measured at each slice's UPF)** |
| Admitted demand | a reservation | **a real UDP flow at that bitrate**, device ↔ gNB ↔ UPF ↔ data network |
| Result per demand | none | delivered Mbps and loss; `met` = ≥ 95 % of the rate with ≤ 2 % loss |

**Boundary:** each device holds a session on its home slice only, and a slice's gNB serves only
that slice (ADR-011). An admitted flow therefore runs on the least-busy device *of the chosen
slice*. The requesting subscriber must still be permitted on that slice.

**Premium capacity is protected.** A demand is placed only on the least specialised slices that
meet its latency need and is refused if they are full. Found on real traffic: with eMBB full,
video was admitted onto URLLC and took 16 of its 20 Mbps. `SPILLOVER=1` restores that behaviour.

Measured ([`docs/evidence/open5gs-orchestrator/`](../../docs/evidence/open5gs-orchestrator/README.md)):

| Experiment | Result |
|---|---|
| 3 demands/s for 120 s | both slices admitted to exactly 200/200 and 20/20 Mbps; 15 refused; **164 of 165 flows delivered their rate** |
| eMBB flooded by 483 Mbps the orchestrator never admitted | every eMBB demand refused on measured load alone, none spilled onto URLLC; admitted again once the flood ended |
| Before the data-path fixes (ADR-012) | 0 of 78 eMBB flows delivered — the reason those fixes exist |

`CAPACITY_OVERRIDE="eMBB=… URLLC=…"` admits against what a host can actually deliver when that is
below policy; `scripts/open5gs/calibrate_capacity.sh` measures it. This laptop delivers the full
policy capacity, so it is not set.

**Phase 2b — three slices** ([ADR-014](../../docs/adr/ADR-014-mmtc-slice-and-100-ues.md)). The mMTC
slice (SST 3) is defined in `slices.env`, so the orchestrator loads it, and `SLICE_PATHS` knows its
pool and data network. Its 70 IoT subscribers are permitted on mMTC only. The traffic generator's
mix (video, files, browsing, control) is phone-like traffic, so `traffic_gen.py` drives only
subscribers permitted on SST 1 or 2 (`SUBSCRIBER_SSTS`). IoT devices get their own traffic profile
(Track 2). Which traffic classes map to mMTC, and priority within URLLC, are the shared
orchestrator work of Phase 2b and are not decided here.

## Honest limitations

- **free5GC mode: capacity is modelled, not measured.** Without a UPF nothing enforces AMBR; these are
  bookkeeping decisions against provisioned numbers. Open5GS mode measures it (above).
- **This is not a 3GPP function.** Do not present it as something free5GC does.
- **Allocation is per demand, not per PDU session.** A real implementation would tie each
  admission to an actual PDU session, which requires the user plane.
- Admission from measured throughput is implemented in Open5GS mode. A slice *aggregate* is still
  enforced only by admission: the gNB polices each session's AMBR, and an unmanaged TCP flood reached
  542 Mbps on the 200 Mbps eMBB slice.
