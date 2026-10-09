## A. Admission on a contested URLLC slice (20 Mbps; control 10 E + blog 12 E offered; 20 000 s)

| Policy | control blocked | blog blocked | value carried (k units) |
|---|---|---|---|
| greedy (previous default) | 20.92 % | 21.44 % | 1963.3 |
| trunk reservation, 4 Mbps | 2.12 % | 53.56 % | 2186.0 |
| threshold pricing | 0.36 % | 81.07 % | 2089.3 |

## B. Sizing eMBB (video 20 E x 8 Mbps + file 8 E x 15 Mbps = 280 Mbps mean; 20 000 s)

| Method | capacity | video blocked (simulated / KR predicted) | file blocked (simulated / KR predicted) |
|---|---|---|---|
| mean x 1.25 (previous default) | 350 Mbps | 3.27 % / 2.94 % | 6.42 % / 5.79 % |
| Kaufman-Roberts, 1 % target | 412 Mbps | 0.51 % / 0.46 % | 1.21 % / 0.98 % |
