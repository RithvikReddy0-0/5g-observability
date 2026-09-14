# Open5GS stack — a 5G core with a working user plane on this laptop

The project now runs on **Open5GS v2.8.0** as well as free5GC v4.2.3. The decision and its
reasoning are in [ADR-010](adr/ADR-010-open5gs-core.md). The free5GC stack is untouched.

The difference that matters: **user traffic actually flows.** free5GC's UPF needs the `gtp5g`
kernel module, which cannot load on WSL2, so that stack never had a PDU session. Open5GS's UPF
forwards in userspace through TUN devices, so on the same laptop 20 UEs get IP addresses,
send real packets through the UPF per slice, and reach the internet.

```bash
make o5gs-build     # build Open5GS + UERANSIM from the SHAs in manifest.lock (~6 min cold)
make o5gs-up        # start everything; 20 UEs attach with PDU sessions
make o5gs-ping      # prove the user plane, per slice
make o5gs-traffic   # real traffic through both slices at once
make o5gs-gate      # KPI gate against the live stack
make o5gs-evidence  # timestamped proof bundle (~4 min)
make o5gs-urls      # Grafana http://localhost:3001 · Prometheus http://localhost:9091
```

> **Keep a WSL terminal open while the stack runs.** See [Engine restarts](#engine-restarts).

---

## What runs

| | |
|---|---|
| Core | NRF, SCP, AUSF, UDM, UDR, PCF, BSF, NSSF, AMF, SMF, UPF — one image, built from source |
| RAN | UERANSIM v3.3.0 gNB, and 20 UEs as one supervised process each |
| Slices | eMBB SST 1/`010203` and URLLC SST 2/`112233`, from [`slices.env`](../deployments/slices.env) |
| Observability | its own Prometheus + Grafana, native metrics from AMF/SMF/UPF/PCF, and two exporters |
| Network | `o5gs_net` 10.53.0.0/24; containers prefixed `o5gs-` |

### Slices are separate all the way to the user plane

| | eMBB | URLLC |
|---|---|---|
| S-NSSAI | SST 1 / SD 010203 | SST 2 / SD 112233 |
| 5QI · ARP | 9 · 8 | 82 · 2 (may pre-empt) |
| Session AMBR | 200 Mbps | 20 Mbps |
| DNN | `internet` | `urllc` |
| UE address pool | 10.45.0.0/16 | 10.46.0.0/16 |
| UPF device | `ogstun` | `ogstun2` |
| Subscribers (home slice) | IMSI …001–010 | IMSI …011–020 |

Giving each slice its own DNN, pool and UPF device is deliberate: every packet on `ogstun`
is eMBB and every packet on `ogstun2` is URLLC, so per-slice traffic can be **measured**
exactly rather than estimated. Each subscriber is also permitted on the other slice, so the
slice orchestrator has a real choice.

### Reproducibility

- Open5GS is pinned in [`manifest.lock`](../manifest.lock) to the **peeled** commit
  `157f611a…` — `refs/tags/v2.8.0` itself points at an annotated tag object.
- The image build verifies the checked-out SHA and the binaries record it
  (`/opt/open5gs/COMMIT`).
- Upstream's meson subprojects follow **branches** (freeDiameter `r1.5.0`, libtins `r4.5`,
  prometheus-client-c `open5gs`). The Dockerfile rewrites them to SHAs before building; an
  unmodified build is not reproducible.
- Base images are pinned by digest. UERANSIM is built from its pinned SHA, replacing the
  unpinned `free5gc/ueransim:latest` the free5GC stack used.

---

## Verified results

From [`docs/evidence/open5gs-20260914-060008Z`](evidence/open5gs-20260914-060008Z/00-summary.md)
unless stated otherwise.

| Claim | Result | How |
|---|---|---|
| All NFs up, no errors on first start | 11/11, 0 errors | container logs |
| gNB ↔ AMF (N2) | NG Setup successful | gNB log |
| UEs registered with a PDU session | **20/20** — 10 per slice | an interface per UE, on the slice's own pool |
| Ping through the UPF, both slices | 5/5, ~2 ms RTT | UE-side replies **and** byte counters on the slice's UPF device |
| Internet egress from a UE | 8.8.8.8 reachable | through the UPF's NAT |
| Throughput through a slice (single flow) | eMBB 256 Mbps · URLLC 24 Mbps | iperf3, 4 s |
| AMBR enforced per session | URLLC: **20.08 Mbps over 20 s** against 20 Mbps | iperf3 |
| Six flows at once | URLLC 20.5–20.6 Mbps each; eMBB 68–76 Mbps each (host-CPU bound; 117–132 in other runs) | [`08-traffic.txt`](evidence/open5gs-20260914-060008Z/08-traffic.txt) |
| KPI gate at steady state | **17/17 PASS** | [`07-kpi-gate.txt`](evidence/open5gs-20260914-060008Z/07-kpi-gate.txt) |
| Gate fails a broken deployment | exit 1 with all UEs stopped | live stack, no stub |

### Where AMBR is enforced, and why short tests overshoot

Neither Open5GS's UPF nor its SMF polices bitrate; the source has no rate limiter. The limit
is applied by **UERANSIM's gNB** (`RateLimiter`, `src/gnb/gtp/task.cpp`) using the session
AMBR the core signals over NGAP. Its token bucket holds one second of AMBR and starts full, so
a test of T seconds can reach AMBR × (T+1)/T. That predicted the 4-second overshoot exactly
(URLLC 23.85 Mbps against a 25.00 ceiling) and the 20-second convergence (20.08 against 21.00).

On real hardware the gNB and the UPF would both police this. Here the enforcement point is the
simulator — worth knowing before attributing a rate limit to the core.

eMBB sessions did **not** reach their 200 Mbps AMBR when several ran at once: the total is
bounded by what a userspace UPF and simulator can forward on this CPU (~330–470 Mbps peak).
That ceiling is the laptop's, not the slice's.

---

## Which metrics can be trusted

Every gauge was compared with ground truth — the UE interfaces that actually exist — on the
running stack.

| Metric | Verdict | Evidence |
|---|---|---|
| `fivegs_smffunction_sm_sessionnbr{snssai}` | ✅ **exact, per slice** | 10/10 in every audit |
| `ran_ue`, `amf_session`, `ues_active`, `gnb` | ✅ exact | 20, 20, 20, 1 |
| `fivegs_upffunction_upf_sessionnbr` | ⚠️ lower bound only | **21** for 20 real sessions after one UE restart |
| `fivegs_upffunction_upf_qosflows{dnn}` | ⚠️ presence only | **11** on `internet` for 10 real sessions, same event |
| `fivegs_smffunction_sm_qos_flow_nbr{snssai,fiveqi}` | ❌ leaks | 20/20, then 61/60, for 10/10 real — never decremented on re-attach |
| `fivegs_smffunction_sm_pdusessioncreationsucc` | ❌ double-counts | 40 per slice for 20 requests — a 200% success ratio |
| `fivegs_smffunction_sm_pdusessioncreationreq` | ❌ trap | also emits an unlabelled duplicate series; a plain `sum()` counts twice |
| UPF `…datavolumeqosleveln3upf` | ❌ cannot separate slices | labelled only by `qfi`, and both slices' default flow is QFI 1 |

### R-02 revisited

free5GC's metrics carried no slice identity at all. Open5GS's SMF carries S-NSSAI natively,
which closes R-02 **for sessions**. It does not close it **for traffic**: the UPF's volume
counters cannot tell slices apart. [`upf_slice_exporter.py`](../deployments/open5gs/observability/upf_slice_exporter.py)
fills that gap from the per-slice TUN devices. Counter direction was established by
measurement — 20 uplink packets of 1028 bytes moved `rx_bytes` by exactly 20 560 — after the
first version had uplink and downlink the wrong way round.

### The core cannot see UEs that disappear

With every UE stopped, the AMF still reported 20 UEs and the UPF still held 20 sessions, and a
KPI gate built on core metrics **passed**. The same blindness applies to a subtler failure (next
section): a UE whose user plane is dead still counts as connected everywhere in the core.

[`ue_probe_exporter.py`](../deployments/open5gs/observability/ue_probe_exporter.py) therefore
pings each slice's UPF gateway from **every UE's own interface** every 5 s, exporting
reachability and RTT per slice. This is the only liveness signal the gate trusts.

---

## UE supervision — a failure the core never reports

UERANSIM declares **radio link failure** when a UE hears nothing from the gNB for 2 s
(`HEARTBEAT_THRESHOLD`, `src/ue/rls/udp_task.cpp`). The UE then re-establishes its
registration with a Service Request and reports **CM-CONNECTED** — but its user plane never
returns. The gNB logs `Uplink data failure, PDU session not found` for every packet, while the
core still counts the session.

This happened to 3 of 20 UEs within 20 minutes, and was found only because the UE-side probes
showed 8/10 and 9/10 reachable while every core metric was green.

It was then **reproduced on demand**: freezing one UE process for 4 s produced the radio link
failure, the Service Request, CM-CONNECTED, and 0/3 ping replies.

[`ran/ue-entrypoint.sh`](../deployments/open5gs/ran/ue-entrypoint.sh) runs each UE as its own
process and checks every 10 s that it answers through the UPF. After 3 misses it restarts
**that UE only**. In the reproduction it detected the failure and restarted the UE 29 s
later; ping recovered to 3/3 and no other UE's process changed.

UEs are started one at a time: `nr-ue` takes the next free `uesimtunN` name, and two processes
starting together race for the same one (`TUNSETIFF: Device or resource busy` — observed).

---

## Slice isolation — measured

Does saturating eMBB hurt URLLC? [`isolation_test.sh`](../scripts/open5gs/isolation_test.sh)
compares an idle phase with a phase where only eMBB carries three downlink flows, using the
same UE-side probes for both. Three runs:

| Run | Phase | URLLC avg RTT | URLLC worst RTT | URLLC min reachable | eMBB DL peak |
|---|---|---|---|---|---|
| 1 | idle | 3.15 ms | 10.14 ms | 10/10 | — |
| 1 | eMBB loaded | 2.92 ms | 7.41 ms | **6/10** | 467 Mbps |
| 2 | idle | 3.42 ms | 10.95 ms | 10/10 | — |
| 2 | eMBB loaded | 3.39 ms | 8.62 ms | **8/10** | 353 Mbps |
| 3 | idle | 5.47 ms | 47.46 ms | 10/10 | — |
| 3 | eMBB loaded | 3.68 ms | 7.42 ms | **9/10** | 303 Mbps |

What this shows, and what it does not:

- **URLLC is not isolated from eMBB.** Under eMBB saturation, up to 4 of 10 URLLC probes per
  round went unanswered; at idle, none did. No supervisor restarts occurred, so these were lost
  packets, not dead UEs.
- **The damage is loss, not delay.** URLLC's average RTT did not rise under load.
- **URLLC's 10 ms budget is not met at the tail even when idle** (10.14, 10.95 and 47.46 ms
  worst probes). A software UPF and simulated RAN sharing a laptop CPU cannot hold a URLLC tail
  budget. The KPI gate therefore enforces the *average* and reports the worst case as an
  advisory that is expected to warn here.
- Three runs on one host, probe loss measured with a 1 s timeout. Treat the direction as solid
  and the exact figures as indicative.

The mechanism was not isolated. The simulator, the UPF and the probes all share one CPU and one
kernel; slices are separate in addressing, sessions and QoS parameters, not in compute.

---

## KPI gate

[`deployments/open5gs/kpi-gates.json`](../deployments/open5gs/kpi-gates.json) — 17 KPIs,
built only on metrics that passed the audit above. Same tool and rules as the free5GC gate
([`docs/kpi-gate.md`](kpi-gate.md)): no data fails, advisories never fail a build, an
unreachable Prometheus exits 2.

The user-plane KPIs that were `SKIP-ODE` on free5GC run here: sessions at the UPF, QoS flows
per slice, per-slice traffic, per-UE reachability, per-slice latency.

**Detection latency is by design, and measured:** a stopped exporter fails `no-scrape-failures`
immediately; UE liveness is windowed over 30 s so one lost probe in a 5 s round is not a dead
UE (it failed once the window passed, 45 s after stopping every UE); latency gates use 1 min.

### A bug this found in the free5GC gate

`count(up == 0)` returns **no result**, not 0, when nothing is down — and the gate treats no
data as a failure. The free5GC gate's `no-scrape-failures`, `sbi-5xx-rate` and
`no-latency-rejections` would have **failed a healthy deployment**. CI never caught it because
the stub Prometheus returned a literal 0. All three now end in `or vector(0)`, and the gate
refuses to load any count/sum gate with an upper bound that lacks the fallback.

---

## Engine restarts

The Docker engine on this machine is a native `docker-ce` running under systemd **inside the
Ubuntu WSL distro**. Only the CLI plugins (`docker compose`, `buildx`) come from Docker
Desktop, as symlinks into `/mnt/wsl/docker-desktop/`.

WSL shuts the distro down shortly after the last terminal attached to it closes, and systemd
stops `docker.service` with it — every container on both stacks goes down at once.

| Window | Terminal held open? | `docker.service` stops |
|---|---|---|
| 04:49 – 05:01 | no (one-shot `wsl` commands) | 11 |
| 05:01:45 – 05:16:45 | yes | **0** |
| 05:17:32 | just released | stopped within ~45 s |

Earlier project notes attributed these outages to "Docker Desktop bouncing". That was wrong
and has been corrected in the runbook.

What the stack does about it: every container has `restart: unless-stopped`, and the UEs are
the UE container's own workload, so they re-attach by themselves when the engine returns
(20 UEs back in 7–23 s, no re-provisioning — Open5GS completes SQN resynchronisation).

What you should do: **keep a WSL terminal open** while working with either stack. Microsoft
documents `vmIdleTimeout` in `.wslconfig` for the whole WSL VM; it documents no per-distribution
idle setting, so none is recommended here.

---

## Operating notes

- The two exporters run **inside** the UPF and UE containers, started by their entrypoints.
  They began as sidecars sharing those network namespaces; a sidecar is stranded in a dead
  namespace when its target restarts, and Docker gave up restarting it.
- `docker logs` keeps lines from before a restart — scope log checks with `--since` the
  container's `StartedAt`, or an old NG Setup line will satisfy them.
- Never `pkill -f <pattern>` inside `sh -c "…<pattern>…"`: it matches its own shell. This
  silently killed the iperf3 server start in the first verification script.
- Stopping the free5GC containers before load tests keeps measurements clean; `make up`
  restores them.

## Not done

- **No slice orchestrator integration yet.** The orchestrator still models capacity from
  provisioned AMBR; with measured per-slice throughput now available it could admit against
  what the network is actually carrying.
- **No compute isolation between slices** — see the isolation results.
- **Rollback on a failed gate** is still missing, as on free5GC.
- The SPEC acceptance script (`tests/acceptance.sh`) targets free5GC and has not been ported.
