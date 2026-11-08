# ADR-023 — Standards alignment and statistics: NESTs, a 3GPP/IETF mapping, and tests before claims

**Status:** Accepted, 2026-11-09
**Builds on:** ADR-015 (traffic profiles and KPI scorecard), ADR-019 (packet scheduler),
ADR-020 (multi-session UEs)

## Context

Two problems surfaced on 2026-11-08.

**A claim without statistics.** A single KPI run from October was compared with a single run from
November and read as "the scheduler lifted URLLC from 0.90 to 1.00". The same-day comparison that
followed showed the scheduler had nothing to do with it (`off` also scored 1.00). With 2 runs per
arm, run-to-run variation on this laptop (URLLC p99 6.1 and 9.8 ms with identical settings) is as
large as most differences we look for, and nothing in the toolchain said so.

**Words that sound standard but are not checked.** The scorecard cites TS 23.501's 5QI table and
names KPIs "latency", "loss", "jitter", "reliability". The slices are described in `slices.env`
in project terms. None of it was tied to the documents that define those words: GSMA NG.116 for a
slice's description, TS 28.554/28.552 for KPIs and measurements, TS 28.541 for the management
model, TS 22.261 and TS 23.501 for URLLC's numbers, and the IETF IPPM RFCs for delay, loss and
delay variation. Where our definitions differ — and they do, in span, statistic and treatment of
late or lost packets — nobody had said how.

## Decisions

### 1. Each slice has a NEST in NG.116's own vocabulary, checked against the deployment

`deployments/open5gs/slice-templates/{embb,urllc,mmtc}.nest.yaml` fill NG.116 v10.0's GST with
this deployment's values. Attribute and parameter names, clauses, units and allowed values come
from `tools/standards/ng116-v10-gst.json`, extracted from the document (clauses 3.4.1–3.4.42: 39
with parameter tables, 3.4.9 Isolation level FFS, 3.4.10 and 3.4.22 void; every parameter is
Optional or Conditional in v10.0). Every value names its source; values with
`from:` are re-read from `slices.env`, `kpi-scorecard.json`, `kpi-gates.json` or TS 23.501's 5QI
table and must match after unit conversion, so a template cannot drift from the deployment.

Only what the project actually sets is in a NEST. GSMA's example NESTs (NG.116 clause 4) list
Availability 99.999 for URLLC and UE density 100 000 for MIoT; neither is a requirement of this
project or measurable on a laptop, so neither is copied. The eMBB targets (p5 ≥ 50 Mbps, aggregate
≥ 500 Mbps) are experienced rates, which NG.116 has no attribute for; they stay in the scorecard.

`tools/standards/check_nest.py` validates the templates (CI) and, with `--run`, compares a KPI run
with them: PDB and PER by the rules of TS 23.501 5.7.3.4/5.7.3.5 (a delay-critical GBR packet later
than the PDB counts as lost; non-GBR: 98 % within the PDB), ceilings as WITHIN/EXCEEDS, and
three-valued verdicts — MET, MISSED, **UNRESOLVED** when the run's sample cannot decide.

### 2. One mapping document, gaps stated

`docs/standards-mapping.md` maps each scorecard KPI and Prometheus metric to TS 28.554/TS 28.552,
each NEST parameter to a TS 28.541 ServiceProfile attribute (Annex L where it maps explicitly, by
name otherwise), URLLC's numbers to TS 22.261 and TS 23.501, and latency/loss/jitter to RFC
7679/7680/3393 — and says, for each, how ours differs. The documents and versions read are listed;
anything not read (TS 22.104, ITU-R M.2410, TR 38.913, RFC 3550) is marked unverified rather than
cited.

### 3. Comparisons go through tests, and the tests say when they cannot decide

`tools/kpi/stats.py` (standard library only): median, percentile bootstrap and two-stage cluster
bootstrap intervals, the Mann–Whitney U test (exact distribution without ties, exact permutation
with ties at small n, normal approximation with tie and continuity correction otherwise),
Cliff's delta, Clopper–Pearson intervals, the rule of three, and the zero-failure sample size.
`tools/kpi/compare_runs.py` takes run directories grouped by arm and prints per-run medians and
intervals, pairwise tests **with the smallest p-value the design can reach**, per-device results
with clustering made explicit, URLLC reliability bounds (loss and late packets), and a check that
every run measured the same window.

The run is the experimental unit. Per-device samples are reported, but as clustered data: their
pooled p-values are labelled as ignoring the clustering. The normal approximation is not used at
tiny n: at 2 runs against 2 it returns p = 0.22 where no exact outcome is below 0.33.

## Verification

- `tools/kpi/test_stats.py` (28 tests) checks the Mann–Whitney test against the reference example
  of SciPy's documentation (R. Shier 2004: U = 17, exact p = 0.1111…, normal p = 0.11134688653314041
  and 0.0864107329737), Cliff's delta (0.7 on the same data), Clopper–Pearson against its closed
  forms at k = 0 and k = n and its defining tail property, the rule of three against its worked
  examples (1/500 for 1 500 trials, 1/100 for 300), and the bootstraps' basic properties.
- `tools/standards/test_check_nest.py` (26 tests): the restricted YAML parser agrees with PyYAML on
  the templates and rejects what it does not understand; the repository templates validate; and a
  copy broken in one way at a time — unknown attribute or parameter, wrong unit or clause, a value
  drifted from `slices.env` or the scorecard, a missing source, a duplicate, a disallowed value, a
  wrong S-NSSAI, an operator-specific 5QI without its characteristics, deterministic communication
  without a periodicity — is rejected with that reason. The comparison rules are checked on
  constructed measurements.
- Both run in CI (`integrity.yml`, job `kpi-gate`), with `check_nest.py` validating the templates.
- Applied to the nine runs of 2026-11-08: [statistics.md](../evidence/open5gs-kpi/statistics.md).

## Results

- **No per-run comparison among the nine runs can reach p < 0.05**, and none does: the smallest
  possible p is 0.333 at 2 v 2, and 0.056 even for all seven scheduled runs against `off`.
- Of the README's five readings, the eMBB cost of the 250 Mbps cell is best supported (by
  mechanism, and every device below every device); "sliced beats FIFO at 600" rests on **two**
  radio-link-failure events (all lost packets of each FIFO run came from one device), not on 4 386
  independent losses; "the scheduler lengthened URLLC's tail" is consistent in direction but not
  supported (p = 0.333; adding the morning run puts a sliced run inside the `off` range).
- The FIFO runs measured URLLC over ~210 s instead of 150 s (the runner waited for stuck
  downloads), and the 600 Mbps arms were not interleaved: both are confounds, now flagged.
- URLLC reliability: zero loss in 537 481 packets over seven runs bounds loss at 5.6·10⁻⁶ (one-sided
  95 %), but every run had packets over 10 ms, and the four sliced runs of the controlled block had
  more than 1 % — so in TS 22.261's sense (delivered within the time constraint) **no run
  demonstrates 99.999 %**, and those four fall short of 99 %. 99.999 % needs ≥ 299 572 consecutive
  packets with none lost or late (10 minutes of the URLLC profile).

## Consequences

- (+) A difference is reported with the test that supports it, or with the reason no test can.
  The smallest reachable p-value is printed, so an underpowered design is visible before anyone
  reads the medians.
- (+) "Latency", "loss", "jitter" and "reliability" now say which standard definition they are
  closest to and how they differ (e.g. our jitter is RFC 3393 mean absolute ipdv over consecutive
  received packets, not RTP interarrival jitter; our latency percentiles exclude lost packets, which
  RFC 7679 counts as infinite).
- (+) The NESTs fail CI if the deployment changes underneath them.
- (−) Two runs per arm remains what exists; the analysis can only say how little it shows. The next
  comparison needs at least 4 interleaved runs per arm.
- (−) Late-packet reliability is bounded from percentiles because the collector keeps no per-packet
  delays or over-PDB counts. Adding a count of packets over the PDB to `kpi_collector.py` is the
  smallest change that would turn the bound into a measurement; it is not made here (the live stack
  is out of scope for this change).
- (−) The GST list is a transcription of one NG.116 version. A new version needs a new extraction;
  the templates name the version they were checked against.
