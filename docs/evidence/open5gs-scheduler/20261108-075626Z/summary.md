# Packet scheduler — measured on the live stack (ADR-019)

Run 20261108-075626Z, 60 s per measurement. Cell, load and probes as described in `tools/packet-scheduler/experiment.py`. RTT is ping from the device's own PDU-session interface to its UPF (ms); loss is lost pings. Delivered is what the devices received.

## A — isolation: eMBB offers 400 Mbps into a 200 Mbps cell (the CPU saturates)

| Run | URLLC critical p50 / p99 / max | URLLC standard p50 / p99 / max | loss crit / std | mMTC p99 | eMBB unloaded p99 | eMBB delivered | radio-link failures | UEs left without data path |
|---|---|---|---|---|---|---|---|---|
| idle (sliced, no load) | 2.09 / 6.261 / 16.5 | 2.14 / 6.16 / 22.6 | 0.0 % / 0.0 % | 7.026 | 6.213 | - Mbps | 0 | 0 |
| round 1 off | 1.92 / 27.709 / 78.1 | 1.955 / 31.505 / 91.1 | 0.0 % / 0.0 % | 21.219 | 1931.8 | 386.92 Mbps | 0 | 0 |
| round 1 fifo | None / None / None | 52.3 / 82.19 / 141.0 | 100.0 % / 77.37 % | 53.297 | None | 97.84 Mbps | 95 | 95 |
| round 1 sliced | 1.96 / 27.008 / 75.6 | 1.99 / 28.909 / 97.6 | 0.0 % / 0.0 % | 34.291 | 12456.63 | 192.44 Mbps | 0 | 0 |
| round 2 sliced | 2.3 / 48.404 / 204.0 | 2.23 / 46.905 / 165.0 | 0.0 % / 0.0 % | 35.044 | 12623.44 | 191.18 Mbps | 0 | 0 |
| round 2 fifo | None / None / None | 49.15 / 54.985 / 55.2 | 100.0 % / 99.78 % | 62.537 | 382.02 | 76.49 Mbps | 99 | 99 |
| round 2 off | 1.84 / 30.503 / 89.5 | 1.86 / 29.123 / 104.0 | 0.0 % / 0.0 % | 21.813 | 2673.72 | 392.4 Mbps | 0 | 0 |

## D — isolation where the cell is the bottleneck: eMBB offers 150 Mbps into a 100 Mbps cell

| Run | URLLC critical p50 / p99 / max | URLLC standard p50 / p99 / max | loss crit / std | mMTC p99 | eMBB unloaded p99 | eMBB delivered | radio-link failures | UEs left without data path |
|---|---|---|---|---|---|---|---|---|
| round 1 off | 1.6 / 18.803 / 57.5 | 1.6 / 16.607 / 48.0 | 0.0 % / 0.0 % | 9.654 | 505.95 | 149.95 Mbps | 0 | 0 |
| round 1 fifo | None / None / None | 104.0 / 107.0 / 115.0 | 100.0 % / 11.22 % | 109.54 | 126.34 | 58.23 Mbps | 97 | 97 |
| round 1 sliced | 1.47 / 4.25 / 13.5 | 1.48 / 5.021 / 14.8 | 0.0 % / 0.0 % | 4.329 | 17.937 | 93.39 Mbps | 0 | 0 |
| round 2 sliced | 1.57 / 4.61 / 23.7 | 1.54 / 4.611 / 24.9 | 0.0 % / 0.0 % | 5.469 | 10.386 | 89.97 Mbps | 0 | 0 |
| round 2 fifo | 104.0 / 107.0 / 111.0 | 104.0 / 104.61 / 105.0 | 62.93 % / 98.52 % | 108.46 | 110.51 | 49.88 Mbps | 99 | 99 |
| round 2 off | 1.95 / 4.621 / 19.6 | 1.95 / 4.83 / 22.0 | 0.0 % / 0.0 % | 4.253 | 8.644 | 150.0 Mbps | 0 | 0 |

## B — ARP tiers: six standard URLLC devices flood a 100 Mbps cell

| Run | critical p50 / p99 / max | standard p50 / p99 / max | loss crit / std | URLLC delivered | radio-link failures |
|---|---|---|---|---|---|
| round 1 fifo | 104.0 / 106.0 / 125.0 | 103.0 / 105.0 / 105.0 | 25.63 % / 82.3 % | 77.86 Mbps | 94 |
| round 1 sliced | 1.54 / 3.22 / 8.87 | 7.87 / 9.233 / 12.2 | 0.0 % / 28.04 % | 92.49 Mbps | 0 |
| round 2 sliced | 1.56 / 7.841 / 16.6 | 7.91 / 13.676 / 27.8 | 0.0 % / 23.59 % | 92.22 Mbps | 0 |
| round 2 fifo | 104.0 / 235.04 / 378.0 | 104.0 / 109.0 / 110.0 | 25.93 % / 90.56 % | 62.75 Mbps | 93 |

## C — shares: eMBB 160 + mMTC 40 Mbps offered into a 60 Mbps cell

| mMTC share | mMTC guaranteed | mMTC delivered | eMBB delivered | URLLC critical p99 |
|---|---|---|---|---|
| 5 % | 3.0 Mbps | 3.69 Mbps | 52.44 Mbps | 66.827 |
| 10 % | 6.0 Mbps | 5.6 Mbps | 50.65 Mbps | 17.604 |
| 15 % | 9.0 Mbps | 8.44 Mbps | 47.69 Mbps | 21.702 |
| 20 % | 12.0 Mbps | 11.18 Mbps | 45.04 Mbps | 21.904 |
| 25 % | 15.0 Mbps | 13.98 Mbps | 42.31 Mbps | 19.201 |
