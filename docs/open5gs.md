# Open5GS stack — a 5G core with a working user plane, on this laptop

The project runs on **Open5GS v2.8.0** as well as free5GC v4.2.3
([ADR-010](adr/ADR-010-open5gs-core.md)). The free5GC stack is untouched.

The difference that matters: **user traffic actually flows.** free5GC's UPF needs the `gtp5g`
kernel module, which cannot load on WSL2. Open5GS's UPF forwards in userspace through TUN devices,
so on the same laptop 20 UEs get addresses, traffic crosses a real 5G user plane per slice, a
demand-based slice orchestrator admits and delivers real flows, every deployment is judged by a
KPI gate with automatic rollback, and the whole stack also runs on Kubernetes.

```bash
make o5gs-build        # images from the SHAs in manifest.lock (~6 min cold)
make o5gs-up           # start everything; 20 UEs attach with PDU sessions
make o5gs-test         # SPEC acceptance criteria: PASS 22, FAIL 0
make o5gs-ping         # user plane per slice
make o5gs-orchestrate  # demands admitted by measured capacity, run as real flows
make o5gs-deploy       # deploy a config change behind the KPI gate, roll back if it fails
make o5gs-k8s-up       # the same stack on Kubernetes, judged by the same gate
make o5gs-urls         # Grafana http://localhost:3001 · Prometheus http://localhost:9091
```

> **Keep a WSL terminal open while any stack runs** — see [Engine restarts](#engine-restarts).

The story of how this state was reached — every problem found, its measured cause and fix — is in
the [completion report](reports/2026-09-14-open5gs-completion-report.md).

**Contents** — [Topology](#topology) · [Verified results](#verified-results) ·
[Slice isolation](#slice-isolation) · [Delivering the promised capacity](#delivering-the-promised-capacity) ·
[Slice orchestrator](#slice-orchestrator-on-real-traffic) · [Metrics you can trust](#which-metrics-can-be-trusted) ·
[UE supervision](#ue-supervision) · [KPI gate](#kpi-gate) · [Deploy and rollback](#deploy-and-rollback) ·
[Acceptance](#acceptance) · [Kubernetes](#kubernetes) · [Engine restarts](#engine-restarts) ·
[Operating notes](#operating-notes) · [Not done](#not-done)

---

## Topology

Slices are dedicated end to end in the user plane and share the control plane
([ADR-011](adr/ADR-011-dedicated-user-plane-per-slice.md)).

| | eMBB | URLLC |
|---|---|---|
| S-NSSAI · 5QI · ARP | SST 1 / 010203 · 9 · 8 | SST 2 / 112233 · 82 · 2 (may pre-empt) |
| Session AMBR | 200 Mbps | 20 Mbps |
| gNB | `o5gs-gnb-embb` 10.53.0.30 | `o5gs-gnb-urllc` 10.53.0.32 |
| DNN → UE pool | `internet` → 10.45.0.0/16 | `urllc` → 10.46.0.0/16 |
| UPF (selected by DNN) | `o5gs-upf-embb` 10.53.0.7, `ogstun` | `o5gs-upf-urllc` 10.53.0.8, `ogstun2` |
| Data network | `o5gs-dn-embb` 10.53.0.51 | `o5gs-dn-urllc` 10.53.0.52 |
| Subscribers (home slice) | IMSI …001–010 | IMSI …011–020 |

Shared: NRF, SCP, AUSF, UDM, UDR, PCF, BSF, NSSF, AMF, SMF, MongoDB; Prometheus and Grafana; and
one UE container running 20 supervised UE processes. Every subscriber is also permitted on the
other slice, which is what gives the orchestrator a choice.

**Reproducibility.** Open5GS is pinned to the *peeled* v2.8.0 commit `157f611a…` (the tag ref is an
annotated tag object); the image build verifies it and records it in the binaries. Upstream's meson
subprojects track branches and are rewritten to SHAs before building. UERANSIM is built from its
pinned SHA with one recorded local patch ([ADR-012](adr/ADR-012-ueransim-udp-buffers.md)), tagged
`v3.3.0-udpbuf`. Base images are pinned by digest.

---

## Verified results

| Claim | Result | Evidence |
|---|---|---|
| UEs with a PDU session, per slice pool | **20 / 20** (10 + 10) | [`open5gs-20260914-060008Z`](evidence/open5gs-20260914-060008Z/00-summary.md), acceptance |
| Traffic through each slice's own UPF | ping ~2 ms; UPF device counters move; internet egress | `make o5gs-ping` |
| Single flow UE → UPF → DN | eMBB ~256 Mbps · URLLC ~25 Mbps | same |
| Session AMBR enforced | URLLC **20.08 Mbps** over 20 s against 20 Mbps | [token bucket](#where-ambr-is-enforced) |
| Deliverable UDP capacity | **eMBB 200 Mbps** (0.43 % loss) · URLLC 20 Mbps | [`open5gs-capacity`](evidence/open5gs-capacity/README.md) |
| URLLC unaffected by a saturated eMBB | **10/10** probes in 3 of 3 runs (shared topology: 6, 8, 9, 10) | [`open5gs-isolation`](evidence/open5gs-isolation/) |
| Orchestrator, both slices saturated | **164 / 165** admitted flows delivered their rate; 15 refused | [`open5gs-orchestrator`](evidence/open5gs-orchestrator/README.md) |
| PDU session procedure on the wire | full sequence incl. user data, 73 ms control plane | [diagram](../diagrams/open5gs-pdu-session-establishment.mmd), [capture](evidence/open5gs-pdu-session-capture/) |
| Teardown `down -v` and rebuild | working stack in ~2.5 min from empty volumes | [`open5gs-rebuild`](evidence/open5gs-rebuild/) |
| Broken change deployed | caught by the gate (8 KPIs), **rolled back automatically** | [`open5gs-deploy`](evidence/open5gs-deploy/README.md) |
| SPEC acceptance | **PASS 22, FAIL 0**, SKIP-ODE 1, GAP 1, OWNER 1 | `make o5gs-test` |
| Kubernetes | 19 pods, 20 UEs, user plane per slice, **same KPI gate 17/17** | [`open5gs-k8s`](evidence/open5gs-k8s/) |

### Where AMBR is enforced

Neither the Open5GS UPF nor the SMF polices bitrate. **UERANSIM's gNB** does (`RateLimiter`,
`src/gnb/gtp/task.cpp`), from the session AMBR signalled over NGAP. Its token bucket holds one
second of AMBR and starts full, so a T-second test can reach AMBR × (T+1)/T: 4 s measured 23.85 Mbps
against a predicted 25.00; 20 s measured 20.08 against 21.00. Nothing enforces a *slice aggregate* —
an unmanaged TCP flood reached 542 Mbps on the 200 Mbps eMBB slice. Only the orchestrator's
admission keeps admitted traffic within it.

---

## Slice isolation

`make o5gs-isolation` — an idle phase against a phase where only eMBB carries three downlink
floods, measured by the same UE-side probes.

With **one shared gNB and UPF**, a saturated eMBB made URLLC lose up to 4 of 10 probes per round.
Measured cause: the shared gNB's UDP receive buffer overflowed (**12 817 drops** in 60 s; the UPF
had headroom), dropping packets regardless of slice.

With **a gNB and UPF per slice** ([ADR-011](adr/ADR-011-dedicated-user-plane-per-slice.md)):

| Topology | URLLC min reachable, eMBB saturated | URLLC worst RTT, eMBB saturated | gNB drops, eMBB saturated |
|---|---|---|---|
| Shared (4 runs) | 6, 8, 9, 10 / 10 | 7.4 – 10.0 ms | 12 817 |
| Dedicated (3 runs) | **10, 10, 10 / 10** | **5.5 – 7.0 ms** | eMBB gNB 14 000 – 17 800 · **URLLC gNB 0** |

Congestion now stays inside the slice that caused it. Still not isolated: **CPU**. URLLC's
worst-case RTT exceeds its 10 ms budget in some idle minutes (8 – 47 ms observed); a software
user plane and simulated RAN sharing a laptop cannot hold a URLLC tail.

---

## Delivering the promised capacity

When the orchestrator began running admitted demands as real UDP flows, **0 of 78 eMBB flows**
received their rate at an admitted 186 Mbps. Every bottleneck was found by accounting for every
lost packet ([ADR-012](adr/ADR-012-ueransim-udp-buffers.md)):

| Fix | Where the packets died | eMBB deliverable |
|---|---|---|
| — | eMBB gNB receive buffer: 128 382 drops | orchestrator: 0/78 flows |
| UERANSIM UDP buffers 8 MiB (small patch, `SO_RCVBUFFORCE`) | then UPF TUN `tx_dropped` (queue of 500) | 40 Mbps |
| UPF TUN `txqueuelen 10000` | then CPU: iperf3 senders inside the UPF container at ~970 % | 120 Mbps |
| Traffic from per-slice data-network containers | — | **200 Mbps**, 0.43 % loss |

---

## Slice orchestrator on real traffic

Same decision logic as the free5GC orchestrator ([README](../tools/slice-orchestrator/README.md)),
with three things added for Open5GS:

- **Measured capacity:** headroom = capacity − max(admitted, measured at the slice's UPF).
- **Execution:** each admitted demand runs as a UDP flow at its bitrate, device ↔ gNB ↔ UPF ↔ DN;
  `met` = ≥ 95 % delivered with ≤ 2 % loss.
- **No spill-over:** a demand uses only the least specialised slices that meet its latency need.

| Experiment | Result |
|---|---|
| 3 demands/s for 120 s | eMBB admitted to **200/200**, URLLC **20/20** Mbps; 15 refused; **164/165** delivered |
| eMBB flooded by 483 Mbps not admitted by the orchestrator | every eMBB demand refused on measured load; none spilled onto URLLC; admitted again after |

Boundary: each device holds a session on its home slice only, and a slice's gNB serves only that
slice, so a flow runs on the least-busy device of the chosen slice; the requester must still be
permitted on it.

---

## Which metrics can be trusted

Every gauge was compared with ground truth — the UE sessions that actually exist.

| Metric | Verdict |
|---|---|
| `fivegs_smffunction_sm_sessionnbr{snssai}` | ✅ exact, per slice |
| `ran_ue`, `amf_session`, `ues_active`, `gnb` | ✅ exact |
| `fivegs_upffunction_upf_sessionnbr`, `…upf_qosflows{dnn}` | ⚠️ presence only — 21 sessions / 11 flows for 20 / 10 real after one UE restart |
| `fivegs_smffunction_sm_qos_flow_nbr` | ❌ never decremented on re-attach |
| `fivegs_smffunction_sm_pdusessioncreationsucc` | ❌ counts each success twice |
| `fivegs_smffunction_sm_pdusessioncreationreq` | ❌ plus an unlabelled duplicate series |
| UPF `…datavolumeqosleveln3upf` | ❌ labelled only by QFI — cannot separate slices |

**R-02:** closed for sessions (the SMF labels S-NSSAI natively), not for traffic —
[`upf_slice_exporter.py`](../deployments/open5gs/observability/upf_slice_exporter.py) reads each
slice's UPF TUN device (rx = uplink, proven: 20 × 1028-byte packets moved `rx_bytes` by exactly
20 560).

**No core metric notices a UE that vanished or lost its user plane.** With every UE stopped the
core still reported 20 UEs and 20 sessions.
[`ue_probe_exporter.py`](../deployments/open5gs/observability/ue_probe_exporter.py) pings each
slice's gateway from every UE's own interface every 5 s; it is the liveness signal the gate trusts.

---

## UE supervision

UERANSIM declares radio link failure after 2 s without hearing from the gNB. The UE then returns to
CM-CONNECTED through a Service Request, **but its user plane never comes back** — the gNB logs
`PDU session not found`, and the core still counts the session. Reproduced on demand by freezing a
UE for 4 s.

[`ran/ue-entrypoint.sh`](../deployments/open5gs/ran/ue-entrypoint.sh) runs each UE as its own
process (started one at a time — concurrent starts race for TUN names), checks every 10 s that it
answers through its UPF, and restarts **that UE only** after 3 misses. UEs are identified by their
**session address, not interface name**: names are reused after a session drops, and checking by
name once kept two dead UEs marked healthy while pings went through another UE's session.

---

## KPI gate

[`deployments/open5gs/kpi-gates.json`](../deployments/open5gs/kpi-gates.json) — 19 KPIs on trusted
metrics, 14 enforced: exporters and PFCP associations, both gNBs, 20 UEs, sessions per slice and per
UPF, UE reachability (30 s window), average RTT within each slice's budget. 5 advisories: URLLC tail
latency, URLLC probe loss, registration failures, traffic on both slices, and the slice orchestrator
being scraped (a host process, so its absence must not fail a deployment of the network). Live stack
with the orchestrator running: **18 passed, 0 failed, 1 advisory warning** (URLLC tail latency, as
expected on this laptop). Earlier runs in the evidence report 17 passed — before the orchestrator
advisory existed. Same tool and rules as the
free5GC gate ([`docs/kpi-gate.md`](kpi-gate.md)): no data fails, advisories never fail, an
unreachable Prometheus exits 2.

Detection latency, measured with every UE stopped: a stopped exporter fails immediately; UE
liveness within 30 s; latency within 60 s.

---

## Deploy and rollback

[`scripts/open5gs/deploy.sh`](../scripts/open5gs/deploy.sh) closes objective 8 on remediation:

1. **validate** the candidate statically (YAML, slice/DNN routing, KPI definitions) — rejected
   without touching the running stack if it fails;
2. **apply** in dependency order (the SMF is always followed by its UPFs);
3. wait for UEs and 75 s of post-deploy data; **gate**;
4. **pass** → new known-good · **fail** → save the rejected candidate, restore known-good,
   redeploy, re-gate.

| Candidate | Outcome |
|---|---|
| SMF routes `urllc` to the eMBB UPF | rejected by validation; running SMF untouched · exit 1 |
| URLLC gNB AMF port 38412 → 38413 | 8 KPIs failed → **automatic rollback** → 17/17 · exit 1, candidate preserved |
| AMF network name changed | 17/17 → deployed, known-good updated · exit 0 |

---

## Acceptance

`make o5gs-test` — [`tests/acceptance-open5gs.sh`](../tests/acceptance-open5gs.sh), same
vocabulary as the free5GC suite.

| | free5GC (this laptop) | Open5GS (this laptop) |
|---|---|---|
| PASS | 12 | **22** |
| SKIP-ODE | 7 | 1 — the bare-metal `VERSIONS.lock` freeze |
| GAP | 1 | 1 — structured JSON logs (not configurable in either core) |
| OWNER | — | 1 — tagging a baseline is the project owner's decision |

PDU session, UE addressing, end-to-end user plane, a PDU-session diagram from observed behaviour and
teardown/rebuild — ODE-only on free5GC — are tested here.

---

## Kubernetes

`make o5gs-k8s-up` ([ADR-013](adr/ADR-013-kubernetes-open5gs.md)). Manifests are **generated from
the compose deployment** by [`k8s/render.py`](../deployments/open5gs/k8s/render.py) — servers bind
`dev: eth0`, IPs become Service names — so the two cannot drift; CI fails if the generated file is
stale. minikube profile `o5gs` (the machine's other profile is untouched), headless Services, init
containers for start order, the gNB pod IP from the Downward API (UERANSIM v3.3.0's interface-name
option is broken: `TryResolveHost()` overwrites the name with an empty string).

Result: 19 pods, 0 restarts; both gNBs and both PFCP associations up; 20 UEs (10 + 10); ping,
counters and iperf3 per slice (eMBB 258, URLLC 25 Mbps); **the same `kpi-gates.json`: 17/17**.
Grafana and the orchestrator are not deployed on Kubernetes.

---

## Engine restarts

The Docker engine is a native `docker-ce` **inside the Ubuntu WSL distro**; only the CLI plugins come
from Docker Desktop. WSL shuts the distro down shortly after the last attached terminal closes,
and systemd stops Docker with it. Measured: 11 engine stops in 12 minutes of one-shot commands,
**0 in 15 minutes** with a session held open, a stop again ~45 s after release. Earlier notes blamed
Docker Desktop; that was wrong.

Every container restarts by policy and the UEs re-attach on their own. **Keep a WSL terminal open.**

---

## Operating notes

- Restarting the SMF requires restarting its UPFs; a UPF started before the SMF keeps stale PFCP
  state and rejects every session (`Cannot find PFCP-Node`). `deploy.sh` enforces the order.
- Exporters run inside the UPF and UE containers: sidecars sharing a network namespace are stranded
  when their target restarts, and Docker gave up restarting them.
- Scope `docker logs` checks with `--since` the container's start; old lines survive restarts.
- Never `pkill -f <pattern>` inside `sh -c "…<pattern>…"` — it kills its own shell.
- `wsl -- bash -c '…'` does not preserve quoting: `$?`, `$(…)` and `$var` are expanded by an outer
  shell. Run anything non-trivial from a script file.
- Test traffic belongs in the data-network containers, never inside a UPF.

## Not done

- **Grafana and the orchestrator on Kubernetes**, and anything beyond single-node minikube.
- **CPU isolation between slices** — URLLC's tail budget is not met on this laptop.
- **A slice-aggregate rate limit in the network** — only admission enforces it.
- **Tagging a baseline** — the owner's decision.
- The **bare-metal environment freeze** (`VERSIONS.lock`) — needs the ODE.
