# Packet scheduler — measured on the live stack (ADR-019)

Run 20261108-195102Z, 60 s per measurement. Cell, load and probes as described in `tools/packet-scheduler/experiment.py`. RTT is ping from the device's own PDU-session interface to its UPF (ms); loss is lost pings. Delivered is what the devices received.

## D — isolation where the cell is the bottleneck: eMBB offers 150 Mbps into a 100 Mbps cell

| Run | URLLC critical p50 / p99 / max | URLLC standard p50 / p99 / max | loss crit / std | mMTC p99 | eMBB unloaded p99 | eMBB delivered | radio-link failures | UEs left without data path |
|---|---|---|---|---|---|---|---|---|
| round 1 hfsc | 1.86 / 8.81 / 47.9 | 1.86 / 10.411 / 50.2 | 0.0 % / 0.0 % | 7.071 | 46.249 | 93.36 Mbps | 0 | 0 |
| round 1 drr | 3.59 / 7.39 / 15.8 | 3.57 / 7.282 / 14.4 | 0.0 % / 0.0 % | 7.531 | 7.528 | 90.85 Mbps | 0 | 0 |
| round 1 sliced | 1.83 / 4.94 / 9.36 | 1.84 / 5.42 / 9.53 | 0.0 % / 0.0 % | 4.258 | 9.615 | 92.46 Mbps | 0 | 0 |
| round 1 off | 2.02 / 5.87 / 17.7 | 2.02 / 5.39 / 21.3 | 0.0 % / 0.0 % | 5.039 | 8.689 | 150.0 Mbps | 0 | 0 |
| round 2 off | 1.76 / 4.72 / 9.89 | 1.75 / 4.74 / 9.64 | 0.0 % / 0.0 % | 4.926 | 6.548 | 150.0 Mbps | 0 | 0 |
| round 2 sliced | 1.57 / 6.551 / 15.2 | 1.58 / 6.821 / 13.8 | 0.0 % / 0.0 % | 8.059 | 44.799 | 93.4 Mbps | 0 | 0 |
| round 2 drr | 3.63 / 8.41 / 14.5 | 3.65 / 8.62 / 13.1 | 0.0 % / 0.0 % | 7.854 | 24.422 | 92.07 Mbps | 0 | 0 |
| round 2 hfsc | 1.6 / 7.321 / 14.7 | 1.62 / 7.141 / 15.1 | 0.0 % / 0.0 % | 6.713 | 36.066 | 93.36 Mbps | 0 | 0 |

