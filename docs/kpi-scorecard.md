# Phase 2b KPI scorecard — what each slice is judged on

Track 3 of the [Phase 2b work division](briefs/phase2b-work-division.md). The definitions live in
one file, [`deployments/open5gs/kpi-scorecard.json`](../deployments/open5gs/kpi-scorecard.json):
- `tools/kpi/run_kpis.py` measures what it names;
- `tools/kpi/scorecard.py` scores it;
- the Excel report is written from both.

Change a target or a weight there and nowhere else. CI rejects the file if it is malformed.

Targets come from the team's KPI notes ([photos](briefs/phase2b-assets/),
[transcription and review](briefs/phase2b-kpi-targets-review.md)), used **as written**. KPIs the
notes do not list are marked *added*, with their source.

## Assumptions (from the notes)

- Industry-standard values are used only as benchmarks.
- We use Open5GS and UERANSIM, not an industry-grade 5G deployment.
- If the actual 5G architecture in the 5G research lab becomes available, industry-standard values
  can be used.

## The KPIs

| Slice | KPI | How it is measured | Target | Industry benchmark | Weight | Origin |
|---|---|---|---|---|---|---|
| URLLC | latency, mean | one-way, device → URLLC data network; 64 B every 20 ms from all 10 devices | ≤ 10 ms (project) · ≤ 2 ms (research) | 1 ms (ITU-R M.2410, radio user-plane) | 0.50 | notes |
| URLLC | latency, p95 | same samples | ≤ 10 ms | 10 ms (5QI 82 PDB, TS 23.501) | 0.15 | notes |
| URLLC | latency, p99 | same samples | ≤ 10 ms | 10 ms (5QI 82 PDB) | 0.15 | notes |
| URLLC | packet loss | sequence numbers per device | ≤ 0.01 % | 10⁻⁴ (5QI 82 PER) | 0.20 | added |
| URLLC | jitter | mean change in latency between consecutive packets | — | — | 0 (reported) | added |
| eMBB | 5th-percentile throughput, DL | every loaded device downloads over TCP for the window | ≥ 50 Mbps (project) | 100 Mbps DL (ITU-R M.2410) | 0.60 | notes |
| eMBB | aggregate throughput, DL | sum over loaded devices | ≥ 500 Mbps (research) | 550 Mbps (papers) | 0.40 | notes |
| eMBB | mean throughput, DL | | — | — | 0 (reported) | added |
| mMTC | registration success rate | IoT UEs' own logs at their latest attach | ≥ 99 % (project) | no universal target (TR 38.913) | 0.40 | notes |
| mMTC | report loss | 10-byte report every 10 s from all 70 devices; sequence numbers | ≤ 0.1 % (research) | 0.1 % (papers) | 0.40 | notes |
| mMTC | report latency, p99 | one-way, device → mMTC data network | ≤ 10 s | 10 s (TR 38.913, small packets; verify the clause) | 0.20 | added |
| mMTC | devices reporting | devices heard from in the window | — | — | 0 (reported) | added |

## Scoring

The target is the project target if there is one, otherwise the research target.
- **Lower is better:** score 1 if met, otherwise target ÷ measured.
- **Higher is better:** score = min(1, measured ÷ target).
- **Slice score:** the weighted sum of its KPI scores. Each slice's weights sum to 1.
- **Overall score:** the mean of the three slice scores.
- **"vs benchmark":** the measurement as a fraction of the industry value, reported beside every
  score. The assumptions treat industry values as benchmarks, not pass/fail lines.

A score answers "how close to our own target", not "how good is this network". A 0.37 for eMBB
on this laptop is a measured gap. Its cause is stated in the report: the laptop delivers ~300–440
Mbps in total, and 50 Mbps × 20 UEs would need 1 Gbps.

## Provisional — to confirm with the guide

| Choice | Value now | Why it is provisional |
|---|---|---|
| URLLC traffic | 64 B every 20 ms per device | the notes fix the KPI, not the traffic |
| mMTC reporting interval | 10 s | the minutes say "periodically" |
| Weights | primary KPI dominant per slice | the notes set no weights |
| Overall score | plain mean of slices | sir may want slices weighted |
| Sweep parameters | eMBB load, IoT interval, IoT device count | the minutes say "varying one parameter" without naming it |
| eMBB targets | as written (50 Mbps p5 and 500 Mbps aggregate) | they contradict each other; see the review |

## What it does not measure

Anything that needs a real radio: spectral efficiency, radio-only latency, coverage, mobility,
connection density per km², battery life. UERANSIM simulates the RAN over UDP. See the
[KPI targets review](briefs/phase2b-kpi-targets-review.md) for the full list.
