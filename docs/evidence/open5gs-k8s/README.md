# Open5GS on Kubernetes (ADR-013)

`scripts/open5gs/k8s_up.sh` on minikube profile `o5gs` (single node, docker driver, 6 CPU / 4 GiB).

| File | Run | Result |
|---|---|---|
| `k8s-20260914-095105Z.txt` + `-gate.json` | `--verify` on a deployment brought up earlier (that first full run's log was discarded: the script failed at step 6 on its own bug) | PASS — gate 17/17 |
| `k8s-20260914-095517Z.txt` + `-gate.json` | **full run from scratch**: namespace deleted (database included), images loaded, manifests applied, provisioned, verified | PASS — 20 UEs, user plane per slice, gate 17/17 |
| `k8s-20260925-053701Z.txt` + `-gate.json` | **Phase 2b** (ADR-014): three slices and 100 UEs, manifests re-rendered from compose, UERANSIM image with both patches | PASS — 22 pods, 3 gNBs and 3 PFCP associations, 100 UEs (20/10/70), ping + UPF counters + iperf3 per slice (eMBB 252.9, URLLC 25.1, mMTC 1.0 Mbps), gate 22 passed / 0 failed |
