# KPI run — 20261108-155255Z

## Scorecard — standard instance (150 s, all three slices at once)

| Slice | KPI | Measured | Target | Industry benchmark | Result | Score |
|---|---|---|---|---|---|---|
| URLLC | End-to-end user-plane latency, mean (one-way, UE to destination) | 2.35 ms | 10 ms | 1 ms (ITU-R M.2410) | ✅ met | 1.00 |
| URLLC | Latency, 95th percentile | 6.1 ms | 10 ms | 10 ms (3GPP TS 23.501 Table 5.7.4-1) | ✅ met | 1.00 |
| URLLC | Latency, 99th percentile | 14.532 ms | 10 ms | 10 ms (3GPP TS 23.501 Table 5.7.4-1) | ❌ missed | 0.69 |
| URLLC | Packet loss | 0 % | 0.01 % | 0.01 % (3GPP TS 23.501 Table 5.7.4-1) | ✅ met | 1.00 |
| URLLC | Jitter (mean change in latency between consecutive packets of a device) | 1.641 ms | — | — | reported | — |
| eMBB | 5th-percentile user throughput, downlink | 7.75 Mbps | 50 Mbps | 100 Mbps (ITU-R M.2410) | ❌ missed | 0.15 |
| eMBB | Aggregate downlink throughput of the slice | 218.8 Mbps | 500 Mbps | 550 Mbps (Research papers) | ❌ missed | 0.44 |
| eMBB | Mean user throughput, downlink | 10.94 Mbps | — | — | reported | — |
| mMTC | UE registration success rate | 100 % | 99 % | — | ✅ met | 1.00 |
| mMTC | Report loss (10-byte periodic reports) | 0 % | 0.1 % | 0.1 % (Research papers) | ✅ met | 1.00 |
| mMTC | Report latency, 99th percentile (one-way, device to data network) | 10.0 ms | 10000 ms | 10000 ms (3GPP TR 38.913) | ✅ met | 1.00 |
| mMTC | Devices reporting during the measurement | 70 devices | — | — | reported | — |

Slice scores: URLLC 0.95, eMBB 0.27, mMTC 1.00; overall 0.74.
