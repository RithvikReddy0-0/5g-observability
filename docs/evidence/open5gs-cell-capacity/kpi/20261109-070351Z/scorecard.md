# KPI run — 20261109-070351Z

## Scorecard — standard instance (150 s, all three slices at once)

| Slice | KPI | Measured | Target | Industry benchmark | Result | Score |
|---|---|---|---|---|---|---|
| URLLC | End-to-end user-plane latency, mean (one-way, UE to destination) | 57.93 ms | 10 ms | 1 ms (ITU-R M.2410) | ❌ missed | 0.17 |
| URLLC | Latency, 95th percentile | 8.5 ms | 10 ms | 10 ms (3GPP TS 23.501 Table 5.7.4-1) | ✅ met | 1.00 |
| URLLC | Latency, 99th percentile | 29.179 ms | 10 ms | 10 ms (3GPP TS 23.501 Table 5.7.4-1) | ❌ missed | 0.34 |
| URLLC | Packet loss | 5.202 % | 0.01 % | 0.01 % (3GPP TS 23.501 Table 5.7.4-1) | ❌ missed | 0.00 |
| URLLC | Jitter (mean change in latency between consecutive packets of a device) | 3.081 ms | — | — | reported | — |
| eMBB | 5th-percentile user throughput, downlink | 10.44 Mbps | 50 Mbps | 100 Mbps (ITU-R M.2410) | ❌ missed | 0.21 |
| eMBB | Aggregate downlink throughput of the slice | 214.2 Mbps | 500 Mbps | 550 Mbps (Research papers) | ❌ missed | 0.43 |
| eMBB | Mean user throughput, downlink | 10.71 Mbps | — | — | reported | — |
| mMTC | UE registration success rate | 100 % | 99 % | — | ✅ met | 1.00 |
| mMTC | Report loss (10-byte periodic reports) | 0 % | 0.1 % | 0.1 % (Research papers) | ✅ met | 1.00 |
| mMTC | Report latency, 99th percentile (one-way, device to data network) | 19.0 ms | 10000 ms | 10000 ms (3GPP TR 38.913) | ✅ met | 1.00 |
| mMTC | Devices reporting during the measurement | 70 devices | — | — | reported | — |

Slice scores: URLLC 0.29, eMBB 0.30, mMTC 1.00; overall 0.53.
