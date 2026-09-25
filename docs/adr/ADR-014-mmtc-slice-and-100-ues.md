# ADR-014 — A third slice for IoT (mMTC) and 100 UEs

**Status:** Accepted, 2026-09-25
**Amends:** ADR-011 (topology), ADR-012 (UERANSIM patches)
**Implements:** Phase 2b, Track 1 — [`docs/briefs/phase2b-work-division.md`](../briefs/phase2b-work-division.md)

## Context

Phase 2b asks for 100 UEs in three classes:
- 10 time-sensitive devices (URLLC);
- 20 bandwidth-hungry devices (eMBB);
- 70 IoT devices that send ten bytes periodically and sleep (mMTC).

Each class becomes a slice with its own KPI. Track 1 owns the stack the other tracks measure
against: the third slice, the 100 UEs, and the deployment, gate and CI updated to match.

Until now the Open5GS stack ran two slices with 10 UEs each.

## Decisions

### 1. mMTC is a third slice, dedicated end to end like the other two

| | eMBB | URLLC | **mMTC** |
|---|---|---|---|
| S-NSSAI | SST 1 / 010203 | SST 2 / 112233 | **SST 3 / 334455** (MIoT) |
| UEs, IMSIs | 20, `…001–020` | 10, `…021–030` | **70, `…031–100`** |
| 5QI · ARP | 9 · 8 | 82 · 2 (may pre-empt) | **9 · 12** (lowest; pre-emptable) |
| Session AMBR | 200 Mbps | 20 Mbps | **1 Mbps** |
| Latency budget | 300 ms | 10 ms | **1000 ms** |
| gNB · UPF · DN | .30 · .7 · .51 | .32 · .8 · .52 | **.34 · .9 · .53** |
| DNN · pool · TUN | internet · 10.45/16 · ogstun | urllc · 10.46/16 · ogstun2 | **iot · 10.47/16 · ogstun3** |
| Also permitted on | URLLC | eMBB | **nothing** — IoT devices are mMTC only |

A shared gNB was what broke isolation before (ADR-011): one full UDP buffer drops every slice's
packets. So mMTC gets its own gNB and UPF at negligible cost. 70 devices' signalling now never
reaches the eMBB or URLLC gNB.

### 2. One plan, defined once and checked

`O5GS_UES="A=20 B=10 C=70"` in [`deployments/slices.env`](../../deployments/slices.env) is the
single definition. `SLICE_*_SUBSCRIBERS` stays 10 + 10 for the free5GC stack, which is unchanged.

IMSIs are assigned contiguously in slice order, by provisioning and by the UE container alike. The
UE container's `UE_PLAN` must name the same slices, in the same order, with the same counts.
Otherwise UEs would start under subscribers provisioned for a different slice, with nothing
visibly wrong.

[`tools/check_open5gs_slices.py`](../../tools/check_open5gs_slices.py) fails CI on any mismatch.
Shown on a wrong count and on swapped slices.

### 3. UEs attach in parallel

**Before:** 20 UEs were started one at a time. `nr-ue` names its interface
`<prefix><first free index>`, so UEs starting together raced for `uesimtunN`. At that rate, 100
UEs would take minutes after every engine restart.

**Now:** the UERANSIM v3.3.0 source was read, not guessed at. It already supports a per-UE
interface prefix (`tunName`). Each UE gets `uesimtun<last four IMSI digits>`, so no prefix is
the start of another. Every tool that looks for `uesimtun` still works.

**A second race, found in the same source.** `nr-ue` picks its policy-routing table id by
reading `/etc/iproute2/rt_tables` and appending the first free one. Two UEs starting together
can take the same id. The UE entrypoint now registers every UE's table, with a unique id, before
any UE starts.

**A third race, found by the scale test.** Every `nr-ue` registers itself in
`/tmp/UERANSIM.proc-table/`. It creates that directory if it is missing: a check, then `mkdir`,
and an abort on `EEXIST` (`io::CreateDirectory`). On a fresh container, the UEs of the first
batch race for it and the losers die: 2 of 10, 4 of 100 and 8 of 25 in three bursts. Ordinary
deploys had happened to miss it. The entrypoint now creates the directory first. Five
fresh-container bursts (batch sizes 25, 100, 25, 100, 10) then reached 100/100 every time, with
0 aborts.

`START_BATCH` UEs attach at once (default 10). **100 UEs attach in 25–37 s at 10 at a time, and
in 4–6 s all at once.** There were 0 TUN allocation errors, and 100 routing tables with none
duplicated.

### 4. Radio-link timing uses a monotonic clock (second UERANSIM patch)

The running stack would not recover after an engine restart. Radio link failures arrived in
bursts about 31 s apart, while the VM was idle.

**Cause:** the VM's wall clock was stepped about +1.1 s every 32 s by `systemd-timesyncd`. The
TSC clocksource ran ~3.5 % slow on that boot. UERANSIM times its heartbeats with the wall clock,
and gives up after 2 s.

**Fix:** [`ueransim-monotonic-clock.patch`](../../deployments/open5gs/images/patches/ueransim-monotonic-clock.patch)
moves every interval measurement to `steady_clock`.

**Result:** 6 clock steps in 130 s, 0 radio link failures, 20/20 reachable. Before the patch,
the same conditions left 6 of 20 reachable.

Details: [`evidence/open5gs-clock`](../evidence/open5gs-clock/README.md). Image tag:
`o5gs/ueransim:v3.3.0-udpbuf-mono`. The host clock was not touched, and the patch also
protects against NTP corrections and resume from sleep.

### 5. The SMF never meets a UPF process that is about to be replaced

ADR-011's rule was "restart the UPFs after the SMF". With three UPFs that rule was not enough.

**What happened:**
- The restarted SMF associated over PFCP with the *old* URLLC UPF process 0.7 s after it came up.
- That UPF was restarted a second later.
- For ~20 s the SMF held an association with a process that no longer existed.
- All 10 URLLC sessions created in that window got addresses but never reached the new UPF
  (`Cannot find PFCP-Node`). The UPF counted 20 sessions for 10 UEs, and the gNB logged
  downlink for unknown TEIDs.

The supervisor restored all 10 within 2 minutes, but a deploy should not depend on that.

**Two changes:**
- `deploy.sh` stops the UPFs, restarts the core, then starts the UPFs.
- `start_ues.sh` waits until the SMF has associated with each UPF *since that UPF started*.

Deploying this ADR's own topology through `deploy.sh` then gave 0 supervisor restarts, sessions
of exactly 20 / 10 / 70 at the three UPFs, and no PFCP or TEID errors.

### 6. The deployment gate counts each slice at its own size

`min()` across slices of 20, 10 and 70 can only ever check the smallest. So sessions and UE
reachability are now one gate per slice, at that slice's size. `or vector(0)` makes a slice
that vanishes fail rather than disappear.

The gate now has 24 KPIs (19 enforced, 5 advisory). Added: the third gNB, UPF, PFCP association
and exporter, and an mMTC latency budget of 1000 ms.

These are **deployment** gates: is every slice up and serving its UEs. The per-slice service
KPIs compared against industry targets are Track 3's scorecard, not this file.

## Result

| | Before | Now |
|---|---|---|
| Slices | 2 | **3** (mMTC added) |
| UEs attached | 20 | **100** — eMBB 20, URLLC 10, mMTC 70 |
| Attach time | 20 UEs one at a time | **100 in 35 s** 10 at a time · **7 s** all at once; 0 registration failures |
| Startup races removed | — | TUN name, routing-table id, proc-table directory |
| Where this laptop stops | — | **~300 UEs**: all attach and stay up, but load reaches 18.5 on 12 threads and the liveness probe stops being trustworthy; clean up to 200 |
| UEs losing their user plane to clock steps | 14 of 20 over two minutes | **0** |
| KPI gate | 17 KPIs | **24 KPIs: 22 passed, 0 failed** (2 advisories) |
| User plane, per slice | eMBB, URLLC | **all three**: ping, UPF counters, iperf3; mMTC capped at 1.0 Mbps by its 1 Mbps AMBR |
| Deployed through `deploy.sh` | — | **DEPLOYED**, gate passed, now the known-good |

The cost of scale is measured by
[`scripts/open5gs/scale_test.sh`](../../scripts/open5gs/scale_test.sh)
([`evidence/open5gs-scale`](../evidence/open5gs-scale/README.md)):
- **Attach bursts:** in a registration storm the SCP peaks first (83 % of a core with 100 UEs at
  once), then MongoDB and the AMF.
- **At rest:** holding UEs costs the core nothing measurable. The UE container grows by ~0.6 % of
  a core per UE, most of it simulating and pinging the UEs.

## Consequences

- **The other tracks have their stack.** It holds 100 UEs across three slices, and the gate
  checks every slice at its size.
- **Liveness observation is what limits scale on this laptop.** At 300 UEs every UE still worked,
  but the probe, pinging all of them every 5 s with a 1 s timeout, started reporting live UEs as
  unreachable. Going beyond ~200 UEs needs a sampling probe, UEs spread over more containers or
  hosts, or the ODE.
- **The orchestrator's traffic generator now drives only eMBB and URLLC subscribers**
  (`SUBSCRIBER_SSTS`). Its phone-like traffic mix does not fit IoT devices, which Track 2 drives
  with their own profile. The orchestrator knows the mMTC slice (`SLICE_PATHS`), but assigning
  it traffic classes is the shared orchestrator work.
- **Kubernetes:** the manifests are re-rendered for three slices, and CI checks they are current.
  `render.py` now finds the per-slice UPFs, DNs and gNBs in compose instead of listing them. On
  minikube: 22 pods, 100 UEs, the user plane through each slice, and the same gate at 22 passed,
  0 failed ([evidence](../evidence/open5gs-k8s/README.md)).
- **The free5GC stack is unchanged.**
