# Open5GS stack — completion report

**Date:** 2026-09-14 · **Host:** WSL2 Ubuntu 24.04, 12 threads, 7.6 GiB RAM (non-baseline, ADR-003)
**Scope:** everything that can be built and proven on this laptop with Open5GS v2.8.0
**Starting point:** commit `fb1c65f` — Open5GS running beside free5GC with a working user plane

This report tells the whole story of that work: what was asked, what was built, what went wrong,
how each problem was found and fixed, what the final measured state is, and what is still not done.
Every number comes from a run whose raw output is in [`docs/evidence/`](../evidence/). Reference
detail lives in [`docs/open5gs.md`](../open5gs.md) and ADR-010 to ADR-013.

---

## Contents

1. [Summary](#1-summary)
2. [Where this started](#2-where-this-started)
3. [What happened, in order](#3-what-happened-in-order)
4. [Problems found and how each was fixed](#4-problems-found-and-how-each-was-fixed)
5. [Final measured state](#5-final-measured-state)
6. [What was built](#6-what-was-built)
7. [How it was verified](#7-how-it-was-verified)
8. [What is not done, and why](#8-what-is-not-done-and-why)
9. [Lessons](#9-lessons)
10. [Reproducing everything](#10-reproducing-everything)

---

## 1. Summary

At the start, Open5GS already carried user traffic on this laptop, but four things were
still claims or gaps:

- the slices were not isolated;
- the slice orchestrator decided on paper and never ran real traffic;
- a failed KPI gate stopped a deployment but did not undo it;
- nothing ran on Kubernetes.

All four are now built and proven here:

| Goal | Outcome |
|---|---|
| Slice isolation | URLLC kept **10/10** UEs reachable while eMBB was saturated, in 3 of 3 runs (it lost up to 4 of 10 before) |
| Slices deliver what they promise | eMBB **200 Mbps**, URLLC **20 Mbps** of real UDP (eMBB delivered **0 of 78** flows before) |
| Orchestrator on real traffic | **164 of 165** admitted flows got their full rate; admission refuses on *measured* load |
| PDU session procedure | captured on the wire at four network functions; sequence diagram drawn from the capture |
| Teardown and rebuild | **PASS** — working stack in ~2.5 min from empty volumes |
| Acceptance tests | **PASS=22, FAIL=0** (free5GC on the same laptop: 12) |
| Automatic rollback | a broken change was deployed, failed the gate on 8 KPIs, and was **rolled back with no human action** |
| Kubernetes | 19 pods, 20 UEs, traffic through each slice, **the same KPI gate passes** |
| CI | every step passes, including two new ones (manifests current, orchestrator rules) |

The most important finding: **several problems were invisible until real traffic was pushed
through them.** The orchestrator's first real run admitted 186 Mbps of eMBB demand and delivered
none of it. The fix was not in the orchestrator. It took three separate bottlenecks in the lab's
data path, each found by counting where the packets went.

---

## 2. Where this started

Commit `39c177e` added Open5GS as a second core ([ADR-010](../adr/ADR-010-open5gs-core.md)).
Unlike free5GC, whose UPF needs the `gtp5g` kernel module (unavailable on WSL2), Open5GS
forwards user traffic in userspace through TUN devices. At that commit:

- 20 UEs held PDU sessions, 10 per slice, each slice with its own DNN and address pool;
- ping and iperf3 worked through the UPF, and UEs could reach the internet;
- a 17-KPI gate passed on the live stack;
- the UE-side liveness probes and supervisor were in place.

Four things were open, and [`docs/open5gs.md`](../open5gs.md) said so:

1. **Slices shared fate.** Both slices went through one gNB and one UPF. Saturating eMBB made
   URLLC lose up to 4 of 10 probes per round.
2. **The orchestrator was never run against real traffic** on this stack.
3. **The gate detected but did not remediate**: no rollback.
4. **No Kubernetes** (SPEC Phase 2a).

The request was: *"complete everything that can be done in this laptop with open5gs."*

---

## 3. What happened, in order

### Phase A — Isolating the slices

**Measure first.** [`scripts/open5gs/isolation_test.sh`](../../scripts/open5gs/isolation_test.sh)
compares an idle phase with a phase where only eMBB carries three downlink floods, using the same
UE-side probes the gate trusts. With eMBB saturated, the per-container numbers were:

| | CPU | UDP receive-buffer drops |
|---|---|---|
| UE processes | 358 % | — |
| **shared gNB** | 183 % | **12 817 in 60 s** |
| shared UPF | 72 % | 0 |

The UPF had headroom. The gNB's single UDP socket overflowed, and a full buffer drops packets
from both slices alike. A UPF per slice alone would not have fixed it.

**Decision ([ADR-011](../adr/ADR-011-dedicated-user-plane-per-slice.md)):** each slice gets its
own gNB **and** its own UPF over a shared control plane:

- `gnb-embb` (10.53.0.30) and `upf-embb` (10.53.0.7), pool 10.45/16;
- `gnb-urllc` (10.53.0.32) and `upf-urllc` (10.53.0.8), pool 10.46/16.

The SMF selects the UPF by DNN, which Open5GS supports natively. Each UE config points at its
slice's gNB.

**Result, same test:** URLLC reachable 10/10 in all 3 runs, worst RTT 5.5 – 7.0 ms, URLLC gNB
drops 0. The eMBB flood still overflows a gNB, but now only its own.
Evidence: [`open5gs-isolation/`](../evidence/open5gs-isolation/).

**A new failure surfaced while doing this:** after restarting only the SMF, the eMBB UPF (started
earlier) rejected every session with `Cannot find PFCP-Node`, while the URLLC UPF (started after
the SMF) worked. Rule adopted: **restart the UPFs after the SMF.** The deploy script now enforces it.

### Phase B — The orchestrator on real traffic

The orchestrator was extended for Open5GS (`CORE=open5gs`) in three ways:

- it reads the Open5GS subscriber database;
- it takes each slice's used capacity as the larger of what it admitted and what is **measured**
  at that slice's UPF (from Prometheus);
- with `EXECUTE=1`, it **runs every admitted demand** as a UDP flow at its admitted bitrate.
  "Met" means ≥ 95 % of the rate delivered with ≤ 2 % loss.

**First run** ([`1-before-fixes-shared-buffers`](../evidence/open5gs-orchestrator/1-before-fixes-shared-buffers/1-saturation.txt)):
72 of 150 flows met. **URLLC 72/72, eMBB 0/78.** Up to 186 Mbps was admitted on a 200 Mbps
slice, and not one eMBB flow received its rate. That run also exposed a placement bug: once eMBB
was full, video was admitted **onto URLLC**, taking 16 of its 20 Mbps.

That result started Phase C.

### Phase C — Making the data path deliver the promised capacity

Each step accounted for every lost packet before changing anything
([ADR-012](../adr/ADR-012-ueransim-udp-buffers.md),
[`open5gs-capacity/`](../evidence/open5gs-capacity/README.md)):

| Step | Where the packets died | Fix | eMBB deliverable |
|---|---|---|---|
| 0 | eMBB gNB receive buffer: **128 382 drops** | — | 0/78 flows |
| 1 | UERANSIM never sets `SO_RCVBUF`, so it gets the 212 KB default, and `rmem_max` cannot be raised in a container | patch: `SO_RCVBUFFORCE`/`SO_SNDBUFFORCE` to 8 MiB when an env var is set | 40 Mbps |
| 2 | then UPF TUN `tx_dropped` (+9 638 vs 9 439 lost): queue of 500 packets | `txqueuelen 10000` on each UPF TUN | 120 Mbps |
| 3 | then CPU: the UPF container at **973 %**, mostly iperf3 senders inside it | per-slice data-network containers generate the traffic | **200 Mbps**, 0.43 % loss |

The UERANSIM change is one small patch, applied at image build (the build fails if it stops
applying). The image is tagged `v3.3.0-udpbuf` so it is never mistaken for upstream.

Two measurement bugs were fixed on the way:

- **iperf3 in reverse mode reports the sender's rate**, so "delivered" had been overstated. The
  calibration script and the orchestrator now compute delivered = rate × (1 − loss).
- **The spill-over bug from Phase B.** A demand is now placed only on the least specialised slice
  that meets its latency need, and refused if that slice is full. `SPILLOVER=1` restores the old
  behaviour on purpose. Nine unit tests
  ([`test_decide.py`](../../tools/slice-orchestrator/test_decide.py)) pin these rules, and CI runs them.

**Final orchestrator runs** ([`open5gs-orchestrator/`](../evidence/open5gs-orchestrator/README.md)):

| Run | Flows met | Refused | Notes |
|---|---|---|---|
| 2 demands/s | **142 / 142** | 0 | slices never filled |
| 2 demands/s + a 483 Mbps flood the orchestrator did not admit | — | all 6 eMBB demands | refused on *measured* load; none spilled onto URLLC; admitted again once the flood stopped |
| 3 demands/s, 120 s | **164 / 165** | 15 | both slices admitted to exactly 200/200 and 20/20 Mbps |

### Phase D — A supervisor bug that the KPI gate caught

After a UPF restart, the gate reported **9/10 UEs reachable** on a slice while the UE supervisor
reported every UE healthy. Cause: when a session drops, its `uesimtunN` device is destroyed, and
the next session anywhere can reuse the name. The supervisor checked by name, pinged successfully
through *another* UE's session, and kept two dead UEs marked healthy.

**Fix:** each UE is identified by the address the SMF gave it, which is unique, not by
interface name. The failure was reproduced with the same trigger. Both UEs were detected
("session address is on no interface"), restarted, and 20/20 were back within 60 s.

This is the gate doing its job: an independent measurement caught a component that was wrong
about its own health.

### Phase E — The PDU session procedure, from the wire

[`scripts/open5gs/capture_pdu_session.sh`](../../scripts/open5gs/capture_pdu_session.sh) captures
inside the AMF, SMF, eMBB UPF and eMBB gNB network namespaces (netshoot image pinned by digest).
It releases and re-establishes one UE's session, merges the captures and decodes them with tshark.

The [sequence diagram](../../diagrams/open5gs-pdu-session-establishment.mmd) was drawn from that
capture: NGAP, SBI via the SCP, PFCP and GTP-U user data. The control plane took **73 ms**. Two
things are marked honestly in the diagram:

- **UE ↔ gNB messages** travel over UERANSIM's simulated radio link and are not in the capture.
- **The UDM registration PUT** is inferred from source (`src/smf/n4-handler.c`), not observed.

Evidence: [`open5gs-pdu-session-capture/`](../evidence/open5gs-pdu-session-capture/).

### Phase F — Teardown and rebuild

[`tests/open5gs-rebuild.sh`](../../tests/open5gs-rebuild.sh) runs `docker compose down -v`,
confirms nothing is left, rebuilds, re-provisions and re-verifies. Result **PASS**:

- 20/20 registered, with sessions from each slice's own pool;
- ping and iperf3 through both UPFs, internet egress;
- gate 17 passed, 0 failed.

Evidence: [`open5gs-rebuild/`](../evidence/open5gs-rebuild/).

### Phase G — Acceptance tests

[`tests/acceptance-open5gs.sh`](../../tests/acceptance-open5gs.sh) ports the SPEC §5 criteria,
with the same vocabulary as the free5GC suite (PASS / FAIL / SKIP-ODE / GAP, plus OWNER for
decisions that belong to the project owner). It uses
[`tools/check_open5gs_slices.py`](../../tools/check_open5gs_slices.py), which proves each slice is
consistent across AMF, NSSF, gNB, SMF routing and UPF pool. CI runs the same check.

**Result: PASS=22, FAIL=0, SKIP-ODE=1, GAP=1, OWNER=1.**

- **SKIP-ODE 1:** the bare-metal `VERSIONS.lock` freeze.
- **GAP 1:** structured JSON logs; neither core can be configured for them.
- **OWNER 1:** tagging a baseline.

PDU session, UE addressing, end-to-end user plane, a diagram from observed behaviour, and
teardown/rebuild are all ODE-only on free5GC, and all tested here.

### Phase H — Deploy with automatic rollback

[`scripts/open5gs/deploy.sh`](../../scripts/open5gs/deploy.sh) closes objective 8 on
remediation for this stack:

1. **Validate statically**: every YAML file, slice/DNN routing consistency, KPI definitions.
   If this fails, the candidate is rejected and the running stack is not touched.
2. **Snapshot** the last known-good config (in git-ignored `.deploy/`).
3. **Apply** in dependency order (SMF, then its UPFs), start UEs, wait 75 s so the gate's
   1-minute windows hold only post-deploy data.
4. **Gate.** Pass: the candidate becomes known-good. Fail: the candidate is saved to
   `.deploy/rejected/<time>`, known-good is restored and redeployed, and the gate runs again.

Exit codes: 0 deployed, 1 rejected or rolled back, 2 could not run. Three scenarios were run
([evidence](../evidence/open5gs-deploy/README.md)):

| Candidate | What happened | Exit |
|---|---|---|
| SMF routes DNN `urllc` to the eMBB UPF | rejected by static validation; running SMF untouched | 1 |
| URLLC gNB AMF port 38412 → 38413 (passes static checks) | deployed; **8 KPIs failed** (URLLC gNB, its 10 UEs, its UPF sessions, slice metrics, reachability, latency); **rolled back automatically**; gate passed again; candidate preserved | 1 |
| AMF network name changed | gate passed; deployed; known-good updated | 0 |

A false alarm on the way: one run appeared to exit 0 after a rollback. `wsl -- bash -c '…'` had
expanded `$?` in an outer shell before the script ran. Run from a script file, the exit code was 1.

### Phase I — Kubernetes

[ADR-013](../adr/ADR-013-kubernetes-open5gs.md) and
[`scripts/open5gs/k8s_up.sh`](../../scripts/open5gs/k8s_up.sh):

- **Separate cluster.** minikube profile `o5gs` (6 CPU, 4 GiB). The other minikube profile on
  this machine was not touched.
- **Manifests generated from the compose deployment** by
  [`k8s/render.py`](../../deployments/open5gs/k8s/render.py), never hand-edited:
  - servers bind `dev: eth0`;
  - IPs become Service DNS names;
  - output: 19 Deployments, 19 headless Services, 4 ConfigMaps, 1 PVC.

  CI re-renders and fails if the committed file differs.
- **Start order:** headless Services (PFCP and NGAP identify peers by source address), with
  init containers waiting for peer DNS.
- **Same images, same verdict.** The images built from the pinned commits are loaded into the
  cluster, and the verdict comes from the **same** `kpi-gates.json` through a port-forward to
  the in-cluster Prometheus.

**Two problems on the way:**

1. **Every gNB pod crash-looped** with "linkIp must be a valid IP". UERANSIM documents an
   interface name as valid there, but `GetIpAddress()` calls `TryResolveHost()` first, which
   returns an empty string and overwrites the name. Fixed without a second patch: the pod IP
   comes from the Downward API and is substituted into the config at start.
2. **The script's own step 6 failed** with `$3: unbound variable`. Positional parameters were
   used inside an eval'd check function. Fixed with named variables.

**Result, full run from an empty namespace:**

- 19 pods, 0 restarts;
- both gNBs through NG Setup, both PFCP associations up;
- 20 UEs (10 + 10), each from its slice's pool;
- ping and UPF counters per slice; iperf3 eMBB 258 / URLLC 25 Mbps;
- **the same KPI gate: 17 passed, 0 failed.**

Evidence: [`open5gs-k8s/`](../evidence/open5gs-k8s/).

### Phase J — Gate, CI and documentation cleanup

**KPI gate.** It failed once on a healthy stack only because the orchestrator (a host process)
was stopped, which made `no-scrape-failures` count it. That gate now excludes the orchestrator,
and a separate **advisory** KPI reports whether the orchestrator is being scraped: a stopped host
tool should not fail a deployment of the network.

The gate is now 19 KPIs: 14 enforced, 5 advisory. On the live stack: **18 passed, 0 failed,
1 advisory warning** — URLLC worst-case RTT over 10 ms, which is expected on shared laptop CPU
and stated in the KPI's own rationale.

**CI.** Replaying the CI jobs locally found one failing step. The generated Kubernetes manifest
is a multi-document YAML file, and the "every YAML config parses" step read single documents.
`deploy.sh`'s validator had the identical bug and rejected every candidate. Both now use
`safe_load_all`. Replay after the fix: **all steps pass.**

**Documentation.** Brought in line with the measured state:

- `docs/open5gs.md`
- `docs/RESULTS.md`
- `README.md`
- `docs/kpi-gate.md`
- ADR-010 outcome, plus the new ADR-011, ADR-012 and ADR-013

---

## 4. Problems found and how each was fixed

| # | Symptom | Root cause (measured) | Fix | Where recorded |
|---|---|---|---|---|
| 1 | URLLC lost up to 4/10 probes when eMBB saturated | shared gNB UDP receive buffer overflow (12 817 drops/60 s) | gNB + UPF per slice | ADR-011 |
| 2 | eMBB UPF rejected all sessions after an SMF restart | UPF kept stale PFCP state from before the SMF restarted | restart UPFs after the SMF; enforced by deploy.sh | ADR-011, operating notes |
| 3 | start_ues reported "registered 0/20" with 20 UEs registered | single-UE nr-ue logs carry no IMSI tag; the count looked for it | count per-UE log files | start_ues.sh |
| 4 | 0 of 78 eMBB orchestrator flows got their rate | gNB UDP buffers (128 382 drops) | UERANSIM buffer patch | ADR-012 §1 |
| 5 | 7.9 % loss remained at 80 Mbps | UPF TUN queue of 500 overflowed | `txqueuelen 10000` | ADR-012 §2 |
| 6 | loss at 160 Mbps, UPF container at 973 % CPU | iperf3 senders inside the UPF container | per-slice DN containers | ADR-012 §3 |
| 7 | video admitted onto URLLC when eMBB was full | placement accepted any latency-qualified slice | no spill-over by default; unit tests | ADR-012 §4 |
| 8 | "delivered" overstated | iperf3 `-R` reports the sender's bits/s | delivered = rate × (1 − loss) | orchestrator, calibrate |
| 9 | 2 dead UEs marked healthy; gate showed 9/10 | supervisor identified UEs by reusable interface name | identify by session address | ADR-012 §5 |
| 10 | a rollback run appeared to exit 0 | `wsl -- bash -c` expanded `$?` early | run from script files | operating notes |
| 11 | every gNB pod crash-looped on Kubernetes | UERANSIM `TryResolveHost` overwrites interface names | pod IP from Downward API | ADR-013 §4 |
| 12 | k8s script failed at step 6 | positional params inside an eval'd function | named variables | k8s_up.sh |
| 13 | gate failed on a healthy stack | orchestrator (host process) counted as a scrape failure | scoped the gate; added an advisory | kpi-gates.json |
| 14 | tshark refused to decode | option `http2.heuristic_enable` absent in that version | removed | capture_pdu_session.sh |
| 15 | CI YAML step and deploy validation rejected the repo | multi-document manifest parsed as one document | `safe_load_all` | integrity.yml, deploy.sh |
| 16 | `.PHONY` in the Makefile held literal `\n` tokens (since the first Open5GS commit) | a scripted edit wrote escape sequences instead of line breaks | real continuation lines; all 24 `o5gs-*` targets declared phony | Makefile |

---

## 5. Final measured state

| Claim | Result | Evidence |
|---|---|---|
| UEs with a PDU session | 20 / 20 (10 + 10, own pool per slice) | acceptance, rebuild |
| Traffic through each slice's own UPF | ping ~2 ms; UPF counters move; internet egress | `make o5gs-ping` |
| Single flow UE → UPF → DN | eMBB ~256 Mbps · URLLC ~25 Mbps | same |
| Session AMBR enforced (by UERANSIM's gNB) | URLLC 20.08 Mbps over 20 s vs 20 Mbps | open5gs.md |
| Deliverable UDP capacity | eMBB 200 Mbps (0.43 % loss) · URLLC 20 Mbps | open5gs-capacity |
| URLLC with eMBB saturated | 10/10 reachable, 3 of 3 runs | open5gs-isolation |
| Orchestrator, both slices saturated | 164/165 flows met; 15 refused | open5gs-orchestrator |
| PDU session on the wire | full sequence incl. user data; 73 ms control plane | open5gs-pdu-session-capture |
| Teardown and rebuild | PASS in ~2.5 min | open5gs-rebuild |
| Broken change deployed | caught (8 KPIs), rolled back automatically | open5gs-deploy |
| SPEC acceptance | PASS 22, FAIL 0, SKIP-ODE 1, GAP 1, OWNER 1 | `make o5gs-test` |
| Kubernetes | 19 pods, 20 UEs, user plane per slice, same gate passes | open5gs-k8s |
| KPI gate, live stack | 18 passed, 0 failed, 1 advisory | `kpi_gate.py` |
| CI replay | all steps pass | `.github/workflows/integrity.yml` |

**State of the machine when this report was written:**

- the compose Open5GS stack (20 containers) and the orchestrator are running;
- the free5GC containers are stopped;
- minikube profile `o5gs` is stopped;
- the deploy known-good snapshot was refreshed against the current configuration.

---

## 6. What was built

**Topology and config** (`deployments/open5gs/`)

- `config/upf-embb.yaml`, `config/upf-urllc.yaml` — one UPF per slice (replace `upf.yaml`).
- `ran/gnb-embb.yaml`, `ran/gnb-urllc.yaml` — one gNB per slice (replace `gnb.yaml`).
- `config/smf.yaml` routes each DNN to its UPF.
- `upf-entrypoint.sh` — parameterised per slice; TUN queue length; NAT exemption for the DN subnet.
- `docker-compose.yaml` — per-slice gNB, UPF and DN services.
- `ran/ue-entrypoint.sh` — per-UE supervision by session address.
- `images/patches/ueransim-udp-socket-buffers.patch`, plus the Dockerfile changes.

**Observability**

- Prometheus scrapes both UPFs and the orchestrator.
- The Grafana dashboard generator adds orchestrator rows: capacity, admitted and measured load
  per slice, flows met, refusals, delivery ratio.
- `kpi-gates.json` — 19 KPIs.

**Orchestrator** (`tools/slice-orchestrator/`)

- `orchestrator.py` — Open5GS mode, measured capacity, flow execution, no spill-over,
  `CAPACITY_OVERRIDE`.
- `test_decide.py` — 9 placement-rule tests.

**Scripts** (`scripts/open5gs/`)

- `deploy.sh` — validate, apply, gate, roll back.
- `k8s_up.sh` — deploy, `--verify`, `--down`.
- `orchestrator_demo.sh`, `start_orchestrator.sh`
- `calibrate_capacity.sh`, `isolation_test.sh`
- `capture_pdu_session.sh`
- Updated for per-slice topology: `traffic.sh`, `verify_user_plane.sh`, `start_ues.sh`,
  `collect_evidence.sh`, `provision_subscribers.py` (`--kubernetes`).

**Tests and CI**

- `tests/acceptance-open5gs.sh`, `tests/open5gs-rebuild.sh`, `tools/check_open5gs_slices.py`.
- New CI steps: Open5GS slice consistency via the shared checker, Kubernetes manifests current,
  orchestrator placement rules, multi-document YAML parsing.

**Kubernetes**

- `deployments/open5gs/k8s/render.py` and the generated `k8s/generated/o5gs.yaml`.

**Make targets**

- `o5gs-orchestrate`, `o5gs-slices`, `o5gs-test`
- `o5gs-deploy`, `o5gs-deploy-init`, `o5gs-rebuild`
- `o5gs-calibrate`, `o5gs-isolation`, `o5gs-capture`
- `o5gs-k8s-up`, `o5gs-k8s-verify`, `o5gs-k8s-down`

**Documentation**

- ADR-011, ADR-012, ADR-013.
- `docs/open5gs.md` rewritten.
- `docs/RESULTS.md`, `README.md`, `docs/kpi-gate.md` and the ADR-010 outcome updated.
- The PDU session diagram.
- One README per evidence directory.
- This report.

---

## 7. How it was verified

Each claim was checked by something other than the component making it:

- **Liveness** comes from UE-side probes, not the core's gauges. The core still reported 20 UEs
  with every UE stopped.
- **Traffic** is counted at each UPF's TUN device, and the counter direction was proven with an
  exact byte count (20 × 1028-byte packets → +20 560 rx bytes).
- **Delivery** is measured at the receiver, with loss, not the sender's rate.
- **Isolation, capacity and orchestrator results** were each repeated (3 isolation runs, 3
  orchestrator runs, a stepped calibration) and kept as raw output.
- **Every gate was shown to fail.** Stopped UEs, a stopped exporter and a broken gNB config
  were each run and each produced a FAIL.
- **Rollback was exercised on a real breakage**, not simulated.
- **Kubernetes was deployed from an empty namespace**, database included, not just re-verified.
- **CI jobs were replayed locally** before any push.

---

## 8. What is not done, and why

**Doable on this laptop, not yet done:**

- **Replacing a UPF pod on Kubernetes.** The expected need to restart the SMF and UPFs is
  written down in ADR-013 as *not tested*.
- **Grafana and the orchestrator on Kubernetes.** The orchestrator drives traffic with
  `docker exec` and would need `kubectl exec`.
- **A slice-aggregate rate limit in the network.** An unmanaged TCP flood reached 542 Mbps on
  the 200 Mbps eMBB slice. The gNB enforces each session's AMBR; only the orchestrator's
  admission keeps a slice's total within capacity. A `tc` limit per UPF TUN would enforce it.
- **CPU isolation between slices.** URLLC's worst-case RTT exceeds 10 ms in some idle minutes.
  Pinning cores might help, but with 12 threads shared with Windows it may not meet the budget.
- **Automatic rollback for the free5GC stack**, which could cover its control plane only.
- **Refreshed Grafana screenshots** showing the orchestrator panels under load.

**Not possible here:**

- **The bare-metal environment freeze (`VERSIONS.lock`).** It needs the ODE (Ubuntu 24.04 on
  bare metal, ≥ 16 GB).
- **A baseline tag.** It is the project owner's decision.
- **Multi-node networking, CNI choice, SR-IOV, real radio.** They are beyond one laptop and a
  simulated RAN.

**Unchanged by this work:** the free5GC stack and its results, including its user plane, which
remains ODE-only.

---

## 9. Lessons

1. **Admitted is not delivered.** The orchestrator's arithmetic was right and delivered nothing.
   Only running real flows showed it.
2. **Count the packets before changing anything.** Each of the three capacity fixes targeted
   the counter that had moved: gNB receive errors, then TUN `tx_dropped`, then CPU. None was
   the first guess.
3. **Measure isolation where it is consumed.** A dedicated UPF would have looked like the
   obvious fix; the drops were in the gNB.
4. **Self-reported health is not health.** The core counted sessions that no longer carried
   traffic, and the supervisor marked dead UEs healthy. Independent UE-side probes caught both.
5. **Test the thing that fails.** A gate never observed failing and a rollback never triggered
   by a real breakage prove nothing. Both were broken on purpose here.
6. **Generate, do not duplicate.** Rendering Kubernetes manifests from compose, with CI checking
   they are current, means one change reaches both deployments.
7. **Quoting across shells lies.** `wsl -- bash -c` produced a false exit code. Run anything
   non-trivial from a file.

---

## 10. Reproducing everything

```bash
make o5gs-build          # images from the pinned SHAs (UERANSIM with the buffer patch)
make o5gs-up             # compose stack; 20 UEs attach
make o5gs-test           # acceptance: PASS 22, FAIL 0
make o5gs-ping           # user plane per slice
make o5gs-isolation      # URLLC under a saturated eMBB
make o5gs-calibrate      # deliverable capacity per slice
make o5gs-orchestrate    # orchestrator on real flows
make o5gs-capture        # PDU session capture + decode
make o5gs-rebuild        # teardown with volumes and rebuild
make o5gs-deploy-init    # record the running stack as known-good
make o5gs-deploy         # deploy a change behind the gate; roll back on failure
make o5gs-k8s-up         # Kubernetes (minikube profile o5gs), same gate
make o5gs-k8s-down       # delete the namespace, stop that cluster
```

Keep a WSL terminal open while any stack runs: WSL idles the Ubuntu distro, and the Docker
engine inside it stops with it.
