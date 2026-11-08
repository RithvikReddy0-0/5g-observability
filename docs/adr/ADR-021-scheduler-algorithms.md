# ADR-021 — Scheduler algorithms: strict priority, DRR and HFSC beside HTB, on the same classifier

**Status:** Accepted, 2026-11-08 — the default stays HTB (`sliced`); see "Decision on the default"
**Builds on:** ADR-019 (packet scheduler: RLS classifier, HTB tree, the cell on the UE container's link)
**Evidence:** `docs/evidence/open5gs-scheduler-algorithms/` (README, per-run directories, overload runs)

## Context

ADR-019 chose HTB for the cell because it gives each slice a guaranteed rate and lends the idle
rest by priority. It was never compared with the other scheduling disciplines a MAC scheduler or a
router could use. The question is which discipline best serves three slices with different needs on
one shared cell: URLLC (small packets, a delay bound), eMBB (as much throughput as possible, fairly
shared), mMTC (small, rare reports that must not be lost), with radio signalling that must never be
starved (heartbeats lost = radio-link failure, ADR-019).

## What this kernel can run

Each qdisc was created on a dummy interface in a throwaway network namespace (inside the scheduler
container, `unshare -n`), kernel `6.6.87.2-microsoft-standard-WSL2`, iproute2 7.0.0. The kernel
config (`/proc/config.gz`) agrees:

| qdisc | Works | Kernel option |
|---|---|---|
| htb, prio, drr, hfsc, qfq, tbf, sfq, fq, fq_codel, codel, red | yes | `=m` |
| cake | **no** ("Specified qdisc kind is unknown") | `CONFIG_NET_SCH_CAKE` is not set |
| pie, fq_pie | **no** | `CONFIG_NET_SCH_PIE` is not set (fq_pie needs it) |
| ets | **no** | `CONFIG_NET_SCH_ETS` is not set |
| taprio | **no** | `CONFIG_NET_SCH_TAPRIO` is not set |
| cbs | **no** | `CONFIG_NET_SCH_CBS` is not set |

So CAKE (diffserv tins), ETS and taprio could not be implemented here: they would need a custom WSL
kernel, which this project does not ship. PIE is an AQM rather than a scheduler and is equally
absent. QFQ works but was not added: it is another weighted-fair approximation, and DRR already
represents that family.

## Decisions

### 1. One classifier, four trees

Every mode keeps ADR-019's classifier unchanged — the same u32 filters on the same RLS offsets put
every packet into the same logical class: signalling, URLLC ARP 1, URLLC ARP 2, eMBB, mMTC, other.
Only what serves those classes changes (`tools/packet-scheduler/scheduler.py`, `POST /config
{"mode": ...}`):

| Mode | Discipline | How it is built | What it promises |
|---|---|---|---|
| `sliced` (default) | HTB | ADR-019 tree: guaranteed rate per class, borrowing up to the cell by HTB prio | rate guarantees; priority only for spare capacity |
| `prio` | strict priority | one HTB class shapes the cell; under it one `prio` qdisc with a band per class, in ADR-019's priority order | the highest backlogged band always goes first; no rate guarantee for lower bands |
| `drr` | Deficit Round Robin (Shreedhar & Varghese 1995) | the same HTB cell class; under it `drr`, one class per logical class, quantum ∝ its share (smallest = one 1514-byte packet; eMBB 30×) | throughput shares in proportion to the quanta, O(1) per packet; delay depends on the round length, so on the quanta |
| `hfsc` | Hierarchical Fair Service Curve (Stoica, Zhang & Ng 1997) | the ADR-019 class tree; root class `ul` = the cell; every class link-shares by its share; signalling and URLLC tiers also get a real-time curve | URLLC's delay bound is set by its own curve (`dmax`), independent of its share |

The leaves are the same in every mode: `pfifo` for signalling (256) and URLLC (64), `fq_codel`
(RFC 8290) for eMBB, mMTC and other.

Two details that the measurements depend on:

- **`prio` and `drr` keep HTB only as the shaper**: a single class at the cell rate with the same
  burst as `sliced`, so at the same cell the only difference is the scheduler under it.
- **`drr` drops whatever no filter claims** (it has no default class). Every flat tree therefore ends
  with a `protocol all` catch-all filter to "other" — without it, ARP on the UE container's link
  would be dropped and the container would lose its neighbours. Unit-tested
  (`test_non_ip_packets_reach_the_default_class_in_every_mode`).

### 2. HFSC curves: delay decoupled from share

For a delay-critical class with guaranteed rate `r` (its share of the cell), the real-time curve is
chosen so that a packet of `umax` = 1514 bytes arriving at an idle class leaves within `dmax`:

- URLLC ARP 1: `dmax` 1 ms; URLLC ARP 2: 2 ms; signalling: 5 ms.
- If `umax/dmax > r` the curve is concave: slope `umax/dmax` for `dmax`, then `r`. Otherwise it
  is `0` for `dmax − umax/r`, then `r`. This is the same conversion `tc` applies to `rt umax .. dmax
  .. rate ..`; `hfsc_rt()` writes it out so the unit tests can check it.
- On the 250 Mbps downlink: ARP 1 = `m1 12.1 Mbit d 1 ms m2 7.5 Mbit`, whatever URLLC's share —
  `test_hfsc_delay_bound_does_not_depend_on_the_share` checks the 1 ms bound at shares of 10 % and 2 %.
- The real-time curves add up to < 25 Mbps on either direction, well inside the cell, so they are
  admissible. Linux HFSC does not cap real-time service by `ul`; with these light classes the cell
  can be exceeded by at most that much, and only while they are backlogged.
- eMBB, mMTC and other have link-sharing curves only (`ls m2` = share): under overload each gets at
  least its share, the rest goes in proportion.

### 3. `directions`

`POST /config {"directions": ["uplink"]}` (or `SCHED_DIRECTIONS`) schedules only one direction and
leaves the other at Docker's default (`noqueue` on a veth). Built for the URLLC-tail investigation;
the default is both.

## How they were compared

Same day, same stack, same code (commit `61f4c5b`; the modes are from `36f49ad`), only `POST /config` changed between runs:

1. **Nominal load** — the standard KPI instance (`tools/kpi/run_kpis.py standard`: fresh attach of
   all UEs, 20 eMBB TCP downloads, URLLC 64 B every 20 ms × 10 devices, mMTC reports, 150 s) at the
   standard cell (250 / 100 Mbps), four runs per mode in round-robin order (forward, reverse,
   forward, reverse), interleaved with the URLLC-tail controls (`docs/evidence/open5gs-urllc-tail/`).
2. **Overload** — `experiment.py` part D (eMBB offers 150 Mbps into a 100 Mbps cell, URLLC both
   tiers, mMTC and an unloaded eMBB device probed by ping to their UPF), counting radio-link failures
   and every UE's data path after each run; the stack is re-attached before any run that would
   inherit broken UEs. Four runs each for off, sliced, drr and hfsc; two for prio and fifo, which
   were disqualified by their first two.

All on 2026-11-08 (UTC), code `61f4c5b`.

## Results

| | sliced (HTB) | prio | drr | hfsc |
|---|---|---|---|---|
| Nominal: URLLC p99, median (range), ms | 9.3 (6.6–10.6) | 9.1 (7.4–10.8) | 10.4 (6.7–14.5) | 9.5 (8.2–11.6) |
| Nominal: eMBB aggregate, Mbps | 225 (223–229) | 224 (223–229) | 221 (218–229) | **202 (184–219)** |
| Nominal: eMBB 5th percentile, Mbps | 11.0 | 9.0 | 9.1 | 9.9 |
| Nominal: URLLC / mMTC loss, overall score | 0 / 0, 0.77 | 0 / 0, 0.76 | 0 / 0, 0.75 | 0 / 0, 0.76 |
| Overload: radio-link failures per run | 0, 0, 0, 0 | **88, 180** | 0, 0, 0, 0 | 0, 0, 0, 0 |
| Overload: mMTC probe loss | 0 % | **100 %** | 0 % | 0 % |
| Overload: URLLC p50 / p99, median, ms | 1.8 / 6.0 | 2.2 / 21.3 (95 % loss in run 2) | **3.8** / 11.2 | 1.9 / 9.4 |

- At nominal load no algorithm differs from HTB on URLLC (p ≥ 0.63, 4 vs 4; within-round ranks,
  Friedman p = 0.65). The run-to-run spread inside one mode is larger than any difference between
  modes.
- Strict priority starves the lower bands as soon as the cell is overloaded, and in this emulator
  starvation becomes mass radio-link failure. A FIFO cell does the same (97–98 RLF).
- DRR's URLLC median latency under load is its round length (eMBB quantum 45 420 B ≈ 3.6 ms at
  100 Mbps): 3.6–4.2 ms in every run, against 1.6–2.0 ms for HTB and HFSC.
- HFSC's decoupled delay bound did not show, because URLLC is never backlogged on this testbed: HTB's
  priority already serves it first. HFSC also delivered less eMBB through the same configured cell.

Full tables, all runs and the statistics: `docs/evidence/open5gs-scheduler-algorithms/README.md`.

## Decision on the default

**The default stays `sliced` (HTB).** No algorithm was clearly and repeatably better on both
normal-load KPIs and overload safety: prio is unsafe under overload, DRR costs URLLC latency under
load, HFSC costs eMBB throughput at nominal load with no URLLC gain. prio, drr and hfsc stay
available as modes for experiments (`POST /config {"mode": ...}`).

## Consequences

- The comparison is limited to what this kernel compiles: CAKE, PIE/FQ-PIE, ETS and taprio need a
  custom WSL kernel or the ODE's hardware. The same scripts would compare them unchanged once
  `scheduler.py` has a tree for them.
- HFSC would be worth re-testing in a scenario where URLLC itself queues (a URLLC load near its
  guarantee), which is where decoupling delay from rate matters.

## References

- I. Stoica, H. Zhang, T. S. E. Ng, "A hierarchical fair service curve algorithm for link-sharing,
  real-time and priority services", Proc. ACM SIGCOMM '97, pp. 249–262, 1997. doi:10.1145/263105.263175
- M. Shreedhar, G. Varghese, "Efficient fair queueing using deficit round robin", Proc. ACM SIGCOMM
  '95, pp. 231–242, 1995. doi:10.1145/217382.217453
- T. Høiland-Jørgensen, D. Täht, J. Morton, "Piece of CAKE: A Comprehensive Queue Management
  Solution for Home Gateways", 2018 IEEE LANMAN, pp. 37–42. doi:10.1109/LANMAN.2018.8475045
  (not runnable on this kernel)
- R. Pan, P. Natarajan, F. Baker, G. White, "Proportional Integral Controller Enhanced (PIE)",
  RFC 8033, February 2017 (not runnable on this kernel)
- T. Høiland-Jørgensen, P. McKenney, D. Täht, J. Gettys, E. Dumazet, "The Flow Queue CoDel Packet
  Scheduler and Active Queue Management Algorithm", RFC 8290, January 2018 (the eMBB/mMTC leaf)
