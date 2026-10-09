# Which algorithm is best? State of the art vs our implementation

Every decision our two orchestrators make, the main approaches in the literature, and what we
measured when we implemented the strongest ones on our own stack. Design:
[ADR-018](adr/ADR-018-algorithm-comparison.md). Raw evidence:
[`docs/evidence/open5gs-algorithm-comparison/`](evidence/open5gs-algorithm-comparison/README.md).

**How to read the "best" column.** The literature compares methods in different simulations, so a
method reported as "best" there is not automatically best here. Where we could, we implemented the
candidates and compared them under the same conditions.
- **Live**, on the 100-UE stack, wherever the network itself matters (the latency controller).
- **In simulation** otherwise, driven by our real decision code, with tens of thousands of demands
  (admission, sizing, switching).

Verdicts below are about this testbed; the literature column says what others found.

## 1. Protecting URLLC latency: how much eMBB to admit

| Approach | Family / reference | Literature | Needs | Implemented | Measured here (same seeded demand, 2 rounds) |
|---|---|---|---|---|---|
| Static limit | — | baseline | nothing | yes | URLLC over its 10 ms budget **71 %** of the time; p95/p99 25.4/52.7 ms; eMBB flows met 60 %; eMBB 260 Mbps |
| AIMD | congestion control (Chiu & Jain 1989; TCP) | simple and robust; oscillates in a sawtooth | nothing | yes (ours, ADR-016) | over budget **20.5 %**; 7.2/21.0 ms; eMBB met 87 %; 170 Mbps; limit std 125 |
| **PI on the latency error** | control theory; **PIE, RFC 8033** (Pan et al. 2017) | standard for holding delay at a target; Linux `tc-pie` | 2 gains | **yes** | over budget **0.0 %** in both rounds; 5.8/**13.0** ms; eMBB met **100 %**; **261 Mbps**; limit std **41** |
| MPC | model-predictive control (Zipper, NSDI 2024; Liu 2024 for uRLLC slicing) | strong when the model is right | a model of the plant | yes (one-step, online least-squares model) | over budget 9.0 %; **5.1**/15.5 ms; eMBB met 95 %; 154 Mbps (too cautious) |
| Bandit (UCB) | online learning (UCB1, Auer et al. 2002; LACO 2020; DARIO for slice admission) | model-free, learns online; slow to converge | exploration time | yes (UCB1 over 6 limit levels) | over budget 9.0 %; 5.4/17.2 ms; eMBB met 97 %; 163 Mbps (still exploring after 300 s) |
| Deep RL (PPO / DQN) | DRL for slicing (survey: Sensors 2022; PPO controller: Wu et al.) | reported best in simulation studies | long training, a simulator or live hours; hard to verify | **no**: hours of training on the live testbed, and unsafe exploration on URLLC | — |

**Best here: PI (PIE-style).** In both rounds, in opposite orders, it kept URLLC within budget the whole time, had the best p99, met every eMBB flow, delivered as much eMBB as no control at all, and moved the limit least. MPC held the lowest p95 but gave up 40 % of eMBB: it fits a straight line (fitted live as p95 = −8 + 38 × load) to a relation that stays flat and then climbs steeply, so it stops early. A curved model would do better; that is the price of a model-based method. UCB needs far more than 300 s to settle. AIMD's sawtooth overshoots, then over-cuts.****

## 2. Admission when a slice is contested

Simulated: a 20 Mbps URLLC slice with critical *control* demands (10 Erlangs) and *blog* demands
(12 Erlangs), 20 000 s, the real `decide()` code.

| Approach | Reference | Literature | Implemented | control blocked | blog blocked | value carried |
|---|---|---|---|---|---|---|
| Greedy, complete sharing | — | the default everywhere; no protection | yes (ours) | 20.9 % | 21.4 % | 1963 |
| **Trunk (bandwidth) reservation** | Miller 1969, Lippman 1975 (optimal on a single link); Key 1990 (networks) | optimal or near-optimal for one link; simple | **yes** | **2.1 %** | 53.6 % | **2186** |
| Threshold pricing (online knapsack) | Zhou, Chakrabarty & Lukose 2008 | worst-case competitive guarantee | yes | 0.4 % | 81.1 % | 2089 |
| Deep RL admission | Wu et al.; Filali et al. | reported above heuristics in simulation | no (same reasons as above) | — | — | — |
| Bandit policy selection | DARIO | adapts to drifting demand | no | — | — | — |

**Best here: trunk reservation.** It carries the most value and cuts critical blocking tenfold.
Pricing protects critical demand even more, but turns away most other demand. The guarantee it is
built for is about worst-case input, which our demand is not.

## 3. Which flow to pre-empt

| Approach | Reference | Implemented | Verdict |
|---|---|---|---|
| Lowest ARP priority first, newest first (ours) | 3GPP TS 23.501 §5.7.2.2 | yes (ADR-016) | correct by the standard |
| Multi-criteria: fewest flows, lowest priority, least bandwidth | de Oliveira et al., IEEE/ACM ToN 2004 | no | identical to ours when every flow has the same rate, as all our URLLC flows (1 Mbps) do; worth it only with mixed sizes |
| Rate reduction instead of pre-emption | same paper | no | needs flows that can shrink; ours are fixed-rate |

**Best here: ours (the 3GPP rule)**, for the reason in the table.

## 4. How much capacity a slice needs

Simulated: eMBB with video (8 Mbps, 20 Erlangs) and file (15 Mbps, 8 Erlangs), 280 Mbps mean,
20 000 s.

| Approach | Reference | Implemented | Capacity | video / file blocked (simulated) | Kaufman-Roberts prediction |
|---|---|---|---|---|---|
| Mean × 1.25 headroom | Little's law | yes (ours, ADR-016) | 350 Mbps | 3.3 % / 6.4 % (misses a 1 % target) | 2.9 % / 5.8 % |
| **Kaufman–Roberts, sized for 1 % blocking** | Kaufman 1981; Roberts 1981 (multi-rate Erlang loss) | **yes** | 412 Mbps | **0.5 % / 1.2 %** | 0.5 % / 1.0 % |
| Traffic forecasting (LSTM, TFT, ...) | slice forecasting literature | no | — | — | — |

**Best here: Kaufman–Roberts.** It is exact for this traffic model, its predictions match the
simulation to within a few tenths of a percent, and it sizes for a blocking target rather than a
guessed margin. Forecasting helps predict the *load* that goes into it; it does not replace it.

## 5. When to switch an on-demand slice on and off

Simulated: periodic IoT bursts (60 s every 300 or 600 s, jitter ±10 s), 6 hours, the real closed-loop
decision code, a 17 s activation measured live.

| Approach | Reference | Implemented | demands refused | slice up |
|---|---|---|---|---|
| Always on | — | yes | 0 % | 100 % |
| Reactive threshold + hysteresis | threshold autoscaling | yes (ours, ADR-017) | 42 % | 60 % / 30 % |
| **Predictive (learned period) + reactive** | proactive/hybrid scaling (FLAS 2025; PCLANSA) | **yes** | **3.6 % / 5.1 %** | 69 % / 35 % |
| DRL threshold adaptation | Traffic-aware threshold adjustment (2018) | no | — | — |

**Best here: predictive + reactive (hybrid).** It turns 42 % refusals into 4–5 % for 5–9 points more
time switched on. It only helps when demand has a pattern; with no pattern it behaves exactly like
reactive.

## Summary

| Decision | Before | Best on our stack | Default now |
|---|---|---|---|
| eMBB admission limit | AIMD | PI (PIE-style) | `CONTROL_POLICY=pi` |
| Admission when contested | greedy | trunk reservation | `ADMISSION_POLICY=reservation` |
| Pre-emption | ARP, lowest first | unchanged (3GPP rule) | unchanged |
| Capacity planning | mean × 1.25 | Kaufman–Roberts | `/plan` reports `required_mbps` from KR |
| Switching on demand | reactive | predictive hybrid | `AUTO_POLICY=predictive` |

**Not implemented, on purpose: deep reinforcement learning.** It is the most-cited family in recent
slicing papers, and those papers report it beating heuristics, in simulation. On this testbed every
control step takes 5 s of real time, so training would take many hours of live load. Exploring would
mean deliberately violating URLLC's latency budget while it learns, and the result would be a
policy we cannot explain. A fair next step is to train it offline against a model fitted to these
measurements, and compare it with the PI controller under the same seeded demand.

## References

- J. S. Kaufman, "Blocking in a shared resource environment," IEEE Trans. Commun., 29(10):1474–1481, 1981.
- J. W. Roberts, "A service system with heterogeneous user requirements," in *Performance of Data Communication Systems and their Applications*, North-Holland, 1981.
- P. B. Key, "Optimal control and trunk reservation in loss networks," Probab. Eng. Inf. Sci. 4, 1990.
- R. Pan et al., "Proportional Integral Controller Enhanced (PIE)," RFC 8033, 2017. <https://www.rfc-editor.org/rfc/rfc8033.html>
- P. Auer, N. Cesa-Bianchi, P. Fischer, "Finite-time analysis of the multiarmed bandit problem," Machine Learning 47, 2002.
- D. Chiu, R. Jain, "Analysis of the increase and decrease algorithms for congestion avoidance in computer networks," Computer Networks and ISDN Systems 17, 1989.
- Y. Zhou, D. Chakrabarty, R. Lukose, "Budget constrained bidding in keyword auctions and online knapsack problems," WWW 2008 / WINE 2008. <https://archives.iw3c2.org/www2008/papers/pdf/p1243-zhou.pdf>
- J. C. de Oliveira, C. Scoglio, I. F. Akyildiz, G. Uhl, "New preemption policies for DiffServ-aware traffic engineering to minimize rerouting in MPLS networks," IEEE/ACM ToN 12(4):733–745, 2004.
- A. Balasingam et al., "Zipper" (MPC for vRAN slicing), USENIX NSDI 2024. <https://www.usenix.org/system/files/nsdi24-balasingam.pdf>
- "Deep Reinforcement Learning for Resource Management on Network Slicing: A Survey," Sensors 22(8):3031, 2022. <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9032530/>
- Wu et al., lightweight PPO slice admission (NSF PAR). <https://par.nsf.gov/servlets/purl/10343822>
- LACO: latency-driven slicing orchestration with UCB, 2020. <https://arxiv.org/pdf/2009.03771>
- DARIO: drift-aware slice admission control (UCB). <https://boris.unibe.ch/199022>
- FLAS: combined proactive and reactive autoscaling, 2025. <https://arxiv.org/pdf/2510.20388>
- 3GPP TS 23.501, System architecture for the 5G System, §5.7.2.2 (ARP).
- Trunk reservation optimal on a single link: B. L. Miller (1969), S. A. Lippman (1975).
- "Machine learning for network slicing resource management: a comprehensive survey," 2020. <https://arxiv.org/pdf/2001.07974>
- "Deep reinforcement learning for 6G AI-RAN: a comprehensive survey" (preprint). <https://arxiv.org/pdf/2608.14877>
- J. Liu, MPC-based resource allocation for uRLLC network slicing (doctoral thesis, 2024). <https://publica.fraunhofer.de/handle/publica/480889>
- Traffic-aware threshold adjustment for NFV scaling using DRL, 2018. <https://arxiv.org/pdf/1811.08116>
- D. Bertsimas et al., "Bounds and policies for loss networks." <https://www.mit.edu/~dbertsim/papers/Stochastic%20Networks/Bounds%20and%20policies%20for%20loss%20networks.pdf>
- Kaufman–Roberts recursion, worked reference. <https://metricgate.com/docs/loss-network-kaufman-roberts/>

**Notes.** The competitive ratio ln(U/L)+1 of the online-knapsack pricing (Zhou et al.) should be
checked against the paper before it is quoted. Roberts 1981 is a book chapter. The slicing papers
were found by web search on 9 October 2026; several were read only as abstracts. Every number
measured here comes from our own evidence files, not from these papers.
