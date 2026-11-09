# Held-out fault diagnosis, commit 4ce595c

| Case | Fault | Expected | Reported (id, severity, score) | Top-1 right | False positives | URLLC p95 before → during |
|---|---|---|---|---|---|---|
| healthy-before | none: the stack as it is | none | nothing | — | 0 | 3.584 → 2.388 ms |
| upf-urllc-stopped | docker stop o5gs-upf-urllc | pfcp-association-loss | target-down critical 0.95; pfcp-association-loss critical 0.85; rlf-no-data-path critical 0.95 | **no** | 0 | 3.81 → None ms |
| gnb-mmtc-stopped | docker stop o5gs-gnb-mmtc | gnb-n2-down | rlf-no-data-path critical 0.95; gnb-n2-down critical 0.75; registration-failure critical 0.60 | **no** | 0 | 3.278 → 4.157 ms |
| pcf-stopped | docker stop o5gs-pcf | target-down | target-down critical 0.95 | yes | 0 | 3.884 → 3.862 ms |
| upf-urllc-cpu-hog | two busy loops inside o5gs-upf-urllc | cpu-saturation | nothing | **no** | 0 | 3.442 → 2.224 ms |
| n6-delay-urllc | netem +15 ms on o5gs-upf-urllc's packets to the URLLC data network (no rule describes this) | none | nothing | — | 0 | 3.767 → 19.194 ms |
| healthy-after | none: the stack after all faults were undone | none | nothing | — | 0 | 2.463 → 2.432 ms |

Faulty cases: top-1 right 1 of 4; expected causes found 3 of 4.
Cases with no expected cause (healthy and uncovered): 3 of 3 with no critical/warning cause.
False positives in all: 0.
