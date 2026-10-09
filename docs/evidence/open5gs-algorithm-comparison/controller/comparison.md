Control laws compared, 300 s per run, mean over rounds

| Policy | URLLC time over 10 ms (%) | URLLC p95 (ms) | URLLC p99 (ms) | URLLC mean (ms) | URLLC flows met (%) | eMBB flows met (%) | eMBB delivered (Mbps) | eMBB admitted | eMBB refused | Limit mean (Mbps) | Limit std (Mbps) | Limit moves |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| static | 71.2 | 25.4 | 52.7 | 6.0 | 100.0 | 60.2 | 260.2 | 360.5 | 2.5 | 500.0 | 0.0 | 0 |
| aimd | 20.5 | 7.2 | 21.0 | 2.3 | 100.0 | 87.1 | 170.2 | 221.5 | 256 | 248.5 | 124.6 | 24.5 |
| pi | 0.0 | 5.8 | 13.0 | 2.1 | 100.0 | 100.0 | 260.7 | 347 | 79 | 420.2 | 41.2 | 36 |
| mpc | 9.0 | 5.1 | 15.5 | 1.9 | 100.0 | 94.8 | 153.5 | 209 | 283 | 222.3 | 105.8 | 50 |
| ucb | 9.0 | 5.4 | 17.2 | 1.9 | 100.0 | 96.9 | 162.9 | 214 | 273.5 | 252.3 | 148.2 | 28.5 |
