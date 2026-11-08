# ADR-020 — Multi-session UEs: a device holds a session on every slice it is permitted on

**Status:** Accepted, 2026-11-08
**Builds on:** ADR-011 (a gNB, UPF and data network per slice), ADR-016 (slice orchestrator),
ADR-017 (network orchestrator), ADR-019 (packet scheduler)
**Replaces:** the "least-busy device" shortcut in the slice orchestrator's flow placement

## Context

Subscribers have always been *permitted* on more than one slice: eMBB devices on URLLC and URLLC
devices on eMBB (`SLICE_<X>_ALSO_PERMITTED`), so the slice orchestrator has a real choice. But each
device held a PDU session on its home slice only, and each gNB served only its own slice. When the
orchestrator placed a demand on the *other* slice, the packets could not leave from the requesting
device. They were sent instead to the least-busy device already on that slice:

> an admitted flow therefore runs on the least-busy device of the CHOSEN slice — the requesting
> subscriber must still be permitted on that slice, but the packets need not leave from that
> subscriber's own device. (orchestrator.py, before this ADR)

That is the right decision on the wrong device. In 3GPP a device holds several PDU sessions at
once, one per S-NSSAI/DNN it needs, and its route-selection policy (URSP) sends each application's
traffic into the matching one.

## Decisions

### 1. Every eMBB and URLLC device holds two sessions

The UE configuration lists the home session first, then one session per slice in
`SLICE_<X>_ALSO_PERMITTED`, in that order. Sessions are requested in config order, so the home
session is always **PDU session 1** (PSI 1). The interface names `uesimtun<NNNN>0/1` say whose
session it is (the last four digits of the IMSI) but **not** which one: UERANSIM numbers interfaces
in the order sessions finish setting up (`NextInterfaceName`), and a URLLC device's eMBB session was
found on `…0`. Which session an interface carries is read from the UE's own log
("PDU session[N] is successful, TUN interface[…]"): `ue-slice.sh sessions` lists every live session
as `<imsi> <psi> <interface> <address> home|second`. mMTC devices stay single-session.

| Device | Sessions | Pools |
|---|---|---|
| eMBB (20) | PSI 1 eMBB (home), PSI 2 URLLC | 10.45/16, 10.46/16 |
| URLLC (10) | PSI 1 URLLC (home), PSI 2 eMBB | 10.46/16, 10.45/16 |
| mMTC (70) | PSI 1 mMTC | 10.47/16 |

That is 130 PDU sessions: eMBB 30, URLLC 30, mMTC 70.

### 2. The eMBB and URLLC cells carry both slices; each session keeps its own user plane

A UERANSIM UE camps on one cell, so a second slice's session must be carried by the cell the device
is on: the eMBB and URLLC gNBs now advertise both S-NSSAIs. The SMF already selects the UPF by DNN,
so a URLLC session through the eMBB gNB still goes to the **URLLC UPF** and the URLLC data network.
Only the radio side is shared — which is what RAN slicing is: one cell, several slices, and a
scheduler that keeps them apart. That scheduler is ADR-019's, and it takes a packet's slice from the
packet itself, not from the gNB it came through.

### 3. "A device of a slice" means its home session

Every tool that counts devices per slice — the UE supervisor, the traffic agent, the probe exporter,
the KPI runner, the network orchestrator's data-path check, acceptance and `start_ues.sh` — counts
home sessions (PSI 1) only. Otherwise URLLC would suddenly report 30 devices, 20 of them
eMBB phones, and every per-slice KPI would change meaning. The core's counts include both: the gate
expects 30 sessions on eMBB and on URLLC.

### 4. The orchestrator uses the requester's own session

`pick_device()`: the flow runs on the requesting subscriber's own session on the chosen slice;
only a subscriber with no session there (an IoT device, or a session that is down) falls back to the
least-busy device. `slice_flow_device_total{kind="own"|"other"}` counts both.

### 5. The second session is opened on demand, not configured

UERANSIM re-opens every session in a UE's `sessions:` list as soon as it is released
(`NasSm::establishRequiredSessions`, `ue/nas/sm/resource.cpp`). A configured second session could
therefore never be kept down: released, it came back 0.8 s later, and the UPF of the slice being
switched off stopped under 20 fresh sessions that the core kept. So the home session is the only
default session; the others are listed under `extraSessions:` (a key UERANSIM ignores) and opened by
the UE container with `nr-cli ps-establish` once every UE is up, 10 at a time, and kept up by the
UE supervisor (at most one attempt a minute per UE). This is also closer to how a phone works: a
session to another slice is opened when an application needs it (URSP).

### 6. The lifecycle handles second sessions

Deactivating a slice now also **releases** the other slices' devices' second sessions on it
(`nr-cli ps-release <psi>`), before its UPF stops, after marking its DNN released so that nothing
re-opens them; those devices stay registered and keep their home session. Activation
**re-establishes** them (`nr-cli ps-establish IPv4 --sst … --sd … --dnn …`) and pings the gateway from
a sample of them. `ue-slice.sh release|establish <slice>`; netorch steps "second sessions".

### 7. CI pins it

`tools/check_open5gs_slices.py`: the home session is a UE config's only default session, its
`extraSessions` are exactly the slices its `ALSO_PERMITTED` names, in order, and every session is
advertised by the UE's gNB and routed to a UPF with that DNN's pool.

## Results

Measured on the live stack with the packet scheduler running
([evidence](../evidence/open5gs-multi-session/README.md)):

| | Result |
|---|---|
| Sessions | 130 live: eMBB 20 home + 10 second, URLLC 10 home + 20 second, mMTC 70; the SMF counts 30 / 30 / 70 |
| Data path | 130 of 130 sessions reach their own UPF |
| Placement on the other slice | 6 of 6 ran on the requester's own session (`kind="own"`); an IoT device was refused |
| Deactivate URLLC | 10.8 s: URLLC devices deregistered, the eMBB phones' URLLC sessions released, the phones stayed registered (20/20); core URLLC sessions 0 |
| Activate URLLC | 34.5 s: 10/10 URLLC devices, 20/20 second sessions back (3/3 sampled reach the URLLC UPF); core 30 / 30 / 70 |
| Start-up | 100 UEs + 30 second sessions in 46 s, 0 supervisor restarts |
| Acceptance | PASS=24 FAIL=0; KPI gate 24 passed, 0 failed |

### A wrong assumption, found on the live stack

The first version took "interface ending in 0" to mean the home session. On the live stack 18 eMBB
and 12 URLLC "home" sessions were counted instead of 20 and 10, and the UE supervisor restarted
healthy UEs: URLLC device 025's `uesimtun00250` carried its *eMBB* session, so the supervisor pinged
the URLLC gateway through an eMBB interface and got no reply. UERANSIM allocates interface names in
setup order. Every tool now reads the PSI from the UE's log instead.

## Consequences

- (+) An admitted demand runs on the device that asked for it, on the slice the orchestrator chose.
- (+) The deployment now looks like 3GPP: devices with several PDU sessions, cells serving several
  slices, and a scheduler separating them on the cell.
- (−) A URLLC session of an eMBB phone shares the eMBB gNB **process** with eMBB traffic. The cell
  scheduler orders packets on the link, but the gNB's CPU and sockets are shared. URLLC's own
  devices keep their dedicated gNB (ADR-011), so URLLC's device KPIs are unaffected.
- (−) Deactivating eMBB or URLLC still stops that slice's own devices, because they camp on its gNB;
  only the *other* devices' sessions are released cleanly. A cell serving two slices could keep its
  devices registered — a bigger change to the per-slice-gNB layout, not made here.
- (−) Which traffic goes into which session is still chosen by the orchestrator per demand, from the
  declared traffic class, not by a URSP rule on the device.
- (−) Opening a session on demand is slow in UERANSIM: the first request often fails its access
  check ("considering access attempt is barred") and is retried after T3580 (16 s), so re-opening 20
  second sessions takes ~20 s of an activation.
