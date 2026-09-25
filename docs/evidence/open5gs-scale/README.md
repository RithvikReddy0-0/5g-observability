# Scale — what 100 UEs cost, and where this laptop stops (Open5GS, Phase 2b)

`scripts/open5gs/scale_test.sh` (`make o5gs-scale`). CPU and memory are read from each
container's cgroup (`scripts/open5gs/cgroup_stats.py`), not from `docker stats`. The CSV opens in
Excel. Host: WSL2, 12 threads, 7.6 GiB, with the Open5GS stack only (free5GC stopped).

| File | Run |
|---|---|
| `scale-20260925-051504Z.txt` / `.csv` | **the result** — after all three startup races were fixed |
| `run1-before-proctable-fix-20260925-050013Z.*` | first run, stopped after 100 UEs in part 2. It found the proc-table race: 98/100, 96/100 and 92/100 attached. Kept as the record of that bug; numbers superseded. |

## Part 1 — attach burst: the same 100 UEs, `START_BATCH` at a time

| UEs at once | All attached in | Registration p50 / p95 | PDU session p50 / p95 | Reg. failures | Peak CPU (% of one core) |
|---|---|---|---|---|---|
| 1 | 111 s | 39 / 75 ms | 223 / 231 ms | 0 | every NF < 6 % |
| 10 (default) | 35 s | 141 / 194 ms | 48 / 75 ms | 0 | SCP 19 · gNB-mMTC 15 · AMF 8 · UDM 8 |
| 25 | 16 s | 300 / 404 ms | 95 / 146 ms | 0 | SCP 37 · gNB-mMTC 22 · AMF 18 · MongoDB 17 · UDM 16 |
| 100 | **7 s** | **2409 / 2526 ms** | 221 / 524 ms | 0 | **SCP 83** · MongoDB 56 · AMF 52 · UDM 40 · UDR 34 |

- **No registration failed at any burst size.** The core queues rather than drops.
- **Batching trades per-UE latency for total time.** All 100 at once finishes 16× sooner than
  one at a time. Each UE then waits ~2.4 s instead of ~40 ms, because it queues behind 99 others.
- **The first bottleneck is the SCP, not the AMF.** Every SBI request goes through it (indirect
  communication), so a registration storm peaks there first, at 83 % of a core. MongoDB (56 %) is
  next. It serves UDR lookups for every authentication.
- The UE container's own CPU during bursts is not in the CSV. Its cgroup changes when the
  container is recreated for each run.

## Part 2 — scale steps: eMBB 20 + URLLC 10 + more mMTC devices, at rest

| UEs | Attached | UE container CPU · memory | Host memory available | Probe round | Reachable | Registration p95 |
|---|---|---|---|---|---|---|
| 100 | 100/100 in 15 s | 56 % · 193 MiB | 4243 MiB | 0.14 s | 100 | 418 ms |
| 150 | 150/150 in 25 s | 89 % · 291 MiB | 4135 MiB | 0.24 s | 150 | 373 ms |
| 200 | 200/200 in 34 s | 141 % · 415 MiB | 3998 MiB | 0.34 s | 200 | 404 ms |
| 300 | 300/300 in 56 s | 188 % · 785 MiB | 3771 MiB | **1.66 s** | **55** (one sample) | **3588 ms** |

- **Holding UEs costs almost nothing in the core.** The AMF and SMF sit at ~0 % at rest for
  100–300 UEs. Memory grows ~1 MiB per UE in the UE container.
- **What costs CPU is simulating and watching the UEs.** Each UE is its own `nr-ue` process, and
  every one is pinged every 5 s by the liveness probe and every 10 s by the supervisor. The UE
  container grows about linearly: roughly 0.6 % of a core per UE.

## Where this laptop stops: ~300 UEs, and it is the observer that breaks first

At 300 UEs one probe sample showed only 55 reachable. Dead UEs, or a failing instrument? The
state was held and examined (same host, same image):

```
startup: 300 300 51 25                       all 300 attached in 51 s, 25 at a time
t+30s  radio link failures 0  supervisor restarts 0  interfaces 300  probe reachable 300  round 0.854 s
t+60s  radio link failures 0  supervisor restarts 0  interfaces 300  probe reachable 300  round 1.229 s
t+90s  radio link failures 0  supervisor restarts 0  interfaces 300  probe reachable 300  round 0.862 s
40 UEs pinged ONE AT A TIME with a 2 s timeout: 40 of 40 answered
o5gs-ue        cpu avg 230.6 %  peak 655.9 %  mem peak 651 MiB
o5gs-gnb-mmtc  cpu avg   8.1 %   ·   o5gs-upf-mmtc 1.3 %   ·   o5gs-amf 0.0 %
VM load average: 18.53, 13.90, 6.80   (12 threads)
```

- **The UEs were alive.** All 300 kept their sessions: no radio link failure, no supervisor
  restart, and every UE pinged on its own answered.
- **The laptop was saturated.** A load average of 18.5 on 12 threads.
- **The liveness probe was the first thing to fail.** It pings every UE at once with a 1 s
  timeout, and a round now took 0.9–1.7 s, so under that load it reports live UEs as
  unreachable. Registration latency degraded too: p95 3.6 s, max 8.4 s, against ~0.4 s at 200.

**Up to 200 UEs everything is measured cleanly on this laptop. At 300 the UEs still work, but the
instruments that judge them are no longer trustworthy.** Scaling further — sir's "later increase
the UEs" — needs one of three things: a probe that samples UEs instead of pinging all of them
every 5 s, UEs spread over several containers or hosts, or the ODE.

## Measurement notes

- Registration latency comes from the UEs' own log timestamps (wall clock). This VM's clock was
  stepped by up to +1.1 s every 32 s during the session (`../open5gs-clock/`), so a single UE's
  value can be inflated by a step. The p50 and p95 figures are robust to that; the max is not.
- **Every registration here includes one authentication resynchronisation.** Provisioning resets
  the network's SQN and each UE process starts fresh. That adds one AUSF/UDM round-trip compared
  with a real device.
- Subscribers beyond 100 were provisioned only for the run and deleted afterwards. The stack was
  restored to the standard 100 UEs.
