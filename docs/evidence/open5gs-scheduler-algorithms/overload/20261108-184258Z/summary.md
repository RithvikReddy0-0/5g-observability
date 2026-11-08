# Packet scheduler — measured on the live stack (ADR-019)

Run 20261108-184258Z, 60 s per measurement. Cell, load and probes as described in `tools/packet-scheduler/experiment.py`. RTT is ping from the device's own PDU-session interface to its UPF (ms); loss is lost pings. Delivered is what the devices received.

## D — isolation where the cell is the bottleneck: eMBB offers 150 Mbps into a 100 Mbps cell

| Run | URLLC critical p50 / p99 / max | URLLC standard p50 / p99 / max | loss crit / std | mMTC p99 | eMBB unloaded p99 | eMBB delivered | radio-link failures | UEs left without data path |
|---|---|---|---|---|---|---|---|---|
| round 1 off | 3.07 / 58.41 / 98.9 | 3.32 / 58.508 / 93.7 | 0.0 % / 0.0 % | 50.816 | 4634.61 | 149.32 Mbps | 0 | 0 |
| round 1 fifo | 104.0 / 116.38 / 147.0 | 104.0 / 144.5 / 174.0 | 64.33 % / 32.37 % | 129.39 | None | 46.22 Mbps | 98 | 126 |
| round 1 sliced | 17.9 / 105.02 / 177.0 | 17.6 / 108.01 / 176.0 | 0.0 % / 0.0 % | 85.703 | 64180.63 | 96.03 Mbps | 0 | 0 |
| round 1 prio | 2.44 / 18.602 / 99.3 | 2.425 / 20.006 / 106.0 | 0.0 % / 0.0 % | None | 179.43 | 49.79 Mbps | 88 | 110 |
| round 1 drr | 4.2 / 13.9 / 41.0 | 4.16 / 13.703 / 33.2 | 0.0 % / 0.0 % | 11.555 | 57.464 | 93.4 Mbps | 0 | 0 |
| round 1 hfsc | 1.89 / 21.201 / 85.3 | 1.915 / 21.802 / 68.7 | 0.0 % / 0.0 % | 30.606 | 263.81 | 93.3 Mbps | 0 | 0 |
| round 2 hfsc | 1.91 / 10.006 / 70.6 | 1.9 / 9.802 / 54.6 | 0.0 % / 0.0 % | 7.233 | 35.486 | 93.38 Mbps | 0 | 0 |
| round 2 drr | 3.97 / 14.701 / 68.0 | 3.99 / 13.902 / 75.0 | 0.0 % / 0.0 % | 15.024 | 122.88 | 93.39 Mbps | 0 | 0 |
| round 2 prio | 2.015 / 23.954 / 29.0 | 2.065 / 21.584 / 36.8 | 94.81 % / 94.89 % | None | 100.15 | 68.92 Mbps | 180 | 130 |
| round 2 sliced | 1.82 / 5.54 / 11.9 | 1.82 / 5.8 / 16.3 | 0.0 % / 0.0 % | 5.255 | 9.516 | 93.08 Mbps | 0 | 0 |
| round 2 fifo | 104.0 / 106.0 / 110.0 | 104.0 / 106.64 / 108.0 | 10.19 % / 94.93 % | 106.0 | 108.0 | 61.52 Mbps | 97 | 124 |
| round 2 off | 2.03 / 5.33 / 55.0 | 2.035 / 6.09 / 51.1 | 0.0 % / 0.0 % | 4.341 | 8.246 | 150.0 Mbps | 0 | 0 |

