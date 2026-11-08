# Packet scheduler — measured on the live stack (ADR-019)

Run 20261108-082833Z, 60 s per measurement. Cell, load and probes as described in `tools/packet-scheduler/experiment.py`. RTT is ping from the device's own PDU-session interface to its UPF (ms); loss is lost pings. Delivered is what the devices received.

## D — isolation where the cell is the bottleneck: eMBB offers 150 Mbps into a 100 Mbps cell

| Run | URLLC critical p50 / p99 / max | URLLC standard p50 / p99 / max | loss crit / std | mMTC p99 | eMBB unloaded p99 | eMBB delivered | radio-link failures | UEs left without data path |
|---|---|---|---|---|---|---|---|---|
| round 1 off | 1.6 / 18.803 / 57.5 | 1.6 / 16.607 / 48.0 | 0.0 % / 0.0 % | 9.654 | 505.95 | 149.95 Mbps | 0 | 0 |
| round 1 fifo | None / None / None | 104.0 / 107.0 / 115.0 | 100.0 % / 11.22 % | 109.54 | 126.34 | 58.23 Mbps | 97 | 97 |
| round 1 sliced | 1.47 / 4.25 / 13.5 | 1.48 / 5.021 / 14.8 | 0.0 % / 0.0 % | 4.329 | 17.937 | 93.39 Mbps | 0 | 0 |
| round 2 sliced | 1.57 / 4.61 / 23.7 | 1.54 / 4.611 / 24.9 | 0.0 % / 0.0 % | 5.469 | 10.386 | 89.97 Mbps | 0 | 0 |
| round 2 fifo | 104.0 / 107.0 / 111.0 | 104.0 / 104.61 / 105.0 | 62.93 % / 98.52 % | 108.46 | 110.51 | 49.88 Mbps | 99 | 99 |
| round 2 off | 1.95 / 4.621 / 19.6 | 1.95 / 4.83 / 22.0 | 0.0 % / 0.0 % | 4.253 | 8.644 | 150.0 Mbps | 0 | 0 |

