# Open5GS evidence — 20260914-060008Z

| | |
|---|---|
| UE interfaces (ground truth) | 20 / 20 |
| User-plane verification | PASS=10  FAIL=0 |
| KPI gate (steady state, before load) | **PASSED** |
| UEs restarted by the supervisor since the UE container started | 1 |

| File | Contents |
|---|---|
| `01-versions.txt` | pins, image IDs, the commit baked into the binaries |
| `02-containers.txt` | container state |
| `03-subscribers.txt` | subscriber records per home slice: DNN, 5QI, ARP, AMBR |
| `04-ue-sessions.txt` | every UE's PDU-session interface and address (UE side), supervisor restarts |
| `05-metrics-audit.txt` | the core's own gauges next to ground truth |
| `06-user-plane.txt` | ping through the UPF per slice, UPF counters, iperf3, internet egress |
| `07-kpi-gate.txt`, `07-kpi-verdict.json` | KPI gate verdict, taken before any load |
| `08-traffic.txt` | concurrent iperf3 per slice against each session's AMBR |
| `09-isolation.txt` | URLLC reachability and RTT, idle vs eMBB saturated |
| `10-prometheus.txt` | scrape targets and the run as Prometheus recorded it |
