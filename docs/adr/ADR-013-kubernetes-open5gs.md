# ADR-013 — Kubernetes deployment of the Open5GS stack, generated from compose

**Status:** Accepted, 2026-09-14
**Implements:** SPEC Phase 2a (ADR-001, ADR-009) for the Open5GS stack — objective 9

## Context

SPEC Phase 2a: move the verified baseline to Kubernetes and re-establish the same end-to-end
behaviour, using the compose deployment as the regression oracle. The laptop has kubectl 1.34,
minikube 1.38 and helm 3.20; an older minikube profile already exists and must not be disturbed.

## Decisions

1. **Manifests are generated from the compose deployment, never hand-maintained.**
   [`deployments/open5gs/k8s/render.py`](../../deployments/open5gs/k8s/render.py) reads the compose
   file and every NF, RAN and observability config, and writes
   [`k8s/generated/o5gs.yaml`](../../deployments/open5gs/k8s/generated/o5gs.yaml). Only what must
   differ is transformed: servers bind `dev: eth0` instead of a fixed IP (Open5GS accepts `dev`),
   every IP reference becomes a Service DNS name, Prometheus targets become Service names. A slice,
   QoS value or NF setting is changed once and both deployments carry it.

2. **Headless Services.** DNS resolves straight to the pod IP. PFCP and NGAP peers identify each
   other by source address, which a ClusterIP's NAT would hide.

3. **Start ordering with init containers.** Open5GS and UERANSIM resolve their peers once at
   startup, and a headless Service has no DNS record until its pod runs. Each Deployment waits for
   its peers' names before starting (13 init containers).

4. **gNB addresses from the Downward API.** UERANSIM v3.3.0 documents a network interface name as
   valid for `linkIp`/`ngapIp`/`gtpIp`, but `GetIpAddress()` first calls `TryResolveHost()`, which
   returns an empty string for a non-hostname and overwrites the interface name before the lookup
   runs. Every gNB pod crash-looped on this. The pod IP (`status.podIP`) is substituted into the
   config at container start instead — no second upstream patch.

5. **Same images, same tests.** The images built from the pinned commits are loaded into the
   cluster (`imagePullPolicy: Never`). The verdict comes from the **same**
   `deployments/open5gs/kpi-gates.json`, queried through a port-forward to the in-cluster
   Prometheus, which keeps the compose scrape jobs and labels.

6. **Scope and trade-offs on this laptop**
   - minikube profile `o5gs`, docker driver, 6 CPUs / 4 GiB; the existing profile is untouched.
   - The compose stack is paused while the cluster runs: two cores on 7.6 GiB would distort every
     measurement.
   - UPF and UE pods are **privileged** (TUN devices, iptables, `ip_forward`); gNBs get
     `NET_ADMIN` for the UDP buffer patch (ADR-012).
   - UE traffic reaches the data-network pods through the UPF's NAT; the compose-only NAT
     exemption (ADR-012) is dropped, since DN pod addresses are not fixed.
   - Not deployed on Kubernetes: Grafana and the slice orchestrator (a host process that drives
     containers with `docker exec`). Prometheus is deployed, because the KPI gate needs it.

## Result

`scripts/open5gs/k8s_up.sh` — evidence in [`docs/evidence/open5gs-k8s/`](../evidence/open5gs-k8s/).

| Check | Compose | Kubernetes |
|---|---|---|
| Pods / containers running | 20 | 19 pods, 0 restarts |
| gNB NG Setup, SMF–UPF PFCP | ✅ | ✅ |
| UEs with a PDU session | 20 (10 + 10) | 20 (10 + 10) |
| Ping through each slice's UPF, UPF counters move | ✅ | ✅ |
| iperf3 UE → UPF → DN | eMBB 256, URLLC 24 Mbps | eMBB 258, URLLC 25 Mbps |
| Same KPI gate definitions | 17/17 | **17/17** |

## Consequences

- Phase 2a behaviour is re-established on Kubernetes for the Open5GS stack, judged by the same
  gate as compose. Phase 2b (observability on Kubernetes) is partly in place: Prometheus runs in
  the cluster; Grafana and the orchestrator do not.
- A config change must be followed by `render.py` and `kubectl apply`; CI checks the generated file
  is current (`render.py` output must match the committed file).
- Headless-service DNS is resolved once by the NFs, so replacing a UPF pod gives it an address the
  SMF has not resolved. Expected consequence, **not tested here**: restart the SMF (then the UPFs,
  per ADR-011) after replacing a UPF pod.
- This is single-node minikube, not a production cluster: no multi-node networking, CNI choice,
  SR-IOV or resource limits were exercised.
