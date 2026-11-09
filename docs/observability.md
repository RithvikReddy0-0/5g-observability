# Observability runbook — per-slice logs, decision traces, correlation, fault diagnosis (ADR-024)

Everything here is **optional**: the network, the KPI gate and every experiment work the same with
none of it running. Design and results: [ADR-024](adr/ADR-024-observability-logs-correlation-diagnosis.md).

| Piece | What | Where it runs | Memory |
|---|---|---|---|
| Decision traces | JSON lines from the slice and network orchestrators, the packet scheduler and the KPI runner | inside those processes → `/tmp/o5gs_traces.jsonl` (the scheduler: `/tmp/o5gs_traces-scheduler.jsonl`, read together) | none extra |
| Fault diagnosis | `tools/observability/diagnose.py --serve` on :9115 | host process, like the orchestrators | 25–27 MiB RSS (measured 2026-11-09) |
| Loki | log store, 72 h retention | container `o5gs-loki`, 10.53.0.43, `127.0.0.1:3100` | hard limit **384 MiB** (Go soft limit 300 MiB), 0.5 CPU; measured 98–118 MiB |
| Grafana Alloy | ships and labels every `o5gs-*` container's log | container `o5gs-alloy`, 10.53.0.44 | hard limit **192 MiB** (soft 150 MiB), 0.5 CPU; measured 72–73 MiB |

Worst case for the whole set: **~600 MiB** (the two hard limits plus the service). Measured on the
stack (2026-11-09, 100 UEs, 130 sessions, idle between experiments): **~220 MiB** in all, Loki about
100–118 MiB, Alloy 72 MiB, the service 27 MiB; host memory available went from 4.76 to 4.49 GiB.
Evidence: [docs/evidence/open5gs-observability/20261109-000016Z](evidence/open5gs-observability/20261109-000016Z/).

All commands from the repository root in the WSL distro, with the stack up (`make o5gs-up`).
Run them **between experiments**: steps 1, 2 and 6 restart processes.

## 0. Offline checks (no stack, a few seconds)

```bash
python3 tools/observability/test_observability.py          # 48 tests
python3 tools/observability/diagnose.py --validate         # recorded incidents: precision / recall / top-1
python3 tools/observability/logparse.py --alloy | diff - deployments/open5gs/observability/alloy/config.alloy && echo "pipeline current"
NO_COLOR=1 python3 tools/kpi-gate/kpi_gate.py --self-test --defs deployments/open5gs/kpi-gates.json
```

## 1. Decision traces from the orchestrators

The start scripts now set `OBS_TRACE_FILE=/tmp/o5gs_traces.jsonl` (`OBS_TRACE_FILE=` turns it off).
A running orchestrator keeps its old code until restarted:

```bash
bash scripts/open5gs/start_orchestrator.sh --stop && bash scripts/open5gs/start_orchestrator.sh
bash scripts/open5gs/start_netorch.sh --stop && bash scripts/open5gs/start_netorch.sh
```

Verify (one demand, a 1 Mbps flow for 20 s):

```bash
curl -s -XPOST localhost:9111/request -d '{"supi":"imsi-208930000000001","traffic_class":"blog"}'
tail -n 2 /tmp/o5gs_traces.jsonl | python3 -m json.tool       # "name": "admission.decide", "admitted": true
python3 tools/network-orchestrator/netorch.py status          # nothing traced; an activate/deactivate is
```

## 2. Fault-diagnosis service, and Prometheus scraping it

```bash
docker restart o5gs-prometheus                 # loads the new fault-diagnosis job (prometheus.yml)
bash scripts/open5gs/start_diagnosis.sh        # :9115; first result within a minute, then every 120 s
```

Verify:

```bash
curl -s localhost:9115/metrics | grep -E '^fault_diagnosis_(up|runs_total|causes|healthy)'
curl -s localhost:9115/diagnosis | python3 -m json.tool | head -60
curl -s 'localhost:9091/api/v1/query?query=up{job="fault-diagnosis"}'   # value "1" after ~30 s
```

Each run makes 27 Prometheus range queries over 20 minutes at a 15 s step, reads Loki if it answers
(else `docker logs --since` of 11 containers plus one `grep` in the UE container), and the trace file.
`DIAG_INTERVAL`, `DIAG_WINDOW` and `DIAG_DOCKER=0` (no docker reads) tune it.

One-off, without the service:

```bash
python3 tools/observability/diagnose.py --last 15m                 # correlation report + ranked causes
python3 tools/observability/correlate.py --last 15m --trigger urllc_owd_p95
make o5gs-gate; python3 tools/kpi-gate/kpi_gate.py --defs deployments/open5gs/kpi-gates.json --json /tmp/v.json
python3 tools/observability/correlate.py --gate /tmp/v.json --last 10m
```

## 3. Logs pipeline: pull and check before starting

```bash
docker pull grafana/loki@sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193    # 3.7.8
docker pull grafana/alloy@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f   # v1.20.1
docker run --rm -v "$PWD/deployments/open5gs/observability/alloy:/cfg:ro" \
  grafana/alloy@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f fmt /cfg/config.alloy >/dev/null \
  && echo "Alloy config parses"
```

The digests are the multi-arch index digests Docker Hub's API reported for those tags on
2026-11-08; a pull that resolves to something else fails, which is the point. Use Compose v2
(`docker compose`), never `/usr/bin/docker-compose` v1.29 (it crashed on this engine, ADR-014 evidence).

Then check that Alloy really builds the pipeline (`fmt` does not parse the LogQL inside the match
stages; CI runs this too):

```bash
bash scripts/open5gs/check_alloy_config.sh       # "Alloy builds every component of config.alloy"
```

## 4. Start the logs pipeline

```bash
docker compose -f deployments/open5gs/docker-compose.logs.yaml -p o5gs-logs up -d
```

If `docker compose` is "unknown" (its plugin is a link into Docker Desktop's WSL mount and vanishes
when Docker Desktop is not running; see the Makefile), the same two containers without Compose:

```bash
O=$PWD/deployments/open5gs/observability
docker run -d --name o5gs-loki --restart unless-stopped -e GOMEMLIMIT=300MiB --memory 384m --cpus 0.5 \
  -v "$O/loki/loki.yaml:/etc/loki/loki.yaml:ro" -v o5gs-logs_o5gs-loki:/loki \
  -p 127.0.0.1:3100:3100 --network o5gs_net --ip 10.53.0.43 \
  grafana/loki@sha256:1107dd5274e0ada47e42472b7a7e71f3b2a2fe878878108f3e2f9e51528f0193 -config.file=/etc/loki/loki.yaml
docker run -d --name o5gs-alloy --restart unless-stopped -e GOMEMLIMIT=150MiB --memory 192m --cpus 0.5 \
  -v "$O/alloy/config.alloy:/etc/alloy/config.alloy:ro" -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v o5gs-logs_o5gs-alloy:/var/lib/alloy/data --network o5gs_net --ip 10.53.0.44 \
  grafana/alloy@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f \
  run /etc/alloy/config.alloy --storage.path=/var/lib/alloy/data --server.http.listen-addr=0.0.0.0:12345
```

On its first start Alloy reads each container's whole log history; the pipeline drops anything older
than Loki's 72 h retention before sending (without that, Loki answered every batch with a 400 for
minutes). Alloy keeps its read positions in its volume, so a restart does not re-read.

Verify:

```bash
curl -s localhost:3100/ready                                                 # "ready" after ~15 s
docker logs --tail 20 o5gs-alloy                                             # no "error" lines
curl -sG localhost:3100/loki/api/v1/label/slice/values                        # eMBB, URLLC, mMTC
curl -sG localhost:3100/loki/api/v1/label/event/values                        # pdu_session_..., pfcp_..., ...
curl -sG localhost:3100/loki/api/v1/query \
  --data-urlencode 'query=sum by (slice, event) (count_over_time({job="o5gs", event=~".+"}[10m]))'
docker stats --no-stream o5gs-loki o5gs-alloy                                 # record MEM USAGE in the evidence
```

## 5. Grafana

The dashboard file reloads by itself; the Loki datasource needs a restart:

```bash
docker restart o5gs-grafana
```

Open http://localhost:3001 → "Open5GS — slices, sessions and traffic" → last row: log events and
fault events per slice, one logs panel per slice, and "Diagnosed causes". Without Loki, only those
log panels show a datasource error.

## 6. Packet scheduler and KPI runner traces

`scheduler.py` (`scheduler.apply` spans) and `run_kpis.py` (`kpi.instance`, `kpi.embb_load`,
`kpi.attach`) emit spans too. They were first kept as patches because both files were being changed
on the live stack; the patches were applied to main once the scheduler experiments finished.

`start_scheduler.sh` mounts the host's `/tmp` into the scheduler container, and the scheduler writes
its spans to **its own file**, `/tmp/o5gs_traces-scheduler.jsonl`; the readers merge every
`/tmp/o5gs_traces-*.jsonl` with the main file. One shared file does not work: the scheduler runs as
root, the orchestrators as the WSL user, and with `fs.protected_regular=2` (Ubuntu's default) the
kernel refuses even root an append-open of another user's file in sticky `/tmp`. The first
deployment hit exactly that: the scheduler applied its cell but every span was dropped.

Restarting the scheduler with `--stop` **removes the queues** (mode off) until it starts again —
only between experiments:

```bash
bash scripts/open5gs/start_scheduler.sh --stop && bash scripts/open5gs/start_scheduler.sh
grep -c '"scheduler.apply"' /tmp/o5gs_traces-scheduler.jsonl                 # >= 1
```

The KPI runner needs no restart; run it with `OBS_TRACE_FILE=/tmp/o5gs_traces.jsonl`.

## 7. The KPI gate

```bash
make o5gs-gate
```

Expected: 0 failed. `fault-diagnosis-reachable` is an advisory (WARN while the service is stopped),
and `no-scrape-failures` ignores the `fault-diagnosis` job like the orchestrators and the scheduler.

## 8. Stop / roll back

```bash
bash scripts/open5gs/start_diagnosis.sh --stop
docker compose -f deployments/open5gs/docker-compose.logs.yaml -p o5gs-logs down         # keeps the Loki volume
docker compose -f deployments/open5gs/docker-compose.logs.yaml -p o5gs-logs down -v      # ... and deletes it
```

Nothing else needs undoing: the Prometheus job and the Grafana panels stay, reporting "down" /
"no data", and the gate treats them as advisory.

## Known gaps

- The UEs' own log files (`/tmp/ue-*.log`, where "Radio link failure detected" is written) are not
  shipped to Loki; the diagnosis service reads them with one `grep` through `docker exec`, and Loki
  sees their consequences (gNB "signal lost", supervisor restarts).
- Times inside Open5GS and UERANSIM lines are taken as UTC (the images set no time zone); the
  `docker logs --timestamps` prefix and Loki's own timestamps are used whenever present.
- Open5GS has no tracing of its own (no OpenTelemetry in v2.8.0); the traces are of our components'
  decisions only.
