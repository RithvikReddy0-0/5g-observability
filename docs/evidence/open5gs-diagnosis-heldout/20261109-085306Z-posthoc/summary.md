# Held-out fault diagnosis, commit ff654cf

| Case | Fault | Expected | Reported (id, severity, score) | Top-1 right | False positives | URLLC p95 before → during |
|---|---|---|---|---|---|---|
| gnb-mmtc-stopped | docker stop o5gs-gnb-mmtc (re-run after the causal-link fix) | gnb-n2-down | gnb-n2-down critical 0.75; rlf-no-data-path critical 0.95; registration-failure critical 0.60 | yes | 0 | 3.021 → 2.506 ms |
| upf-urllc-cpu-starved | o5gs-upf-urllc limited to 0.5 CPU (docker update) plus two busy loops in it | cpu-saturation | cpu-saturation critical 0.85 | yes | 0 | 3.548 → 56.502 ms |

Faulty cases: top-1 right 2 of 2; expected causes found 2 of 2.
Cases with no expected cause (healthy and uncovered): 0 of 0 with no critical/warning cause.
False positives in all: 0.
