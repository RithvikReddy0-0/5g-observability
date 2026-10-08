# Network orchestrator — slice lifecycle on the Open5GS stack

The slice orchestrator ([`../slice-orchestrator`](../slice-orchestrator/README.md)) decides how
**traffic** uses the slices. This one manages the **network**: whether a slice exists in a working
state, and with how much compute. In standards terms that is 3GPP slice management (NSMF/NSSMF,
TS 28.531) and an NFV orchestrator, as opposed to admission control above the core.
Design and results: [ADR-017](../../docs/adr/ADR-017-network-orchestrator.md),
[evidence](../../docs/evidence/open5gs-network-orchestrator/README.md).

```bash
make o5gs-network                                         # start it if needed, show every slice
python3 tools/network-orchestrator/netorch.py deactivate mMTC
python3 tools/network-orchestrator/netorch.py activate mMTC
python3 tools/network-orchestrator/netorch.py scale eMBB 1   # CPU quota of eMBB's gNB + UPF; 0 = unlimited
python3 tools/network-orchestrator/netorch.py auto on        # closed loop on the slice orchestrator's /plan
```

## What a slice is, and what each operation does

A slice is its own gNB, UPF and data-network containers (ADR-011), its subscribers' UEs, and its
admission switch in the slice orchestrator.

| Operation | Steps, each checked before the next | On failure |
|---|---|---|
| `activate` | UPF, then wait for PFCP association with the SMF → data network → gNB, then wait for NG Setup → UEs unparked, then wait for their sessions → ping the gateway from sampled UEs → admit | everything started is stopped again (rollback) |
| `deactivate` | stop admitting → drain admitted flows → deregister and stop the UEs → stop gNB, data network, UPF → verify no UE is up and the other slices kept theirs, and read the core's session count | reported as failed |
| `scale` | `docker update --cpus` on gNB and UPF, read back | reported as failed if the quota did not take |

**State is discovered, never remembered:** ACTIVE, DORMANT or DEGRADED, from the containers and the
UEs, on every query. Operations run one at a time.

## Closed loop

`NETORCH_AUTO=1` (or `netorch.py auto on`). Every `AUTO_INTERVAL` seconds (10) it reads `/plan` for
the last `PLAN_WINDOW` seconds (60) of demand.
- An `ON_DEMAND` slice (mMTC) that is dormant is activated when the plan needs it.
- One that is active is deactivated after `IDLE_SECONDS` (120) with no demand, and never within
  `MIN_UP` (60 s) of its last change.
- Demands for a dormant slice are refused but still logged, so the plan sees them. That is what
  wakes the slice up.

## API (`:9113`) and metrics

| | |
|---|---|
| `GET /slices` | discovered state of every slice |
| `POST /slices/<name>/activate`, `/deactivate`, `/scale {"cpus": x}` | start an operation; returns its id (202), or 409 if one is running |
| `GET /ops`, `GET /ops/<id>` | operations with their steps and timings |
| `GET /auto`, `POST /auto {"enabled": true}` | the closed loop and its recent decisions |
| `netorch_slice_active`, `netorch_slice_state`, `netorch_slice_ues_up`, `netorch_slice_nf_running`, `netorch_slice_cpus`, `netorch_operations_total`, `netorch_operation_seconds` | Prometheus, Grafana row "Network orchestrator" |

## Boundaries

- It switches slices that are **deployed from a template**. Creating a slice with a new S-NSSAI
  needs AMF/NSSF/SMF configuration and a core restart, which `scripts/open5gs/deploy.sh` owns.
  `deploy.sh` refuses to run while a slice is dormant.
- The UE container is shared by all slices, so its CPU cannot be split per slice.
- It runs against Docker. On Kubernetes the same operations would be `kubectl scale`, which is not
  implemented.
