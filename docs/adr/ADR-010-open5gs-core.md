# ADR-010 — Add Open5GS as a second 5G core

**Status:** Accepted (project owner's decision, 2026-09-14)
**Supersedes:** nothing. Amends the SPEC's single-core assumption (ADR-001, ADR-002, §3).

## Context

Phases 0 through 2 were built on free5GC v4.2.3. Two limits kept coming back, and neither
could be solved from inside free5GC on this hardware:

1. **No user plane (R-01).** free5GC's UPF forwards packets through the `gtp5g` kernel
   module. `gtp5g` builds against a standard Linux kernel and cannot load on WSL2. Without a
   UPF there are no PDU sessions, no UE IP addresses and no user traffic, so 7 acceptance
   criteria stayed ODE-only and every capacity figure in the slice orchestrator had to be
   *modelled* rather than measured.

2. **No slice identity in telemetry (R-02).** None of free5GC's native metrics carries an
   S-NSSAI. Slice-labelled metrics had to be reconstructed from AMF logs by a separate
   exporter, which is an ADR-004 rung-2 workaround, not a property of the core.

The project owner decided to build the system on Open5GS.

## What was verified before committing to it

Claims about Open5GS were checked against its source at the pinned commit, not taken from
documentation:

| Question | Finding | Where |
|---|---|---|
| Does the UPF need a kernel module? | **No.** It opens TUN devices and forwards in userspace. | `src/upf/gtp-path.c` → `ogs_tun_open()` |
| Can this laptop provide TUN inside a container? | **Yes.** `ip tuntap add` with `NET_ADMIN` + `/dev/net/tun` succeeded on the WSL2 kernel. | tested 2026-09-14 |
| Does the core export per-slice metrics? | **Yes, from the SMF:** `fivegs_smffunction_sm_sessionnbr`, `…pdusessioncreationreq/succ` carry `{plmnid, snssai}`; `…qos_flow_nbr` adds `fiveqi`. | `src/smf/metrics.c` |
| Does the SMF require 4G Diameter config? | No; it logs a warning and continues. | `src/smf/fd-path.c` |
| Is SCTP available for N2? | Yes, in the WSL2 kernel the Docker engine runs on. | `/proc/net/protocols` |

## Decision

1. **Open5GS is added beside free5GC, not in place of it.** It gets its own compose project
   (`deployments/open5gs/`), network `10.53.0.0/24`, database and `o5gs-` container prefix.
   The free5GC deployment, its evidence bundles and the results in the review deck are
   unchanged and still reproducible.

2. **Pinned exactly like the other upstreams.** `manifest.lock` pins Open5GS v2.8.0 to its
   *peeled* commit `157f611a…` (the tag ref resolves to an annotated tag object, which is
   not a checkout target). `bootstrap.sh --verify-only` covers it with no code changes.

3. **Images are built from source, not pulled.** Open5GS publishes no official images, and
   third-party ones do not record their source commit. The Dockerfile pins the base image by
   digest, verifies the checked-out SHA, and **rewrites the meson subproject wraps to SHAs**
   — upstream points three of the four at branches, so an unmodified build is not
   reproducible. UERANSIM is also built from its pinned SHA, closing the M0 open item about
   `free5gc/ueransim:latest`.

4. **Slices are identical on both cores.** Both are provisioned from `deployments/slices.env`
   (eMBB SST 1/`010203`, URLLC SST 2/`112233`, same 5QI, ARP and AMBR), so a comparison is
   fair.

5. **Each slice gets its own DNN and address pool** — eMBB `internet` → `10.45.0.0/16` on
   `ogstun`, URLLC `urllc` → `10.46.0.0/16` on `ogstun2`. This is an implementation choice
   with a purpose: user traffic becomes attributable to a slice at the UPF by address and by
   device, which is what lets per-slice throughput be *measured*.

## Consequences

- The user plane can be proven on this laptop. The ODE-only acceptance criteria that exist
  only because of `gtp5g` (PDU session, UE IP, end-to-end connectivity) become runnable on
  the Open5GS stack. Those that are about the ODE itself (the `VERSIONS.lock` hardware
  freeze, the bare-metal baseline tag) do not.
- R-02 is re-opened as a question rather than a certainty: per-slice *session* metrics exist
  natively; whether per-slice *traffic* metrics exist at the UPF must be checked empirically.
- The SPEC is written around free5GC (its title, §3, ADR-004's instrumentation ladder).
  Those sections remain accurate for the free5GC stack; nothing here rewrites them.
- Two stacks cost memory on a 7.6 GiB host. Open5GS NFs are C processes with small
  footprints, but the two cores should not be load-tested at the same time.
- Tooling written against free5GC names — the slice exporter, the KPI gate definitions, the
  acceptance script — does not apply to Open5GS unchanged and must be ported deliberately,
  not assumed to work.

## Outcome (measured 2026-09-14)

Full detail and evidence in [`docs/open5gs.md`](../open5gs.md).

- **User plane proven on this laptop.** 20/20 UEs with PDU sessions, ping and iperf3 through
  the UPF on both slices, internet egress, AMBR enforced per session (URLLC 20.08 Mbps over
  20 s against 20 Mbps). The KPI gate's user-plane KPIs run instead of skipping: 17/17 PASS.
- **R-02 closed for sessions, not for traffic.** The SMF's session gauge is labelled by S-NSSAI
  and exact. The UPF's volume counters are labelled only by QFI and cannot separate slices;
  per-slice traffic comes from the slice TUN devices instead.
- **Several native gauges read wrong** (QoS flows leak, PDU-session successes double-count, UPF
  sessions over-count after a UE restart) and are excluded from the gate.
- **The core does not notice UEs that vanish or lose their user plane.** Liveness is measured
  from the UE side, and a UE whose user plane dies after a simulated radio link failure is
  restarted individually.
- **Slices were not isolated on a shared gNB and UPF.** Saturating eMBB cost URLLC packet loss (up
  to 4 of 10 probes per round), not delay. Superseded by
  [ADR-011](ADR-011-dedicated-user-plane-per-slice.md): with a gNB and UPF per slice URLLC holds
  10/10. CPU is still shared, and URLLC's 10 ms tail budget is missed in some idle minutes.

Follow-on decisions: [ADR-011](ADR-011-dedicated-user-plane-per-slice.md) (user plane per slice),
[ADR-012](ADR-012-ueransim-udp-buffers.md) (delivering the admitted capacity),
[ADR-013](ADR-013-kubernetes-open5gs.md) (Kubernetes).
- **The "Docker Desktop bounces" diagnosis was wrong.** The engine runs inside the Ubuntu WSL
  distro and stops when WSL idles it; proven by holding a session open (0 stops in 15 min vs 11
  in 12 min).
