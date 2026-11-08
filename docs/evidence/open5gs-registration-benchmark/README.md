# Registration benchmark — N UEs registering in one burst, against Mukute et al. (IEEE Access, 2024)

`scripts/open5gs/registration_benchmark.py`. Run 2026-11-08 (UTC), commit `61f4c5b` + the script,
on the live stack (Open5GS v2.8.0, UERANSIM v3.3.0, WSL2 on a 12-thread / 7.6 GiB laptop).

**Reference:** T. Mukute, L. Mamushiane, A. A. Lysko, E.-R. Modroiu, T. Magedanz, J. Mwangama,
"Control Plane Performance Benchmarking and Feature Analysis of Popular Open-Source 5G Core
Networks: OpenAirInterface, Open5GS, and free5GC", *IEEE Access*, vol. 12, pp. 113336–113360, 2024,
doi:10.1109/ACCESS.2024.3441725 (full text read from the CSIR ResearchSpace copy).

## Their metric, and ours

Their macro-benchmark (Section VII-A, Fig. 3): a traffic generator registers N UEs "in a burst"; they
report (a) the **total time for all N UEs** and (b) the **average time per UE**, for N up to 300 (the
most free5GC could handle), for the **registration and de-registration procedures together**.

Here, for each N, every subscriber is re-provisioned, the UE container restarts with exactly N UEs and
`START_BATCH=N` (all N `nr-ue` processes launched at once), and each UE's own log gives its
registration time: `MM-REGISTER-INITIATED` → `Initial Registration is successful`.

- **Total** = first UE's initiation → last UE's success. Timed twice: from the log timestamps, and with
  the script's monotonic clock polling the logs every ~0.1 s (they agree to within 0.3 s; the
  monotonic value reads 0.0 when a burst finished inside one poll).
- **Per-UE time** = each UE's own initiation → success; mean, median and max over the N UEs.
- **Launch spread** = first → last initiation: the time the UE container takes to start N processes.
- Peak CPU of the control-plane NFs during the burst, from their cgroups.

## Results

Set 1 (`20261108-202232Z/`): N = 1, 10, 50, 100, 200, three repetitions, order alternating up and
down. Set 2 (`20261108-202831Z/`, six minutes later, same stack and procedure; the script then also
kept every UE's times and the log of any outlier): N = 100 and 200, two more repetitions. No
registration failed in any run; every UE registered.

| Set | N | Rep | Total (s) | Total, monotonic (s) | Per UE mean / median / max (s) | Launch spread (s) | Peak CPU, % of a core: SCP / AMF / MongoDB / UDM |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 0.092 | — | 0.092 / 0.092 / 0.092 | 0 | 3.9 / 1.4 / 1.6 / 1.4 |
| 1 | 1 | 2 | 0.064 | — | 0.064 / 0.064 / 0.064 | 0 | 3.3 / 1.2 / 1.7 / 0.9 |
| 1 | 1 | 3 | 0.059 | — | 0.059 / 0.059 / 0.059 | 0 | 2.9 / 1.1 / 1.6 / 0.9 |
| 1 | 10 | 1 | 0.195 | 0.213 | 0.145 / 0.147 / 0.162 | 0.03 | 17.0 / 7.0 / 7.1 / 7.0 |
| 1 | 10 | 2 | 0.189 | — | 0.144 / 0.145 / 0.156 | 0.03 | 16.1 / 6.6 / 8.0 / 6.8 |
| 1 | 10 | 3 | 2.594 | 2.566 | 0.142 / 0.151 / 0.187 | **2.52** | 17.1 / 6.1 / 7.6 / 6.5 |
| 1 | 50 | 1 | 2.791 | 2.688 | 0.408 / 0.428 / 0.580 | 2.66 | 48.3 / 24.1 / 23.0 / 21.2 |
| 1 | 50 | 2 | 2.858 | 2.969 | 0.488 / 0.579 / 0.673 | 2.66 | 67.8 / 32.0 / 31.3 / 27.6 |
| 1 | 50 | 3 | 2.794 | 2.723 | 0.379 / 0.457 / 0.514 | 2.66 | 47.1 / 24.7 / 25.8 / 21.6 |
| 1 | 100 | 1 | **26.221** | 26.254 | 1.825 / 1.090 / **26.11** | 5.11 | 71.5 / 38.2 / 41.7 / 31.9 |
| 1 | 100 | 2 | **142.334** | 141.909 | 2.151 / 0.620 / 5.303 | **142.23** | 67.4 / 33.9 / 41.1 / 29.4 |
| 1 | 100 | 3 | 2.925 | 2.902 | 0.828 / 0.969 / 1.251 | 2.79 | 66.2 / 36.2 / 36.8 / 29.5 |
| 2 | 100 | 1 | 6.656 | 6.357 | 0.985 / 1.145 / **6.42** | 2.93 | see `results.csv` |
| 2 | 100 | 2 | 3.246 | 2.980 | 0.713 / 0.836 / 1.241 | 2.88 | see `results.csv` |
| 1 | 200 | 1 | 3.996 | 4.001 | 1.390 / 1.567 / 2.062 | 3.66 | 74.1 / 44.8 / 56.0 / 38.4 |
| 1 | 200 | 2 | **25.521** | 25.432 | 1.517 / 1.695 / **25.09** | 3.64 | 74.4 / 46.4 / 48.4 / 39.1 |
| 1 | 200 | 3 | 3.917 | 3.727 | 1.560 / 1.754 / 2.256 | 3.65 | 72.4 / 44.1 / 50.7 / 33.8 |
| 2 | 200 | 1 | 3.747 | 3.759 | 1.713 / 1.853 / 2.389 | 3.54 | see `results.csv` |
| 2 | 200 | 2 | 3.924 | 3.861 | 1.558 / 1.800 / 2.095 | 3.54 | see `results.csv` |

Median over the repetitions (5 for N = 100 and 200, 3 otherwise), with range:

| N | Total (s) | Registrations per second (N / median total) | Per-UE mean (s) |
|---|---|---|---|
| 1 | 0.064 (0.059–0.092) | — | 0.064 |
| 10 | 0.195 (0.189–2.594) | 51 | 0.144 (0.142–0.145) |
| 50 | 2.79 (2.79–2.86) | 18 | 0.41 (0.38–0.49) |
| 100 | 6.66 (2.93–142.3) | 15 (31–34 in the two runs without an outlier) | 0.99 (0.71–2.15) |
| 200 | 3.92 (3.75–25.5) | 51 | 1.56 (1.39–1.71) |

### The outliers, and what limits the total here

- **The total is bounded by how fast the UE container starts processes**, not by the core: the launch
  spread is ~14 ms per UE (2.7 s for 50, 2.8–2.9 s for 100, 3.5–3.7 s for 200), and in 10 of the 19
  runs it is ≥ 90 % of the total. The per-UE time is the core's (and the emulated radio's) share.
- **Three runs had one UE that took 6–26 s** (N = 100 set 1 rep 1, set 2 rep 1; N = 200 set 1 rep 2):
  one UE out of 100 or 200; the other UEs' times were ordinary. The one captured log
  (`20261108-202831Z/outlier-n100-rep1-208930000000048.log`) shows the delay is on the network side:
  the UE sent its registration at 20:28:34.251 and the first Authentication Request came back at
  20:28:40.603, 6.35 s later; everything after took 70 ms. Where the request waited (AMF, AUSF/UDM via
  the SCP, which peaks at ~74 % of a core in a 200-UE burst) was not traced.
- **One run had a UE that started 142 s late** (N = 100 set 1 rep 2): its process began after the
  others had long finished (launch spread 142 s) and then registered normally in < 5.3 s. That is the
  UE container restarting a process, not the core. With N = 10 one run had a 2.5 s late start.

## Against Mukute et al. (Open5GS curve of their Fig. 3, read off the plot, ±0.5 s)

| N | Theirs: total (s), registration + de-registration | Ours: total (s), registration only, median | Theirs: average per UE (s) | Ours: per-UE mean (s), median |
|---|---|---|---|---|
| 50 | ≈ 1.4 | 2.79 (launch-bound) | ≈ 1.2 | 0.41 |
| 100 | ≈ 3.4 | 6.66 (2.9–3.2 without an outlier) | ≈ 2.9 | 0.99 |
| 200 | ≈ 7.2 | 3.92 | ≈ 6.3 | 1.56 |
| 300 | ≈ 11.0 | not run | ≈ 9.8 | not run |

What can and cannot be concluded:

- **Per-UE times here are 3–4× shorter than theirs at 100 and 200 UEs**, but theirs include a
  de-registration per UE and ours do not, so their figure is roughly an upper bound for registration
  alone; a factor of ~2 would be explained by that alone if both procedures cost about the same.
- **Totals are not comparable at N ≤ 100**: ours are dominated by process launch (UERANSIM runs one
  process per UE; their generator emulates all UEs in one process over SCTP), theirs by the core.
- **Both grow with N** in the same way for the per-UE time (about linearly: 0.41 → 0.99 → 1.56 s here
  for 50 → 100 → 200; ≈ 1.2 → 2.9 → 6.3 s there).

## Every difference in setup

| | Mukute et al. 2024 | Here |
|---|---|---|
| Open5GS | v2.6.4 (their Table 2) | v2.8.0 |
| Procedures timed | registration **and** de-registration | registration only — UERANSIM's `deregister normal` makes the UE register again at once, its log does not keep the de-registration and the AMF logs no accept time, so de-registration has no clean end point (checked on one UE) |
| UE / RAN emulator | their own traffic generator (they report UERANSIM crashing at 200 UEs) | UERANSIM v3.3.0, one `nr-ue` process per UE, three `nr-gnb` (one per slice) |
| Burst | all N UEs from one generator | all N `nr-ue` processes launched at once (`START_BATCH=N`); launching takes ~14 ms per UE |
| Core deployment | OpenStack VM sized to Open5GS's minimum (1 CPU, 4 GB RAM in their Table 2), Ubuntu 20.04, kernel 5.4 | Docker containers, one per NF, on a WSL2 VM (12 threads, 7.6 GiB) shared with the RAN, UEs, UPFs and monitoring; kernel 6.6 |
| Host | i7 @ 2.5 GHz, 8 cores, 16 GB, OpenStack zed | laptop (Windows 11 + WSL2) |
| Network between UEs and core | VPN between two VMs | Docker bridge on one host |
| SBI | (not stated) | indirect, through the SCP (Open5GS default here) |
| Authentication | (not stated) | every registration includes one SQN resynchronisation (subscribers re-provisioned before each run) |
| Slices / sessions | (not stated) | three slices; each UE also sets up a PDU session after registering (not timed) |
| N | 1–300 | 1, 10, 50, 100, 200 (300 at once was not attempted: at 300 UEs this laptop's instruments already fail, `../open5gs-scale/`) |
| Repetitions | (not stated) | 3 (N = 1, 10, 50) and 5 (N = 100, 200), all reported |
| Timing | generator-side | UE log timestamps (wall clock, ms) and a monotonic poll of the logs (~0.1 s resolution) |
