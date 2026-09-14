# Open5GS on Kubernetes (ADR-013)

`scripts/open5gs/k8s_up.sh` on minikube profile `o5gs` (single node, docker driver, 6 CPU / 4 GiB).

| File | Run | Result |
|---|---|---|
| `k8s-20260914-095105Z.txt` + `-gate.json` | `--verify` on a deployment brought up earlier (that first full run's log was discarded: the script failed at step 6 on its own bug) | PASS — gate 17/17 |
| the newer `k8s-*Z.txt` + `-gate.json` | **full run from scratch**: namespace deleted (database included), images loaded, manifests applied, provisioned, verified | PASS — 20 UEs, user plane per slice, gate 17/17 |
