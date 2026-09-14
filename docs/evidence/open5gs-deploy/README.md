# Deploy behind the KPI gate, with automatic rollback (Open5GS)

`scripts/open5gs/deploy.sh` — one file per run. Known-good and rejected snapshots live in
`.deploy/` (local, git-ignored).

| File | Candidate | What happened | Exit |
|---|---|---|---|
| `deploy-20260914-081601Z.txt` | none (`--init`) | running stack passed the gate; recorded as known-good (33 files) | 0 |
| `deploy-20260914-092219Z.txt` | SMF routes DNN `urllc` to the eMBB UPF | **rejected by static validation**; the running SMF was not restarted | 1 |
| `deploy-20260914-092251Z.txt` | URLLC gNB AMF port typo 38412 → 38413 (passes static checks) | deployed; gate **8 failed** (URLLC gNB, its 10 UEs, its UPF sessions, SMF slice metrics, QoS flows, reachability, latency); **rolled back automatically**; gate 17/17; candidate preserved | 1 |
| `deploy-20260914-092755Z.txt` | AMF network name changed | gate 17/17; **deployed**, known-good updated | 0 |
| `deploy-20260914-100738Z.txt` | none (`--init`) | known-good re-recorded after the gate, orchestrator and Kubernetes changes (33 files) | 0 |

The validator reads every YAML file as a multi-document stream: the generated Kubernetes manifest
(`deployments/open5gs/k8s/generated/o5gs.yaml`) is one, and a single-document parse rejected
every candidate once that file existed.
