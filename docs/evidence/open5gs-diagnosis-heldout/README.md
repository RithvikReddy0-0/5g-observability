# Fault diagnosis on faults it was not written for (ADR-024)

`diagnose.py --validate` scores 49 recorded incidents at precision 1.00, recall 1.00 and top-1 0.93.
Its rules were written knowing those incidents, so that is a consistency check, not an accuracy
measurement. This folder tests it on faults injected live:
- **Script:** `tools/observability/inject_faults.py`.
- **Pre-registration:** the expected and acceptable causes of every case were fixed in the script before the first run, and committed in that form.
- **Method:** each fault is held for 180 s, then `diagnose.py --last 180s` runs (its baseline is the window before). Then the fault is undone, UEs are re-attached if needed, and the stack settles for 240 s.

## Held-out run `20261109-075900Z` (commit `4ce595c`)

| Case | Fault | Expected | Reported, in rank order | Top-1 | False positives |
|---|---|---|---|---|---|
| healthy-before | none | nothing | nothing | — | 0 |
| upf-urllc-stopped | `docker stop o5gs-upf-urllc` | PFCP association loss | target down 0.95 · **PFCP association loss 0.85** · RLF / no data path 0.95 (explained by PFCP loss) | no (2nd) | 0 |
| gnb-mmtc-stopped | `docker stop o5gs-gnb-mmtc` | gNB without N2 | RLF / no data path 0.95 · **gNB N2 down 0.75** · registration failure 0.60 | no (2nd) | 0 |
| pcf-stopped | `docker stop o5gs-pcf` | target down | **target down 0.95** | yes | 0 |
| upf-urllc-cpu-hog | two busy loops in the URLLC UPF | CPU saturation | nothing | no (missed) | 0 |
| n6-delay-urllc | +15 ms netem on UPF → URLLC data network (**no rule covers this**) | nothing | nothing (the correlation report shows the anomaly: URLLC p95 3.8 → 18.7 ms, z 14.9) | — | 0 |
| healthy-after | none | nothing | nothing | — | 0 |

**Score**
- **Expected cause found:** 3 of 4 faulty cases.
- **Top-1 right:** 1 of 4.
- **False positives:** 0 in all 7 cases, including the fault no rule describes and both healthy windows.

This is well below the in-sample figures, and it is the number to quote for unseen faults. It
comes from 4 faulty cases on one day, so it is an indication, not an accuracy estimate.

**Reading the misses**
- **upf-urllc-stopped:** "target down" outranked the PFCP loss.
  - Both are true consequences of the same stopped container, and the evidence names the right UPF (`10.53.0.8`).
  - The PFCP finding lists all three slices, because the SMF's "no response" lines appeared under all of them; the heartbeat and de-association lines say URLLC only. Attribution could be sharper.
- **gnb-mmtc-stopped:** the consequence (mMTC UEs without a data path) outranked the cause, because no causal link said a gNB without N2 explains it. The gNB finding also did not name its slice.
- **upf-urllc-cpu-hog:** the loops did take ~208 % CPU (checked afterwards with `docker stats`). But the host has 12 cores and the UPF had no quota, so its forwarding thread was never short of CPU, and URLLC p95 *fell* (3.4 → 2.2 ms). The CPU rule names saturation only alongside a latency breach, so "healthy" matched the service. The pre-registered expectation was wrong about the fault, not the diagnosis about the stack.
  - **A real gap found on the way:** the live path never collected per-container CPU or quotas, so the rule's container branch only worked on recorded incidents.

## Changes made because of it, and a post-hoc check (NOT held-out)

1. **Causal link:** a gNB without N2 now explains RLF / no data path, so the cause ranks first.
2. **Live CPU data:** the live diagnosis collects per-container CPU (`docker stats`) and CPU quotas (`docker inspect`).

Both have unit tests, which fail on the previous code. `diagnose.py --validate` is unchanged (49 cases, precision 1.00, recall 1.00, top-1 0.93).

The post-hoc run (folder `*-posthoc`) re-runs the gNB case and injects a CPU fault that does starve
the UPF: a 0.5-CPU quota plus the two loops. It was written after seeing the results above, so it
checks the fixes and is not part of the held-out score.

Post-hoc run `20261109-085306Z-posthoc` (fixes in the working tree on top of `ff654cf`):

| Case | Reported, in rank order | Right first? | URLLC p95 |
|---|---|---|---|
| gnb-mmtc-stopped (re-run) | **gNB N2 down 0.75** · RLF / no data path 0.95 (explained by it) · registration failure 0.60 | yes | 3.0 → 2.5 ms |
| upf-urllc-cpu-starved | **CPU saturation 0.85, URLLC**: "upf-urllc at 51% CPU against a quota of 0.5 CPUs", probe RTT 83.9 ms and one-way p95 59.3 ms over the 10 ms budget | yes | 3.5 → **56.5 ms** |

Both are right, with no false positives. The CPU finding cites the live per-container data, which
did not exist before the change.

After this run the URLLC UPF's quota reads 12 CPUs, not "none": `docker update --cpus 0` is a
silent no-op, and a quota of every host core is the same thing.

**Next held-out test:** new faults, not these:
- a UPF restart under load;
- the SMF stopped;
- packet loss on N3;
- a full disk in Loki.
