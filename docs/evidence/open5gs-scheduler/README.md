# Packet scheduler — evidence (ADR-019)

Produced by `make o5gs-scheduler-test` (`tools/packet-scheduler/experiment.py`) on the live 100-UE
stack. Each run directory holds `results.json` (every measurement, every per-class counter),
`summary.md` (the tables) and `results.xlsx`.

| Run | Parts |
|---|---|
| [`20261108-075626Z`](20261108-075626Z/summary.md) | A isolation (400 Mbps into a 200 Mbps cell, CPU saturated), B ARP tiers, C share sweep |
| [`20261108-082833Z`](20261108-082833Z/summary.md) | D isolation where the cell is the bottleneck (150 Mbps into 100), added after A showed the CPU limiting URLLC. The combined A–D tables are in `20261108-075626Z/summary.md` (`results-merged.json`) |

## How to read it

- **Modes**, compared at the same cell capacity: `off` = no shaping (the stack before ADR-019);
  `fifo` = one drop-tail queue at the cell capacity, i.e. a shared cell with no scheduler;
  `sliced` = the scheduler.
- **RTT** is ping from a device's own PDU-session interface to its UPF: the request goes up, the
  reply comes back down through the cell's downlink queue.
- **Radio-link failures** are counted from the UEs' own logs during the run. A UE that suffers one
  keeps its interface but loses its data path (UERANSIM's gNB drops the session context and never
  restores it), so the harness checks every UE afterwards and re-attaches all of them before the next
  run. No run inherits the damage of the one before.

## Findings

1. **A shared cell without a scheduler fails catastrophically under overload.** In every FIFO run,
   93–99 of the 100 UEs suffered a radio-link failure: heartbeats and RRC queue and drop like any
   other packet. URLLC lost 77–100 % of its probes. With the scheduler: **0 radio-link failures and 0
   URLLC loss** in every run, because signalling and URLLC are classified out of the packets and
   served first.
2. **Where the cell is the bottleneck, the scheduler wins on every measure (D).** eMBB offering
   150 Mbps into a 100 Mbps cell: sliced kept URLLC at a p99 of 4.3–5.0 ms, mMTC at 4.3–5.5 ms and
   even an idle eMBB device at 10–18 ms, with no radio-link failure and 90–93 Mbps delivered. FIFO at
   the same cell: 97–99 radio-link failures, ~107 ms and heavy loss everywhere, 50–58 Mbps delivered.
   (`off` has no cell, so 150 Mbps is not an overload there; it is listed for completeness.)
3. **ARP priority per packet works where the cell is the bottleneck (B).** Six standard URLLC
   devices flooding a 100 Mbps cell: the critical device's p99 was **3.2 and 7.8 ms with 0 % loss**
   (FIFO: 106 and 235 ms, 26 % loss, 93–94 radio-link failures). The flooded standard tier lost
   24–28 % — the short queue drops its excess instead of letting its latency grow.
4. **Guaranteed shares are a working tuning knob (C).** mMTC delivered 3.7 / 5.6 / 8.4 / 11.2 / 14.0
   Mbps for guarantees of 3 / 6 / 9 / 12 / 15 Mbps, with eMBB taking the rest of the 60 Mbps cell.
5. **When the CPU saturates, URLLC's tail is CPU contention, which no queue can fix (A).** At 400
   Mbps of eMBB the load average reached ~10 on 12 threads. URLLC p99 was 27–48 ms sliced and 28–31 ms
   unshaped: the same, because the host spends its CPU on eMBB before the cell drops the excess.
   Admission control (ADR-016/018) is the layer that prevents that; the scheduler does not replace it.
6. **Unresolved: an overloaded slice's own devices.** With eMBB offered at twice the cell, an
   *unloaded* eMBB device's p99 was 12.5 s sliced against 1.9–2.7 s unshaped. Its own packets are a
   sparse flow in fq_codel and the measured kernel queues on its path were near-empty (UPF tunnel
   empty, gNB N3 socket ≤ 200 KB, cell drops counted in the leaf queue), so the delay builds inside
   the overloaded eMBB gNB process, whose internal queues are not observable. It affects only the
   overloaded slice, only with the CPU saturated (in D the same device stayed at 10–18 ms), and
   URLLC, mMTC and signalling were unaffected.

## A measurement bug found while reading these

`packet_scheduler_class_drops_total` first read the HTB **class** counter, which counts only enqueue
failures; fq_codel drops a fat flow's packets inside the leaf queue (883 948 drops against 886 at
the class, in one overload). The scheduler now reports drops and backlog from each class's leaf
queue. The `classes.*.drops` values in `20261108-075626Z/results.json` predate the fix and
undercount; nothing in the summary tables uses them. One radio-link-failure count came out as −1
(a UE restarted mid-run and started a fresh log); the harness now clamps at 0, and the raw value is
kept as `radio_link_failures_raw` in `20261108-082833Z/results.json`.
