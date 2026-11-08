# URLLC reliability — one long standard instance, 2026-11-08

**Question:** does the URLLC slice meet the 99.999 % reliability of 3GPP TS 22.261, and can a
testbed run show it?

**Answer:** for *delivery*, yes, at 95 % confidence: 331 237 packets, none lost, so the loss
probability is below 9.1 × 10⁻⁶ (rule of three). For delivery *within a latency bound* — which is
what TS 22.261 calls reliability — no: 2 263 packets (0.68 %) arrived later than the slice's 10 ms
budget, and 125 (0.038 %) later than 30 ms. The measured reliability within 10 ms is 99.32 %
(95 % CI 99.29–99.35 %), three orders of magnitude short of 99.999 %.

## Definition used

TS 22.261 (V17.12.0, clause 3.1): reliability is the "percentage value of the packets successfully
delivered to a given system entity within the time constraint required by the targeted service out
of all the packets transmitted". So a packet fails if it is lost **or** late. Two time constraints:

- **10 ms** — this slice's own budget (`SLICE_B_MAX_LATENCY_MS`, the packet delay budget of the
  delay-critical 5QI it uses; for delay-critical GBR flows TS 23.501 counts a packet delayed beyond
  the PDB as lost);
- **30 ms** — the tightest TS 22.261 row that pairs 99.999 % reliability with a latency: Table
  7.2.3.2-1, wireless road-side infrastructure backhaul (max. end-to-end latency 30 ms, reliability
  99.999 %). The URLLC numbers of TS 22.261 itself point to TS 22.104 for most vertical scenarios.

Showing 99.999 % with zero failures at 95 % confidence needs n ≥ 3 / 10⁻⁵ = 300 000 packets (rule
of three: with 0 failures in n trials the one-sided 95 % upper bound of the failure probability is
≈ 3/n). With failures, the interval is the exact Clopper–Pearson interval.

## Run

`20261108-200101Z/` — `STANDARD_DURATION=660 KPI_EVIDENCE=open5gs-urllc-reliability python3
tools/kpi/run_kpis.py standard`: fresh attach of all 100 UEs (133 s), then one standard instance of
660 s instead of 150 s: 20 eMBB TCP downloads for the whole window, URLLC 64 B every 20 ms from each
of 10 devices, mMTC reports. Default scheduler: sliced (HTB), cell 250 / 100 Mbps
(`scheduler-mode.txt`, `scheduler-state.json`). Commit `61f4c5b` plus the collector's 30 ms bound
(committed with this evidence). Afterwards: 0 UEs without a data path.

| | Value |
|---|---|
| URLLC packets sent (by sequence numbers) / received | 331 237 / 331 237 (each of the 10 devices 33 123–33 124, none lost) |
| One-way latency p50 / p95 / p99 / p99.9 / max | 1.25 / 4.30 / 8.24 / 24.1 / 58.6 ms |
| eMBB aggregate / 5th percentile | 222.7 / 11.1 Mbps, 20/20 downloads completed |
| mMTC | registration 100 %, report loss 0 |
| Scores URLLC / eMBB / mMTC, overall | 1.00 / 0.31 / 1.00, 0.77 |

## Reliability ([`20261108-200101Z/reliability.md`](20261108-200101Z/reliability.md), `tools/kpi/reliability.py`)

| A packet fails if | Failures k | n | Measured reliability | 95 % CI of the failure probability | Meets 99.999 %? |
|---|---|---|---|---|---|
| lost | 0 | 331 237 | 100 % | 0 – 1.1 × 10⁻⁵ (one-sided bound 3/n = 9.1 × 10⁻⁶) | **yes**, at 95 % confidence |
| lost or later than 10 ms (slice budget) | 2 263 | 331 237 | 99.317 % | 6.6 × 10⁻³ – 7.1 × 10⁻³ | **no** |
| lost or later than 20 ms | 592 | 331 237 | 99.821 % | 1.7 × 10⁻³ – 1.9 × 10⁻³ | no |
| lost or later than 30 ms (TS 22.261 Table 7.2.3.2-1) | 125 | 331 237 | 99.962 % | 3.1 × 10⁻⁴ – 4.5 × 10⁻⁴ | **no** |
| lost or later than 50 ms | 7 | 331 237 | 99.9979 % | 8.5 × 10⁻⁶ – 4.4 × 10⁻⁵ | no (not shown) |
| lost or later than 100 ms | 0 | 331 237 | 100 % | 0 – 1.1 × 10⁻⁵ | yes, at 95 % confidence |

## Limits of this result

- One run, one host, one load. The URLLC tail varies by a factor of 2–4 between runs on this laptop
  ([`../open5gs-urllc-tail/`](../open5gs-urllc-tail/README.md)): the late-packet counts would differ
  in another run, the conclusion (well below 99.999 % within 10 ms) would not need to.
- Loss is counted from per-device sequence numbers: a packet lost after a device's last received
  packet of the window is not visible (at most one per device here, i.e. 10 in 331 237), and a
  sender restart starts a new epoch (none happened: one epoch per device).
- Latency is one-way uplink, device → data network, through the emulated radio (UERANSIM over UDP),
  gNB, UPF and data network on one host — not a radio interface. The 10⁻⁵ bound is about this
  testbed's software path, not about a real air interface.
- The rule-of-three bound covers loss at this offered load; it says nothing about overload, where a
  FIFO cell lost URLLC packets ([`../open5gs-scheduler-algorithms/`](../open5gs-scheduler-algorithms/README.md)).
