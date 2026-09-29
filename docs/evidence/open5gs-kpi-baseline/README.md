# KPI baseline on the 100-UE stack (Open5GS, 2026-09-29)

Measured to check the Phase 2b KPI targets
([review](../../briefs/phase2b-kpi-targets-review.md)) against what this laptop actually does.
The stack was eMBB 20, URLLC 10 and mMTC 70 UEs, all attached. Host: WSL2, 12 threads, 7.6 GiB.

## eMBB — 20 UEs downloading at once, 20 s

`scripts/open5gs/traffic.sh 20 20 down embb`: one TCP flow per UE, from the eMBB data network
through the eMBB UPF and gNB to each UE's own interface. All flows run at the same time.

```
  eMBB  (session AMBR 200 Mbps)
    UE 10.45.0.10      44.6 Mbps
    UE 10.45.0.11      29.9 Mbps
    UE 10.45.0.12      27.3 Mbps
    UE 10.45.0.13      11.6 Mbps
    UE 10.45.0.14       8.9 Mbps
    UE 10.45.0.15       9.2 Mbps
    UE 10.45.0.16      15.2 Mbps
    UE 10.45.0.17      11.4 Mbps
    UE 10.45.0.18       7.8 Mbps
    UE 10.45.0.19      54.9 Mbps
    UE 10.45.0.2       16.5 Mbps
    UE 10.45.0.20      14.2 Mbps
    UE 10.45.0.21      14.6 Mbps
    UE 10.45.0.3        8.6 Mbps
    UE 10.45.0.4       16.6 Mbps
    UE 10.45.0.5        6.5 Mbps
    UE 10.45.0.6       29.0 Mbps
    UE 10.45.0.7       20.3 Mbps
    UE 10.45.0.8       55.2 Mbps
    UE 10.45.0.9       39.1 Mbps
    total   441.3 Mbps   mean per session   22.1 Mbps
```

| Statistic | Value |
|---|---|
| Aggregate | **441.3 Mbps** |
| Mean per UE | 22.1 Mbps |
| 5th percentile (lowest of 20) | **6.5 Mbps** |
| Best UE | 55.2 Mbps |
| UEs at ≥ 50 Mbps | 2 of 20 |

TCP flows share a bottleneck unevenly, and here the spread is 8×. A 5th-percentile KPI captures
exactly that. The session AMBR (200 Mbps) is not what limits any flow: the laptop's user plane is
the bottleneck.

## URLLC — round-trip time while eMBB was loaded

From the UE-side probe (`ue_probe_rtt_ms`), maximum over the last minute, read straight after the
eMBB run. The probe measures round-trip UE → gNB → UPF gateway every 5 s.

| | Round trip |
|---|---|
| Average, idle (earlier runs) | 2.0–2.5 ms |
| Average, with eMBB fully loaded | **6.9 ms** |
| Worst probe, with eMBB fully loaded | **16.2 ms** |

## mMTC — registration success

From the scale tests ([`../open5gs-scale/`](../open5gs-scale/README.md)): 8 attach runs of 100 UEs
and 4 steps up to 300 UEs, about 800 registrations in all. **0 failed: 100 %.**

One exception, outside those runs: after a Docker engine restart, the first 10 UEs to start
failed registration (`PAYLOAD_NOT_FORWARDED`) because the AMF was not ready yet. Their supervisor
restarted them and they registered. A registration-success KPI must say whether restarts count.
