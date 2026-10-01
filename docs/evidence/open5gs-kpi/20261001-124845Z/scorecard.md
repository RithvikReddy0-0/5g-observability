# KPI run — 20261001-124845Z

## Scorecard — standard instance (150 s, all three slices at once)

| Slice | KPI | Measured | Target | Industry benchmark | Result | Score |
|---|---|---|---|---|---|---|
| URLLC | End-to-end user-plane latency, mean (one-way, UE to destination) | 3.934 ms | 10 ms | 1 ms (ITU-R M.2410) | ✅ met | 1.00 |
| URLLC | Latency, 95th percentile | 11.438 ms | 10 ms | 10 ms (3GPP TS 23.501 Table 5.7.4-1) | ❌ missed | 0.87 |
| URLLC | Latency, 99th percentile | 22.039 ms | 10 ms | 10 ms (3GPP TS 23.501 Table 5.7.4-1) | ❌ missed | 0.45 |
| URLLC | Packet loss | 0 % | 0.01 % | 0.01 % (3GPP TS 23.501 Table 5.7.4-1) | ✅ met | 1.00 |
| URLLC | Jitter (mean change in latency between consecutive packets of a device) | 2.763 ms | — | — | reported | — |
| eMBB | 5th-percentile user throughput, downlink | 10.83 Mbps | 50 Mbps | 100 Mbps (ITU-R M.2410) | ❌ missed | 0.22 |
| eMBB | Aggregate downlink throughput of the slice | 299.3 Mbps | 500 Mbps | 550 Mbps (Research papers) | ❌ missed | 0.60 |
| eMBB | Mean user throughput, downlink | 14.96 Mbps | — | — | reported | — |
| mMTC | UE registration success rate | 100 % | 99 % | — | ✅ met | 1.00 |
| mMTC | Report loss (10-byte periodic reports) | 0 % | 0.1 % | 0.1 % (Research papers) | ✅ met | 1.00 |
| mMTC | Report latency, 99th percentile (one-way, device to data network) | 18.0 ms | 10000 ms | 10000 ms (3GPP TR 38.913) | ✅ met | 1.00 |
| mMTC | Devices reporting during the measurement | 70 devices | — | — | reported | — |

Slice scores: URLLC 0.90, eMBB 0.37, mMTC 1.00; overall 0.76.

## Sweep — embb_ues (eMBB devices downloading at once)

| embb_ues | URLLC mean / p99 (ms) | URLLC loss | eMBB p5 / aggregate (Mbps) | mMTC reg. success | mMTC loss | URLLC · eMBB · mMTC score |
|---|---|---|---|---|---|---|
| 0 | 2.475 / 8.59 | 0 % | — / — | 100 % | 0 % | 1.00 · — · 1.00 |
| 5 | 2.867 / 14.606 | 0 % | 46.38 / 357.7 | 100 % | 0 % | 0.95 · 0.84 · 1.00 |
| 10 | 2.683 / 16.421 | 0 % | 12.64 / 302.6 | 100 % | 0 % | 0.94 · 0.39 · 1.00 |
| 20 | 2.942 / 15.167 | 0 % | 7.03 / 275.1 | 100 % | 0 % | 0.95 · 0.30 · 1.00 |

## Sweep — mmtc_interval (seconds between each IoT device's reports)

| mmtc_interval | URLLC mean / p99 (ms) | URLLC loss | eMBB p5 / aggregate (Mbps) | mMTC reg. success | mMTC loss | URLLC · eMBB · mMTC score |
|---|---|---|---|---|---|---|
| 1 | 2.487 / 11.872 | 0 % | 7.22 / 254.6 | 100 % | 0 % | 0.98 · 0.29 · 1.00 |
| 5 | 2.301 / 10.458 | 0 % | 7.04 / 261.7 | 100 % | 0 % | 0.99 · 0.29 · 1.00 |
| 10 | 3.404 / 18.807 | 0 % | 9.04 / 283.2 | 100 % | 0 % | 0.93 · 0.34 · 1.00 |
| 30 | 3.608 / 19.759 | 0 % | 5.49 / 299.5 | 100 % | 0 % | 0.92 · 0.31 · 1.00 |

## Sweep — mmtc_devices (IoT (mMTC) devices attached)

| mmtc_devices | URLLC mean / p99 (ms) | URLLC loss | eMBB p5 / aggregate (Mbps) | mMTC reg. success | mMTC loss | URLLC · eMBB · mMTC score |
|---|---|---|---|---|---|---|
| 70 | 2.836 / 17.198 | 0 % | 10.89 / 423.9 | 100 % | 0 % | 0.94 · 0.47 · 1.00 |
| 140 | 1.997 / 10.379 | 0 % | 8.35 / 454.9 | 100 % | 0 % | 0.99 · 0.46 · 1.00 |
| 210 | 2.381 / 16.998 | 0 % | 13.68 / 436.0 | 100 % | 0 % | 0.94 · 0.51 · 1.00 |
