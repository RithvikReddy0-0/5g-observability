Run 20261108-200101Z: 660 s, 10 URLLC devices; sent (by sequence numbers) 331237, received 331237, lost 0
devices with loss: none

| Failure = | k | n | Reliability measured | 95 % CI of failure probability | One-sided 95 % upper bound | Meets 99.999 %? |
|---|---|---|---|---|---|---|
| lost (no latency bound) | 0 | 331237 | 100.000000 % | 0.00e+00 – 1.11e-05 | 9.06e-06 (3/n) | yes (95 %) |
| lost or later than 1 ms | 219310 | 331237 | 33.790609 % | 6.60e-01 – 6.64e-01 | 6.63e-01 | no: measured worse |
| lost or later than 2 ms | 77304 | 331237 | 76.662027 % | 2.32e-01 – 2.35e-01 | 2.35e-01 | no: measured worse |
| lost or later than 5 ms | 11129 | 331237 | 96.640170 % | 3.30e-02 – 3.42e-02 | 3.41e-02 | no: measured worse |
| lost or later than 10 ms | 2263 | 331237 | 99.316803 % | 6.55e-03 – 7.12e-03 | 7.07e-03 | no: measured worse |
| lost or later than 20 ms | 592 | 331237 | 99.821276 % | 1.65e-03 – 1.94e-03 | 1.91e-03 | no: measured worse |
| lost or later than 30 ms | 125 | 331237 | 99.962263 % | 3.14e-04 – 4.50e-04 | 4.38e-04 | no: measured worse |
| lost or later than 50 ms | 7 | 331237 | 99.997887 % | 8.50e-06 – 4.35e-05 | 3.97e-05 | no: measured worse |
| lost or later than 100 ms | 0 | 331237 | 100.000000 % | 0.00e+00 – 1.11e-05 | 9.06e-06 (3/n) | yes (95 %) |

To show 99.999 % with zero failures at 95 % confidence needs n >= 300001 packets (rule of three).
