# Slice orchestrator on real traffic (Open5GS)

Each folder is one run of `scripts/open5gs/orchestrator_demo.sh`. Every admitted demand ran as a
real UDP flow at its admitted bitrate; "met" means >= 95 % of the rate delivered with <= 2 % loss.

| Run | What changed before it | Flows met | Refusals | Notes |
|---|---|---|---|---|
| `1-before-fixes-shared-buffers` | nothing | 72 / 150 (URLLC 72/72, **eMBB 0/78**) | 2 | eMBB gNB UDP buffer overflow; test traffic generated inside the UPF; video spilled onto URLLC when eMBB was full |
| `2-after-fixes-2-per-sec-and-measured-load` | ADR-012 fixes, no spill-over | **142 / 142** | 0 (never full at 2/s) | experiment 2: all 6 demands refused on a 483 Mbps unmanaged flood, none spilled onto URLLC |
| `3-after-fixes-3-per-sec-saturation` | same | **164 / 165** | 15 (6 eMBB, 9 URLLC) | both slices at exactly 200/200 and 20/20 Mbps admitted |
