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
| `deploy-20260925-045501Z.txt` | **Phase 2b: third slice (mMTC) and 100 UEs** — 20 files changed, 4 new (ADR-014) | UPFs stopped while the core restarted; 100/100 attached in 31 s; gate 22 passed, 0 failed; **deployed**, known-good updated | 0 |
| `deploy-20260925-054450Z.txt` | none (`--init`) | known-good re-recorded after the proc-table fix in the UE entrypoint, the scale test, the rebuild and the Kubernetes run (37 files) | 0 |

| `deploy-20261001-123845Z.txt` | Phase 2b Tracks 2–4: KPI collectors in the URLLC/mMTC data networks, traffic agent, a new Prometheus job | **reported DEPLOYED, but nothing was applied.** The `docker compose` plugin was gone (Docker Desktop's engine was stopped), `compose up` failed unseen, and the gate passed on the old containers. Prometheus was also never restarted, so the new scrape job was not loaded. Found only because the new metrics were missing. | 0 (wrong) |
| `deploy-20261001-124758Z.txt` | same candidate, after the fix | `deploy.sh` now checks that `compose` works before touching anything. It treats a failed `compose up` as a failed apply, and restarts Prometheus on every apply. **ABORTED — this host cannot apply a candidate right now; the running stack is unchanged** | 2 |

The data-network containers were then recreated to match the compose file, by hand (`docker run`
with the compose labels), after a fallback to the standalone Compose v1.29 crashed on this Docker
engine (`KeyError: 'ContainerConfig'`). That crash had left them stopped and renamed. Never use
`docker-compose` v1 on this host.

The validator reads every YAML file as a multi-document stream: the generated Kubernetes manifest
(`deployments/open5gs/k8s/generated/o5gs.yaml`) is one, and a single-document parse rejected
every candidate once that file existed.
