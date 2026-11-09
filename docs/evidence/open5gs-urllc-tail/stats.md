| Comparison (urllc.owd_p99_ms) | n | Median difference | p |
|---|---|---|---|
| off − fifo-10g+sliced-10g | 4 / 8 | +4.79 | 0.39 |
| off − fifo-10g | 4 / 4 | +3.19 | 0.46 |
| off − sliced-10g | 4 / 4 | +6.40 | 0.49 |
| off − sliced | 4 / 4 | +4.55 | 0.17 |
| fifo-10g+sliced-10g+sliced-ul-only − off | 12 / 4 | -4.79 | 0.25 |
| sliced − sliced-dl-only | 4 / 4 | +0.90 | 0.74 |
| sliced-ul-only − off | 4 / 4 | -5.51 | 0.49 |
| off+fifo-10g+sliced-10g+sliced-ul-only − sliced-dl-only+sliced+prio+drr+hfsc | 16 / 20 | +0.52 | 0.74 |
| prio − sliced | 4 / 4 | -0.22 | 0.83 |
| drr − sliced | 4 / 4 | +1.01 | 0.63 |
| hfsc − sliced | 4 / 4 | +0.11 | 0.80 |

| Spearman ρ with urllc.owd_p99_ms over 36 runs | ρ |
|---|---|
| cpu.amf | constant, undefined |
| cpu.dn-mmtc | constant, undefined |
| cpu.dn-urllc | +0.69 |
| cpu.gnb-embb | +0.01 |
| cpu.gnb-mmtc | +0.88 |
| cpu.gnb-urllc | +0.80 |
| cpu.smf | +0.30 |
| cpu.ue | -0.39 |
| cpu.upf-embb | +0.01 |
| cpu.upf-mmtc | +0.85 |
| cpu.upf-urllc | +0.77 |
| host.load1_end | -0.19 |
| host.load1_start | -0.29 |
| embb.dl_aggregate_mbps | -0.26 |
| run order | -0.54 |

| Arm | mean rank of urllc.owd_p99_ms within a round (1 = shortest), 4 complete rounds |
|---|---|
| sliced-10g | 2.75 |
| sliced-ul-only | 3.75 |
| sliced | 4.75 |
| prio | 5.00 |
| fifo-10g | 5.25 |
| sliced-dl-only | 5.25 |
| drr | 5.50 |
| hfsc | 6.00 |
| off | 6.75 |

Friedman chi-square 5.93, df 8, p = 0.65
