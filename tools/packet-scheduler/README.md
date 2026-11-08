# Packet scheduler (ADR-019)

Classifies every radio packet of the Open5GS stack by its headers and schedules the slices on one
shared cell — what a gNB's MAC scheduler does, at the IP layer of this testbed.
Design and measured results: [ADR-019](../../docs/adr/ADR-019-packet-scheduler.md).

```
make o5gs-scheduler                 # start it (builds o5gs/scheduler:1 the first time)
python3 tools/packet-scheduler/scheduler.py --show     # live class tree, counters, filters
make o5gs-scheduler-test            # the experiment: off vs FIFO cell vs sliced, ARP tiers, share sweep
scripts/open5gs/start_scheduler.sh --stop              # remove the queues (unshaped link)
```

## What it reads in a packet

UERANSIM's radio link is UDP port 4997 (RLS). Offsets from the outer IP header:

| Offset | Field | Used for |
|---|---|---|
| 9 / 20 or 22 | IP protocol / UDP source or destination port | is it radio traffic, which direction |
| 32 | RLS message type (4 heartbeat, 5 ack, 6 PDU, 7 PDU ack) | signalling vs PDU |
| 41 | PDU type (1 RRC, 2 data) | signalling vs user data |
| 54 | the UE's own IP packet | — |
| 66 / 70 | its source / destination address | slice (by pool) and ARP tier (by address) |

## Classes

| Class | Prio | Guaranteed | Queue |
|---|---|---|---|
| signalling (heartbeats, acks, RRC/NAS) | 0 | `SCHED_SIGNALLING_SHARE` | pfifo 256 |
| URLLC critical (ARP 1) | 1 | URLLC share × 3/10 | pfifo 64 |
| URLLC standard (ARP 2) | 2 | URLLC share × 7/10 | pfifo 64 |
| eMBB | 3 | `SLICE_A_SCHED_SHARE` | fq_codel |
| mMTC | 4 | `SLICE_C_SCHED_SHARE` | fq_codel |
| other (scrapes, unknown) | 6 | `SCHED_DEFAULT_SHARE` | fq_codel |

All settings live in `deployments/slices.env`. At run time:

```
curl -s -XPOST localhost:9114/config -d '{"mode":"fifo"}'                  # sliced | prio | drr | hfsc | fifo | off
curl -s -XPOST localhost:9114/config -d '{"directions":["uplink"]}'        # schedule one direction only
curl -s -XPOST localhost:9114/config -d '{"cell_dl_mbps":150}'
curl -s -XPOST localhost:9114/config -d '{"shares":{"eMBB":50,"mMTC":15}}'
curl -s -XPOST localhost:9114/config -d '{"prio":{"mMTC":3}}'
```

Run-time changes are not persisted: a restart returns to `slices.env`.

`prio` (strict priority), `drr` (deficit round robin) and `hfsc` (real-time curves for signalling and
URLLC) keep the same classifier and classes and only change the tree (ADR-021,
`docs/evidence/open5gs-scheduler-algorithms/`). `sliced` (HTB) stays the default: prio caused mass
radio-link failures under overload, DRR added ~2 ms of URLLC median latency under load, HFSC delivered
less eMBB at nominal load. `python3 scheduler.py --print <mode>` shows any mode's tc commands.

## Files

| File | |
|---|---|
| `scheduler.py` | the daemon: finds the UE container and its host veth, installs HTB + u32 in both directions, re-applies on change, `/metrics` `/state` `/config` on :9114 |
| `test_scheduler.py` | packets built byte for byte as UERANSIM builds them, plus a real capture (`tests/fixtures/rls-sample.pcap`); CI runs it |
| `experiment.py` | the live experiment behind ADR-019's results |
| `../../deployments/open5gs/images/scheduler.Dockerfile` | pinned Python + iproute2 + nsenter |
