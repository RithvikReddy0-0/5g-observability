# Deliverable UDP capacity per slice (Open5GS)

`scripts/open5gs/calibrate_capacity.sh`: one UDP downlink flow per device, aggregate rate stepped
up; "delivered" = >= 95 % of the offered rate with <= 2 % loss. Policy capacity (slices.env):
eMBB 200 Mbps, URLLC 20 Mbps.

| Step | Change | eMBB deliverable | URLLC deliverable | File |
|---|---|---|---|---|
| 0 | shared gNB buffers, default TUN queue, traffic generated inside the UPF | not calibrated — the orchestrator admitted 186 Mbps and 0 of 78 eMBB flows got their rate | 20 | `../open5gs-orchestrator/1-before-fixes-shared-buffers` |
| 1 | UERANSIM UDP socket buffers 8 MiB (patch) | **40 Mbps** (80 Mbps: 7.9 % loss, all of it TUN `tx_dropped` at the UPF) | 20 | `1-gnb-udp-buffers-only.txt` (its "delivered" column is the pre-fix sender rate; the loss column is correct) |
| 2 | + UPF TUN txqueuelen 10000 | **120 Mbps** (160 Mbps: 5.95 % loss — iperf3 senders inside the UPF container took ~970 % CPU) | 20 | not kept: the file was overwritten by step 3. Numbers from the run's console output. |
| 3 | + per-slice data-network containers generate the traffic | **200 Mbps** at 0.43 % loss (240 Mbps: 2.05 %) | 20 | `3-plus-tun-queue-and-dn-containers.txt` |

eMBB now delivers its full policy capacity, so no CAPACITY_OVERRIDE is needed on this laptop.
