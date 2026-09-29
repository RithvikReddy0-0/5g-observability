# Phase 2b — KPI targets: the team's notes, checked against the running stack

The team's KPI notes (Track 3) are photographed below and transcribed. Each target was then
checked against the stack Track 1 built — 100 UEs, three slices — with measurements in
[`evidence/open5gs-kpi-baseline`](../evidence/open5gs-kpi-baseline/README.md).

Source material:
- [Guide's requirements sketch](phase2b-assets/00-guide-requirements-sketch.webp) — 100 UEs; time
  sensitive, more bandwidth, IoT; KPI per class; orchestrator.
- KPI notes, [page 1](phase2b-assets/01-kpi-targets-notes-p1.webp) and
  [page 2](phase2b-assets/02-kpi-targets-notes-p2.webp).

## The notes, transcribed

**Split:** 100 UEs — URLLC 10, eMBB 20, mMTC 70.

**Assumptions**
- Industry-standard values are used only as benchmarks.
- We use Open5GS and UERANSIM, not an industry-grade 5G deployment.
- If the actual 5G architecture in the 5G research lab becomes available, industry-standard
  values can be used.

**Sources**
- ITU-R M.2410 — industry benchmarks.
- 3GPP TS 23.501 Table 5.7.4-1 — 5QI QoS values.
- 3GPP TR 38.913 — mMTC requirements.

**Slice 1 — URLLC: end-to-end / user-plane latency** (time from the source UE to the destination)
- Industry benchmark: 1 ms (ITU-R M.2410).
- Project target: mean latency ≤ 10 ms, with p95/p99 to identify occasional high-delay packets.
- Since the setup runs on a VM/laptop, 10 ms is a practical but only experimental threshold.

**Slice 2 — eMBB: 5th-percentile UE throughput** (exposes the lower end of user performance)
- Industry benchmark: 100 Mbps DL, 50 Mbps UL (ITU-R M.2410).
- Project target: ≥ 50 Mbps DL per UE, for 20 simultaneous UEs, on a software laptop testbed.

**Slice 3 — mMTC: UE registration success rate** (supporting a very large number of devices)
- No universal fixed numerical target.
- Project target: ≥ 99 %.

**Research-based project targets** (papers using the same Open5GS + UERANSIM)

| Slice | KPI | Papers report | Target |
|---|---|---|---|
| URLLC | end-to-end latency | ~2 ms | ≤ 2 ms |
| eMBB | aggregate throughput | 550–900 Mbps | ≥ 500 Mbps |
| mMTC | packet loss | 0.1 % | ≤ 0.1 % |

## Checked against the stack (2026-09-29)

| Slice | Target | Measured | Verdict |
|---|---|---|---|
| URLLC | mean latency ≤ 10 ms | round trip 2.0–2.5 ms idle, **6.9 ms** with eMBB fully loaded | ✅ met |
| URLLC | p95 / p99 | not measurable yet: the probe keeps only min/avg/max per 5 s round | ⚠️ needs a per-packet instrument (Track 2) |
| URLLC | ≤ 2 ms (papers) | 2.0–2.5 ms idle; worst probe **16.2 ms** under eMBB load | ⚠️ borderline idle, missed under load |
| eMBB | ≥ 50 Mbps DL per UE, 20 at once | 5th percentile **6.5 Mbps**, mean 22.1, best 55.2; 2 of 20 UEs ≥ 50 | ❌ not reachable on this laptop |
| eMBB | aggregate ≥ 500 Mbps (papers) | **441.3 Mbps** | ❌ close, short |
| mMTC | registration success ≥ 99 % | ~800 registrations in the scale tests, **0 failed (100 %)** | ✅ met |
| mMTC | packet loss ≤ 0.1 % (papers) | no 10-byte reports sent yet | ⏳ Track 2 |

## Corrections and suggestions

1. **The 1 ms in ITU-R M.2410 is not end-to-end.** It is one-way user-plane latency across the
   radio network only. Call it the user-plane latency benchmark. State what we measure: today, the
   round trip UE → gNB → UPF, roughly twice a one-way figure. Pinging the data network instead
   adds the last hop.
2. **The 10 ms target has a standards basis, not just a practical one.** URLLC runs on 5QI 82,
   whose packet delay budget in TS 23.501 Table 5.7.4-1 is 10 ms — the first source in the notes.
3. **The two eMBB targets contradict each other.** 50 Mbps × 20 UEs = 1 Gbps, while the
   research target is ≥ 500 Mbps aggregate, and this laptop delivers ~440 Mbps. Options:
   - an aggregate target of ~400 Mbps plus a 5th-percentile floor (today 6.5 Mbps);
   - or keep 50 Mbps per UE as a benchmark and report the gap, as the assumptions allow.

   Either way, `SLICE_A_CAPACITY_MBPS=200` in `deployments/slices.env` (what the orchestrator
   admits against) must be raised to match.
4. **Registration success needs a defined window.** It is 100 % in steady state. But after a
   Docker engine restart the first 10 UEs failed because the AMF was not ready yet. The AMF's
   counters are not split by slice, so count mMTC success from the IoT UEs' own logs
   (IMSIs …031–100).
5. **Packet loss ≤ 0.1 % needs ≥ 1000 packets per measurement** to be resolvable. That depends on
   the reporting interval, which is still undecided — 70 devices every 10 s give 1000 packets in
   about 2.5 minutes.
6. **A second mMTC KPI from TR 38.913:** latency for infrequent small packets, no worse than 10 s
   for a 20-byte uplink packet. It matches the 10-byte reports. Verify the clause before citing it.
   The connection-density target (1,000,000 devices/km²) is a benchmark only; it cannot be tested
   here.
7. **Name the papers** behind the research-based targets (~2 ms, 550–900 Mbps, 0.1 %). Reviewers
   will ask.

## Still to decide

- The IoT reporting interval (how often each device sends its 10 bytes).
- Per-slice weights for the scorecard, and which parameters the Excel sweeps vary.
- Whether URLLC priority means sub-classes in one slice or separate slices.
