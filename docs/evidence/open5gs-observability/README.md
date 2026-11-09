# Observability deployment on the live stack (ADR-024)

## 20261109-000016Z — first deployment

On the running Open5GS stack: 100 UEs, 130 PDU sessions, scheduler `sliced` 250/100 Mbps. It was
deployed between experiments, following [docs/observability.md](../../observability.md). The
files:

| File | What |
|---|---|
| `deploy.log` | every step and its output, in four parts (below) |
| `mem-before.txt` | `docker stats` of every container before anything was started |
| `verify.txt` | memory (3 samples), Loki ingest, traces, diagnosis service, a one-off diagnosis |
| `gate.txt`, `acceptance.txt` | KPI gate and acceptance with all of it running |

**Result:**
- **KPI gate:** 27 passed, 0 failed, 1 advisory. `fault-diagnosis-reachable` now passes; the advisory is the known URLLC tail.
- **Acceptance:** PASS=24 FAIL=0 SKIP-ODE=1 GAP=1.

**Measured memory:**

| Piece | Measured | Limit |
|---|---|---|
| Loki | 98–118 MiB | 384 MiB |
| Alloy | 72–73 MiB | 192 MiB |
| `diagnose.py --serve` | 25–27 MiB RSS | — |

That is about 220 MiB in all, against the ~600 MiB worst case the runbook allowed for. Host memory available went from 4.76 to 4.49 GiB.

**What works:**
- **Loki labels:**
  - `slice`: eMBB, URLLC, mMTC.
  - `component`: 16 values.
  - `event`: 19 kinds, among them `pfcp_deassociated`, `ip_pool_exhausted`, `ue_restart` and `scheduler_apply`.
  - `level`: 4 values.
  - There is no IMSI label.
- **Traces:**
  - `admission.decide` from the slice orchestrator.
  - `scheduler.apply` from the packet scheduler.
  - The two are read together from two files (see below).
- **Diagnosis service:** `fault_diagnosis_up 1`; Prometheus scrapes it (`up{job="fault-diagnosis"}` = 1); it ran 9 times with "no fault found". Its timeline shows the scheduler re-apply as a trace.

### Three faults found by deploying, none caught by tests or CI before

1. **The scheduler's spans were all dropped.**
   - **Cause:** it runs as root in a container and appended to `/tmp/o5gs_traces.jsonl`, which the orchestrators had created as the WSL user. With `fs.protected_regular = 2` (Ubuntu's default), the kernel refuses even root an `O_CREAT` open of another user's file in sticky `/tmp`. The tracer correctly never failed the scheduler, so the only symptom was `dropped 1`.
   - **Fix:** the scheduler writes `/tmp/o5gs_traces-scheduler.jsonl`, and `trace.read_spans` merges every `<file>-*.jsonl` sibling. A test covers it, red on the old reader.
2. **Alloy refused the whole pipeline, twice.**
   - **First failure:** the generated `stage.match` selectors put `re.escape` output (`network\-orchestrator`) in a LogQL double-quoted string, which LogQL reads as a Go string, so Alloy reported "invalid char escape".
   - **Second failure:** backtick strings, the obvious alternative, are a syntax error in Alloy v1.20.1's match stage, for label matchers and line filters alike ("unexpected IDENTIFIER, expecting STRING").
   - **Fix:** every matcher and line filter is a Go double-quoted string, escaped once more.
   - **How it was checked:** all 55 selectors were loaded one by one into the pinned Alloy image first.
   - **Why nothing caught it:** `alloy fmt` and the generator check both passed the broken file, because neither parses the LogQL inside the stages.
   - **New checks:**
     - `scripts/open5gs/check_alloy_config.sh` builds the real config in the pinned image. It fails on the config as merged and on the backtick version, and passes the fixed one. CI runs it.
     - A unit test checks the quoting rules offline.
3. **The first start spammed errors.** Alloy read weeks of container history, and Loki answered each batch with a 400, "timestamp too old" (104 error lines in 2 minutes). The pipeline now drops lines older than the 72 h retention before sending; after that, one error from a batch already in flight, then none.

**Not a fault, but a difference from the runbook:** `docker compose` was unavailable. Its plugin is a symlink into Docker Desktop's WSL mount, which was not present. So Loki and Alloy were started with the equivalent `docker run` commands, now in the runbook.
