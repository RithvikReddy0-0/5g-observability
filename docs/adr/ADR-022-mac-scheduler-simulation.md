# ADR-022 — What a real MAC scheduler would do: a slot-level simulation of the three slices

**Status:** Accepted, 2026-11-09
**Builds on:** ADR-015 (traffic profiles), ADR-018 (algorithms chosen by comparison), ADR-019 (packet
scheduler: slices scheduled on a shared cell at the IP layer)
**Answers:** the guide's question after ADR-019: "what would a real MAC scheduler do?"
**Nature:** **SIMULATION.** Nothing in this ADR was measured on the testbed.

## Context

ADR-019 schedules the slices on a shared cell, but at the IP layer: UERANSIM has no MAC layer, no
PRBs, no slots and no channel. Its "cell" is a link with a declared capacity, and every packet costs
the same whatever the UE's radio. A real gNB is different in three ways:

1. it schedules **resource blocks in time and frequency**, every slot (0.5 ms at 30 kHz);
2. what a resource block carries depends on **that UE's channel on that block, in that slot**
   (its CQI), so the same share of the cell is worth more to one UE than to another;
3. it schedules in **two levels** when the cell is sliced: between slices, then between each slice's users.

None of this can be measured on the testbed. It can be simulated. ADR-018 set the rule for
simulations: run the strongest candidates from the literature under the same conditions, with
fixed seeds, and say exactly what is modelled.

## Decisions

### 1. A slot-level downlink simulator of one cell (`tools/mac-sim/macsim.py`)

| Part | Choice | Why / source |
|---|---|---|
| Numerology | μ = 1: 30 kHz SCS, 0.5 ms slots | the usual FR1 TDD numerology (n78) |
| Bandwidth | 40 MHz = **106 PRBs** | TS 38.101-1 Table 5.3.2-1. Gives 14 RBGs, few enough for pure Python, and a cell of ~86 Mbps (in overload), the same order as the testbed's 100–250 Mbps cells |
| Allocation unit | type 0, **RBG = 8 PRBs** (13 × 8 + 1 × 2) | TS 38.214 Table 5.1.2.2.1-1 (BWP 73–144 PRBs, configuration 1) |
| Rate per RBG | CQI → spectral efficiency of **TS 38.214 Table 5.2.2.1-2** (4-bit CQI table 1) × 132 RE per PRB (12 PDSCH symbols × 12 subcarriers − 12 DMRS RE), one layer | the standard's own efficiency per CQI |
| SNR → CQI | highest CQI whose efficiency ≤ log2(1 + SNR / Γ), Γ = 3 dB | **assumption**. Real link curves are vendor-specific; this puts CQI 1 at −6.5 dB and CQI 15 at 19.6 dB |
| Mean SNR | UEs uniform by area in an annulus; edge SNR + 35·log10(R/d) + log-normal shadowing | **assumption** (synthetic). Same drop for every scheduler of a seed |
| Fading | 4 equal-power Rayleigh taps, each a Gauss-Markov process with ρ = J0(2π f_d T) (Jakes), 3 km/h at 3.5 GHz; frequency response per RBG | **assumption** (synthetic); frequency-selective, so RBGs differ per UE |
| CSI | CQI every 5 slots (2.5 ms); channel constant in between; no CSI error | so a transmission at the reported CQI always succeeds: **BLER 0, HARQ not modelled** |
| Transport block | each RBG carries bits at its own CQI | simplification (a real TB has one MCS over all its PRBs). RadioSaber's own synthetic experiments did the same |
| Traffic | eMBB 20 UEs: Poisson 1500 B packets, or full buffer; URLLC 3 critical + 7 standard, **64 B every 20 ms, 10 ms budget**; mMTC 70 UEs, **10 B every 10 s**, random phase | the testbed's profiles (ADR-015, `tools/traffic/README.md`) |
| URLLC discard | a packet older than its 10 ms budget is dropped and counted as a miss | the PDCP discard timer (TS 38.323). Without it, one overload makes every later packet late |

Determinism: every random stream is seeded from (seed, radio, UE). All schedulers of a seed see the same
UE positions, the same fading and the same packets (common random numbers; a unit test pins it).

### 2. Intra-slice schedulers: the five classics

Each scheduler is **greedy per RBG**: for a given RBG, it names the user with the highest metric
among those whose backlog is not yet covered this slot. r = bits the user's CQI gives on that RBG,
R̄ = the user's average throughput (EWMA, 50 ms), W = head-of-line delay.

| Scheduler | Metric | Source |
|---|---|---|
| Round Robin | next backlogged user in turn (pointer per RBG grant) | — |
| Max-C/I | r | — |
| Proportional Fair | r / R̄ | Jalali, Padovani, Pankaj, VTC 2000 (CDMA-HDR) |
| M-LWDF | a·W·r / R̄, a = −ln δ / T (real-time); r / R̄ otherwise | Andrews et al., IEEE Commun. Mag. 2001 |
| EXP/PF | exp((aW − ⟨aW⟩) / (1 + √⟨aW⟩))·r / R̄ (real-time); r / R̄ otherwise | Rhee, Holtzman, Kim, VTC 2003-Spring |

EXP/PF's adaptive weight w(t) for non-real-time flows is fixed at 1 here. The exponential form is the
one LTE scheduling surveys give; we checked the paper's bibliographic record but not its full text.

### 3. Inter-slice schedulers: four policies, one ablation, one baseline

Weights are the packet scheduler's guarantees (eMBB 60 %, URLLC 10 %, mMTC 5 %), normalised over
the three slices where a policy needs weights that sum to 1.

| Policy | What it does | Channel-aware between slices? | Work-conserving? |
|---|---|---|---|
| **static split** | each slice gets its share of RBGs every slot (fractions carried over), in fixed contiguous ranges | no | **no**: an idle slice's RBGs stay idle |
| **NVS** | the whole slot to the slice with the highest weight / (EWMA of slots it got), β = 0.01 | no | across slots only |
| **priority** (ADR-019 analogue) | HTB-like: in priority order (URLLC, eMBB, mMTC) each slice takes up to its *guaranteed* share, then leftover RBGs go in priority order. Inside URLLC, critical devices first | no | yes |
| **quota-seq** (ablation) | RadioSaber's quotas, but RBGs handed out in index order | no | yes |
| **RadioSaber** | per slot: quota per slice from the weights, with a rolling offset for rounding and the share of slices with no data split among the others (Algorithm 1); then repeatedly grant the (RBG, slice) pair with the best channel quality, where each slice's own scheduler says which of its users would get that RBG, until each slice reaches its quota (Algorithm 2) | **yes** | yes (see below) |
| **none** | no slicing: one M-LWDF scheduler over all 100 UEs | — | yes |

**RadioSaber, faithfully.** Chen, Yao, Hassanieh, Mittal (NSDI 2023) break the circular dependency
between a channel-aware inter-slice scheduler and channel-aware per-slice schedulers by making both
greedy. The inter-slice scheduler *asks* each slice's scheduler which user it would put on an RBG,
and reads that user's channel quality. We implemented Algorithms 1 and 2 of the paper and checked
them against the authors' public code (`elvinlife/RadioSaber`, `downlink-transport-scheduler.cpp`:
the `MaximizeCell` assignment that their scenario uses for "RadioSaber"). We differ in three
documented ways:

- the offset carries only the rounding of the quota (target − quota). Carrying the RBGs actually
  used made idle slices bank credit without bound once leftovers were filled;
- RBGs that no slice can use within its quota (its users' backlogs are covered) go to the slice
  that can use them best (a work-conserving fill). The paper's simulator would leave them to padding;
- quotas are in RBGs (the paper works in PRBs and divides by the RBG size). The results are the
  same when RBGs are equal; ours have one short last RBG.

NVS follows the slice-selection rule of the RadioSaber authors' NVS baseline
(`downlink-nvs-scheduler.cpp`: weight ÷ EWMA of slots served, β = 0.01, one slice per slot). The NVS
paper (Kokku et al., IEEE/ACM ToN 2012) is the source of the idea. We verified its bibliographic
record, not this exact rule in its text.

### 4. Experiments

Every inter-slice policy uses the same per-slice schedulers: PF for eMBB, M-LWDF for URLLC, RR for
mMTC. Each slice gets its own scheduler, which is the RadioSaber idea. Every intra-slice scheduler
is run under the static split, the same scheduler in all three slices. That makes 11 configurations
× 4 scenarios × **10 seeds**. Each run is 5.5 s (11 000 slots); the first 0.5 s is warm-up and is
excluded. Results are the mean ± the 95 % confidence half-width (Student's t, 9 degrees of freedom).

| Scenario | eMBB | URLLC | Radio |
|---|---|---|---|
| normal | 30 Mbps offered (below capacity) | profile | base: edge +2 dB, shadowing 4 dB, mildly selective |
| overload | full buffer (demand > capacity) | profile | base (same drop as normal) |
| urllc-flood | full buffer | the 7 standard devices send 64 B every 0.25 ms (14.3 Mbps), the ADR-019 B analogue | base |
| spread | full buffer | as urllc-flood | edge −4 dB, shadowing 8 dB, strongly frequency-selective: where channel awareness should matter |

## Verification

- **Unit tests** (`tools/mac-sim/test_macsim.py`, 20 tests, about 3 s, in CI): the CQI table is TS 38.214's (every
  efficiency = Qm × R / 1024); RR gives equal turns whatever the channel; Max-C/I picks the best CQI
  on every RBG; PF converges to equal time shares on a two-UE cell, and throughputs in the ratio of the
  rates, while Max-C/I starves the weaker UE; PF rides the peaks of a fading channel (> 1.3 × RR);
  M-LWDF and EXP/PF serve the older head-of-line packet first, and M-LWDF serves an old packet on a worse
  channel where Max-C/I does not; static split delivers 0.80 / 0.133 / 0.067 of the RBGs to within
  0.5 %; NVS follows the weights; RadioSaber's quotas follow the weights; **RadioSaber carries > 1.4 ×
  static split and > 1.4 × quota-seq on a channel-diverse two-slice cell**, still splitting 50/50; it
  asks a slice again after a grant; priority honours guarantees before borrowing and serves critical
  URLLC devices first; runs are deterministic; every scheduler sees the same radio and packets.
- **Evidence**: `docs/evidence/mac-sim/`, every run in `runs.csv`, the summary in `summary.csv` and
  `results.json`, the tables in `results.md`. All are generated by the command there.

## Results

**SIMULATION.** 440 runs, 10 seeds each, mean ± 95 % CI
([evidence](../evidence/mac-sim/README.md); all tables in `results.md`).

**Inter-slice** (eMBB PF, URLLC M-LWDF, mMTC RR inside the slices)

| Policy | overload: eMBB | overload: URLLC miss | flood: eMBB | flood: URLLC miss | flood: critical miss | spread: SE of used RBGs | spread: URLLC miss |
|---|---|---|---|---|---|---|---|
| static split | 74.1 ± 2.9 Mbps | 1.63 ± 1.21 % | 74.1 ± 2.9 | 53.2 ± 8.2 % | 1.20 % | 2.09 bit/RE | 65.5 % |
| NVS | 78.1 ± 3.8 | 0.22 ± 0.29 % | 74.4 ± 3.0 | 39.8 ± 8.1 % | 0.11 % | 2.14 | 56.9 % |
| **priority** (ADR-019 analogue) | 85.3 ± 3.4 | **0.07 ± 0.12 %** | 55.2 ± 2.2 | 14.7 ± 6.2 % | 0.24 % | 1.89 | 37.5 % |
| quota-seq (ablation) | 85.4 ± 3.4 | 1.87 ± 1.35 % | **77.1 ± 3.1** | 57.1 ± 7.9 % | 1.35 % | 2.11 | 65.9 % |
| **RadioSaber** | **85.5 ± 3.4** | 0.35 ± 0.35 % | 73.1 ± 2.8 | 34.7 ± 8.4 % | 0.75 % | **2.18** | 52.0 % |
| none (one M-LWDF) | 85.3 ± 3.4 | 0.07 ± 0.12 % | 54.1 ± 3.6 | **5.1 ± 3.9 %** | 0.01 % | 1.92 | **29.2 %** |

**Paired by seed** (same radio and packets, so the radio's variance cancels; A − B):

| | overload | urllc-flood | spread |
|---|---|---|---|
| RadioSaber − quota-seq: Δ SE of used RBGs | +0.016 ± 0.002 bit/RE | −0.007 ± 0.013 | **+0.070 ± 0.028** |
| RadioSaber − quota-seq: Δ URLLC miss | −1.5 ± 1.0 pp | **−22.4 ± 2.3 pp** | **−13.9 ± 4.5 pp** |
| RadioSaber − quota-seq: Δ eMBB | +0.11 ± 0.07 Mbps | −3.97 ± 0.25 | −1.30 ± 0.38 |
| RadioSaber − static: Δ eMBB | **+11.5 ± 0.5 Mbps** | −0.9 ± 0.2 | +0.9 ± 0.5 |
| priority − static: Δ eMBB / Δ URLLC miss | +11.3 Mbps / −1.6 pp | **−18.9 Mbps / −38.5 pp** | −13.6 Mbps / −28.0 pp |

**Intra-slice** (static split, the same scheduler in all slices)

| Scheduler | overload: eMBB | eMBB 5th pct UE | eMBB Jain | flood: URLLC miss | flood: critical miss |
|---|---|---|---|---|---|
| Round Robin | 49.4 ± 2.9 Mbps | 0.70 Mbps | 0.69 | 63.2 ± 7.2 % | 4.95 % |
| Max-C/I | **127.1 ± 2.7** | 0.00 | 0.18 | 66.2 ± 2.4 % | **53.3 ± 16.2 %** |
| Proportional Fair | 74.1 ± 2.9 | **1.42** | **0.79** | 59.1 ± 6.2 % | 1.01 % |
| M-LWDF | 74.1 ± 2.9 | 1.42 | 0.79 | **53.2 ± 8.2 %** | 1.20 % |
| EXP/PF | 74.1 ± 2.9 | 1.42 | 0.79 | 53.5 ± 8.1 % | 1.12 % |

What this says, in this model:

1. **Lending idle resource blocks is worth more than anything else for throughput.** Every policy that
   lends them within the slot carries ~85 Mbps of eMBB in overload, against 74 for a static split
   (+11.5 Mbps paired, +15 %). The slicing literature's first lesson, hard partitions waste the cell,
   holds at slot level too.
2. **Channel awareness between slices helps the small slice, not cell throughput.** With the quotas
   fixed (quota-seq), choosing RBGs by channel cuts URLLC's misses by 22 pp under the flood and 14 pp
   on the spread radio. URLLC devices get blocks where *their* channel is good. Cell spectral
   efficiency rises only 0.07 bit/RE (+3 %) on the spread radio and not at all on the base one. That
   is far from RadioSaber's reported 17–72 %, for an identifiable reason: here one slice (eMBB, 20 UEs,
   80 % of the weight) owns most blocks, and its own PF scheduler already finds a good user on almost
   every block. RadioSaber's evaluation used up to 20 slices of comparable weight. The model does not
   contradict the paper; it shows the gain depends on the slice mix.
3. **The packet scheduler's design (priority with guarantees, ADR-019) is the best sliced policy for
   URLLC**: 0.07 % misses at nominal load and 14.7 % under the flood, against 34.7 % for RadioSaber.
   It also costs eMBB the most when URLLC floods (−18.9 Mbps against static), because URLLC borrows
   first. This is the same trade-off ADR-019 measured on the testbed (scenario B), now at slot level
   with a radio. Without slicing, one M-LWDF does better still for URLLC (5 %), but nothing protects
   eMBB from the flood (54 Mbps).
4. **Inside a slice**, PF is the right default for eMBB: Max-C/I gains +72 % throughput by starving
   the edge (5th percentile 0, Jain 0.18), and RR loses a third of the throughput. For URLLC, any
   channel-only scheduler is dangerous when one tier floods: Max-C/I lets the flooders starve the
   critical devices (53 % miss). PF, M-LWDF and EXP/PF hold them near 1 %. The delay-aware pair also
   lowers the total miss rate (53 % against 59 %).
5. At nominal load the intra-slice choice does not matter for URLLC (64 B every 20 ms is ~0.3 % of
   the cell). What matters is **where** its blocks are. Policies that pin URLLC to a position in the
   band (static split, quota-seq) leave a device fading there waiting: 0.6–1.9 % misses. Policies that
   let it take any block (NVS, priority, RadioSaber, none) stay at or below 0.41 %.
6. On the spread radio, coverage limits everything. Devices whose mean SNR is below CQI 1 are served
   only on fading peaks, whatever the scheduler (eMBB 5th percentile ~0.2 Mbps; mMTC p95 ~0.1 s with
   wide intervals).

## Consequences

- (+) The guide's question has a quantitative answer, labelled as simulation: what a slot-level MAC
  scheduler would do with these three slices, these shares and this traffic, against five intra-slice
  and six inter-slice policies, with confidence intervals and paired comparisons.
- (+) It supports ADR-019's design choice. Among the policies a real gNB could run, priority with
  guaranteed shares and borrowing gives URLLC the lowest misses. Its cost to eMBB under a URLLC
  flood is the same trade-off measured on the testbed.
- (+) Deterministic, standard library only, about 7 minutes for the whole study; a unit test per
  scheduler property runs in CI in a few seconds.
- (−) **It is a model, not the testbed, and not a real RAN.** None of these numbers can be quoted as
  a measurement. On a real gNB (OpenAirInterface, srsRAN or a commercial one) these would differ:
  - **HARQ and BLER**: link adaptation targets ~10 % BLER, and retransmissions add 1–2 ms or more.
    Here every transmission succeeds, so URLLC latency is optimistic;
  - **CSI**: reports are delayed, quantised and wrong in fades. Here CQI is exact and fresh every
    2.5 ms, which flatters every channel-aware scheduler (Max-C/I, PF, RadioSaber);
  - **one MCS per transport block**, PDCCH capacity limits (how many UEs per slot), uplink, MIMO and
    TDD patterns (n78 is TDD: downlink slots only part of the time) are not modelled;
  - **the channel is synthetic**: the path-loss, shadowing, SNR-gap and tap-delay values are
    assumptions chosen for a plausible spread, not a 3GPP deployment or channel model (TR 38.901).
    A real CQI trace can replace it (`tools/mac-sim/README.md`);
  - **EXP/PF**'s adaptive weight for non-real-time flows is fixed at 1, and the NVS rule follows the
    RadioSaber authors' baseline code;
  - **RadioSaber** has two documented deviations: the offset carries only rounding, and leftover
    blocks are filled work-conservingly.
- (−) **How it relates to ADR-019.** The IP-level packet scheduler decides *which packet goes next*
  on a link of fixed capacity; every byte costs the same. The MAC scheduler decides *which resource
  block goes to whom*; a byte costs more for a cell-edge UE. So:
  - ADR-019's shares are shares of a **bit rate**. The same shares here are shares of **resource
    blocks**, worth different bit rates to different UEs;
  - channel-aware policies (PF, RadioSaber) have no counterpart on the testbed; they cannot be built
    there, because UERANSIM has no channel;
  - the priority policy here is the slot-level analogue of the HTB tree, and the qualitative result
    agrees (URLLC protected, eMBB pays under a URLLC flood). The magnitudes do not transfer.
- (−) The scenarios are small: one cell, 100 UEs, 5 s per run, three slices dominated by eMBB.
  Conclusions about RadioSaber's gain apply to this slice mix only (see result 2).
- Next steps, if the project moves to a real RAN: use these results to choose the gNB's slicing
  policy (priority with guarantees, PF for eMBB, a delay-aware scheduler for URLLC), and measure the
  same KPIs there. The scenarios here are the test plan.

## References (each checked on 2026-11-08/09)

1. 3GPP TS 38.214, *NR; Physical layer procedures for data*: Table 5.2.2.1-2 (4-bit CQI table),
   Table 5.1.2.2.1-1 (nominal RBG size). Table values checked against a reproduction of Rel-19:
   https://itecspec.com/spec/3gpp-38-214-5-2-2-1-channel-quality-indicator-cqi/ and
   https://howltestuffworks.blogspot.com/2020/04/5g-nr-pdsch-resource-allocation-in.html
2. 3GPP TS 38.101-1, Table 5.3.2-1 (N_RB per channel bandwidth and SCS):
   https://itecspec.com/spec/3gpp-38-101-1-5-3-2-maximum-transmission-bandwidth-configuration/
3. Y. Chen, R. Yao, H. Hassanieh, R. Mittal, "Channel-Aware 5G RAN Slicing with Customizable
   Schedulers", USENIX NSDI 2023, pp. 1767–1782:
   https://www.usenix.org/conference/nsdi23/presentation/chen-yongzhou. Algorithms 1 and 2 read in the paper
   (https://www.usenix.org/system/files/nsdi23-chen-yongzhou.pdf). Code read, not run:
   https://github.com/elvinlife/RadioSaber
4. M. Andrews, K. Kumaran, K. Ramanan, A. Stolyar, P. Whiting, R. Vijayakumar, "Providing quality of
   service over a shared wireless link", IEEE Communications Magazine 39(2):150–154, Feb. 2001,
   doi:10.1109/35.900644: https://www.nokia.com/bell-labs/publications-and-media/publications/providing-quality-of-service-over-a-shared-wireless-link/
5. R. Kokku, R. Mahindra, H. Zhang, S. Rangarajan, "NVS: A Substrate for Virtualizing Wireless
   Resources in Cellular Networks", IEEE/ACM Transactions on Networking 20(5):1333–1346, Oct. 2012,
   doi:10.1109/TNET.2011.2179063: https://researchweb.draco.res.ibm.com/publications/nvs-a-substrate-for-virtualizing-wireless-resources-in-cellular-networks
6. J.-H. Rhee, J. M. Holtzman, D.-K. Kim, "Scheduling of real/non-real time services: adaptive EXP/PF
   algorithm", 57th IEEE VTC 2003-Spring, vol. 1, pp. 462–466, doi:10.1109/VETECS.2003.1207583:
   https://manuscript.isc.ac/Inventory/49/1570276.htm (the version RadioSaber cites). An earlier
   version is "Scheduling of Real/Non-real Time Services in an AMC/TDM System: EXP/PF Algorithm"
   (Rhee, Kim), CDMA International Conference 2002, LNCS, doi:10.1007/3-540-36555-9_52.
7. A. Jalali, R. Padovani, R. Pankaj, "Data throughput of CDMA-HDR a high efficiency-high data rate
   personal communication wireless system", IEEE VTC 2000-Spring, doi:10.1109/VETECS.2000.851593:
   https://scilit.com/publications/38ec2d54731caa8ecfefabf9497ddcef
8. 3GPP TS 38.323, *NR; Packet Data Convergence Protocol (PDCP)*, clause 5.3 "SDU discard"
   (discardTimer): https://itecspec.com/spec/3gpp-38-323-5-procedures
