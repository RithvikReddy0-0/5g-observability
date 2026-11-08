# Standards mapping — KPIs, metrics and slice templates against 3GPP, GSMA and IETF

[ADR-023](adr/ADR-023-standards-and-statistics.md). This maps what the project measures and
configures to the standards that define the same things, and says **exactly where our definition
differs**. A KPI that is "like" a 3GPP KPI but measured over a different span or with a different
statistic is not that KPI. Read the tables as "closest standard counterpart, and the gap".

## Documents read

Every clause cited below was read in these versions. Nothing is cited from memory; where a
document was not read, the row says **unverified**.

| Document | Version read | Source |
|---|---|---|
| GSMA PRD NG.116, Generic Network Slice Template | v10.0, October 2024 | <https://www.gsma.com/newsroom/wp-content/uploads//NG.116-v10.0.pdf> |
| 3GPP TS 28.554, 5G end to end KPI | V17.14.0 (ETSI TS 128 554, 2024-07) | <https://www.etsi.org/deliver/etsi_ts/128500_128599/128554/17.14.00_60/ts_128554v171400p.pdf> |
| 3GPP TS 28.552, 5G performance measurements | V17.16.0 (ETSI TS 128 552, 2025-01) | <https://www.etsi.org/deliver/etsi_TS/128500_128599/128552/17.16.00_60/ts_128552v171600p.pdf> |
| 3GPP TS 28.541, 5G Network Resource Model | V17.16.0 (ETSI TS 128 541, 2024-10) | <https://etsi.org/deliver/etsi_ts/128500_128599/128541/17.16.00_60/ts_128541v171600p.pdf> |
| 3GPP TS 22.261, Service requirements for the 5G system | V17.11.0 (ETSI TS 122 261, 2022-10) | <https://www.etsi.org/deliver/etsi_ts/122200_122299/122261/17.11.00_60/ts_122261v171100p.pdf> |
| 3GPP TS 23.501, System architecture for the 5GS | V17.9.0 (ETSI TS 123 501, 2023-07) | <https://www.etsi.org/deliver/etsi_ts/123500_123599/123501/17.09.00_60/ts_123501v170900p.pdf> |
| IETF RFC 7679, One-Way Delay Metric for IPPM | — | <https://www.rfc-editor.org/rfc/rfc7679.txt> |
| IETF RFC 7680, One-Way Loss Metric for IPPM | — | <https://www.rfc-editor.org/rfc/rfc7680.txt> |
| IETF RFC 3393, IP Packet Delay Variation Metric | — | <https://www.rfc-editor.org/rfc/rfc3393.txt> |

Release 17 is used for all 3GPP documents: NG.116 v10.0 states (1.2) that its attributes take
3GPP Release 16 as the basis, and Release 17 is the newest release whose ETSI publications were
read here. **Not read, so not relied on:** TS 22.104 (cyber-physical control; TS 22.261 V17.11.0
7.2.2 points there for URLLC KPI values, and TS 28.541 cites it for `jitter`), ITU-R M.2410 and
TR 38.913 (cited by `kpi-scorecard.json`; still unverified), RFC 3550 and RFC 5481, TS 24.501.

## 1. Project KPIs against TS 28.554 / TS 28.552 / TS 22.261 / TS 23.501

How we measure (from the code): URLLC and mMTC packets are timestamped with `CLOCK_MONOTONIC` in
`traffic_agent.py` just before `sendto()` on the UE's own PDU-session address, and received by
`kpi_collector.py` in the slice's data-network container, which takes the time right after
`recvfrom()`. The path is UE → gNB (UERANSIM, RLS over UDP — no air interface) → GTP-U → UPF → N6 →
data network. All latency is **uplink, one-way, application to application**. eMBB throughput is
iperf3 TCP goodput, data network → UE, per device, over the instance window.

| Project KPI (`kpi-scorecard.json`) | Closest standard definition | How ours differs |
|---|---|---|
| `urllc-latency-mean`, `-p95`, `-p99` (`urllc.owd_*_ms`) | **TS 28.554 6.3.1.8.1 DelayE2EUlNs**, "average e2e UL packet delay between the PSA UPF and the UE for a network slice … from the time when an UL RLC SDU was scheduled at the UE until the time when the corresponding GTP PDU was received by the PSA UPF", MEAN, unit 0.1 ms; built from **TS 28.552 5.4.9** (one way packet delay between PSA UPF and UE, `GTP.DelayUlPsaUpfUeMean/Dist.SNSSAI`). | Span: ours starts in the UE's IP stack (before the TUN device and RLS encapsulation), and ends in the data network after the N6 hop, socket and Python — 28.554 ends at the PSA UPF. Ours is a superset of that span, so it can only read **higher**. Statistic: 28.554 defines a mean; our p95/p99 correspond to the distribution measurement (`…Dist`) of 28.552, not to a 28.554 KPI. RAN: simulated, so the RAN component has none of the HARQ/scheduling delay a radio has. |
| same | **TS 22.261 3.1 end-to-end latency**: "the time that it takes to transfer a given piece of information from a source to a destination, measured at the communication interface". | Closest in spirit (application to application), but ours is one-way and the destination is our collector, not an application server. |
| same, against the 10 ms target | **TS 23.501 5.7.3.4 PDB**: "an upper bound for the time that a packet may be delayed between the UE and the N6 termination point at the UPF"; 5QI 82 = 10 ms (Table 5.7.4-1), of which NOTE 4 assigns a static 1 ms CN PDB "between a UPF terminating N6 and a 5G-AN". | PDB is a bound, not a percentile. For a Delay-critical GBR flow (5QI 82) "packets delayed more than the PDB are added to the PER" — so a p99 above 10 ms means more than 1 % of packets count as lost against a 10⁻⁴ PER (see §5). Our span adds the N6 hop the PDB excludes. |
| `urllc-packet-loss` (`urllc.loss_ratio`) | **TS 28.554 6.8** Reliability KPIs: `ULRelPSR_Uu` (6.8.1.2), `ULRelPSR_N3` (6.8.1.4), packet success rate per interface, built from TS 28.552 `DRB.PacketSuccessRateUlgNBUu` and `GTP.InDataPktN3UPF` / `GTP.InDataPktPacketLossN3UPF`. 6.8 defines reliability by TS 22.261 3.1: "percentage value of the packets successfully delivered to a given system entity within the time constraint required by the targeted service out of all the packets transmitted". | Ours is end to end (all interfaces at once) where 28.554 splits it per interface (Uu, N3). Ours has **no time constraint**: a packet arriving 400 ms late counts as delivered. TS 23.501's PER is a bound on **non-congestion** link-layer loss (5.7.3.5); ours includes queue drops in the cell scheduler, so comparing it with the PER is conservative. |
| `urllc-jitter` (`urllc.jitter_ms`) | TS 28.541 ServiceProfile `jitter`: "the deviation from the desired value to the actual value when assessing time parameters", microseconds, reference "TS 22.104 clause C.4.1" (not read). No jitter or delay-variation measurement exists in TS 28.552 V17.16.0 (searched every `e)` measurement name). | A different quantity: 28.541's jitter is deviation from a *desired* value; ours is the mean change in delay between consecutive packets, an RFC 3393 IPDV statistic (§4). |
| `embb-throughput-p5`, `-mean` (`embb.dl_p5_mbps`) | **TS 22.261 Table 7.1-1** "Experienced data rate (DL)": 50 Mbit/s DL, 25 Mbit/s UL for urban macro and rural macro (note 5: "targeted values and not strict requirements"). The project target of 50 Mbps equals the urban-macro value. TS 28.554 **6.3.6 RAN UE throughput** (6.3.6.2: volume and time are counted only while data is in the buffer, and "the last slot shall always be removed"). | Ours is TCP goodput at the UE application over the whole window (slow start, idle and retransmission time included), then the 5th percentile across 20 devices. 6.3.6 removes buffer-empty time and is measured in the RAN per cell. The "5th percentile" framing comes from ITU-R M.2410 via the scorecard — **unverified** here. |
| `embb-throughput-aggregate` (`embb.dl_aggregate_mbps`) | **TS 28.554 6.3.3 DTSNSI**, downstream throughput of a single network slice: octets on N3 from all UPFs to NG-RAN for the slice ÷ granularity period, kbit/s, MEAN; from TS 28.552 5.4.1.4 `GTP.OutDataOctN3UPF.SNSSAI`. | Ours sums goodput at the receivers: no GTP-U/IP/TCP header bytes, no retransmissions, and only the iperf3 flows (not every packet of the slice). DTSNSI counts every octet leaving the UPF on N3. `upf_slice_exporter` (TUN-device bytes per slice UPF) is the closer counterpart; it counts inner IP bytes, without GTP-U. |
| `mmtc-registration-success` (`mmtc.registration_success`) | **TS 28.554 6.2.3 RSR**, registration success rate of one network slice: `AMF.5GSRegisSucc` / `AMF.5GSRegisAtt` × 100 %, RATIO; per TS 28.552 **5.2.2.1** `RM.RegInitReq.SNSSAI` (counted "on receipt by the AMF … of Registration Request" for an initial registration) and **5.2.2.2** `RM.RegInitSucc.SNSSAI` (on transmission of Registration Accept). | Ours is counted at the UE, from each UE's log at its latest attach: successes ÷ (successes + logged failures). An attempt that never got an answer is in 28.552's denominator but not ours. 28.552 has **no** failure counter `RM.RegInitFail`; Open5GS's `fivegs_amffunction_rm_reginitfail` is an Open5GS addition. |
| `mmtc-packet-loss` (`mmtc.loss_ratio`) | As URLLC loss; 5QI 9 PER is 10⁻⁶ (TS 23.501 Table 5.7.4-1). | As URLLC loss. ~1 000 reports per run cannot resolve rates below ~3·10⁻³ (rule of three). |
| `mmtc-report-latency` (`mmtc.owd_p99_ms`) | 5QI 9 PDB 300 ms (Table 5.7.4-1): for non-GBR, "in uncongested scenarios, 98 percent of the packets should not experience a delay exceeding the 5QI's PDB" (5.7.3.4). The scorecard's 10 s comes from TR 38.913 — **unverified**. | Uplink, one way, same span caveats as URLLC. 1-ms timestamp resolution (10-byte packet carries ms). |
| `mmtc-devices-reporting` | TS 28.554 6.2.1 AMFMeanRegNbr (mean registered subscribers per slice, at the AMF). | Ours counts devices heard from at the data network — a data-path liveness count, not a registration count. |
| (gate) deployment PDU sessions per slice | TS 28.554 **6.4.1 PDUSesMeanNbr** from TS 28.552 **5.3.1.1** `SM.SessionNbrMean.SNSSAI` ("sampling at a pre-defined interval, the number of PDU sessions … and then taking the arithmetic mean"). | Open5GS exports the instantaneous count; the gate compares it, not a sampled mean. |

## 2. Prometheus metrics against TS 28.552

| Metric (exporter) | TS 28.552 V17.16.0 measurement | Note |
|---|---|---|
| `fivegs_smffunction_sm_sessionnbr{snssai}` (Open5GS SMF) | 5.3.1.1 `SM.SessionNbrMean.SNSSAI` (and 5.3.1.2 `SM.SessionNbrMax`) | 28.552 defines the mean/max of samples; Open5GS gives the current value. The one exact per-slice count (kpi-gates.json). |
| `fivegs_smffunction_sm_pdusessioncreationreq` / `…succ` | 5.3.1.3 `SM.PduSessionCreationReq.SNSSAI`, 5.3.1.4 `SM.PduSessionCreationSucc.SNSSAI` → TS 28.554 6.2.5 `PDUSessionEstSR` | Not usable as-is: on this stack `…succ` counted each success twice (a 200 % rate; kpi-gates.json). |
| `fivegs_smffunction_sm_qos_flow_nbr` | 5.3.2.1.7 `SM.QoSflowNbrMean.SNSSAI` | Read 20/20 against 10/10 real flows (kpi-gates.json): not used. |
| `fivegs_amffunction_rm_reginitreq` / `…succ` | 5.2.2.1 `RM.RegInitReq.SNSSAI`, 5.2.2.2 `RM.RegInitSucc.SNSSAI` → TS 28.554 6.2.3 RSR | Same names; the RSR KPI could be computed from them per S-NSSAI. |
| `fivegs_amffunction_rm_reginitfail` | none | No such measurement in 28.552 V17.16.0. |
| `fivegs_amffunction_rm_registeredsubnbr` | 5.2.1.1 `RM.RegisteredSubNbrMean.SNSSAI` | Mean of samples in 28.552; current value in Open5GS. |
| `fivegs_upffunction_upf_qosflows` | 5.4.10.1 `UPF.MeanQosFlows` (optionally per S-NSSAI and DNN) | Lower bound only after UE restarts (kpi-gates.json). |
| `fivegs_upffunction_upf_sessionnbr` | none found (no `UPF.*Session*` measurement) | Open5GS addition. |
| `fivegs_ep_n3_gtp_{in,out}datavolumeqosleveln3upf` | 5.4.1.x N3 GTP data volume per QoS level | Labelled by QFI only, which mixes slices (upf_slice_exporter.py explains why). |
| `upf_slice_*` (repo, per-slice UPF TUN counters) | 5.4.1.1 `GTP.InDataPktN3UPF.SNSSAI`, 5.4.1.3 `GTP.InDataOctN3UPF.SNSSAI`, 5.4.1.4 `GTP.OutDataOctN3UPF.SNSSAI` | Same per-slice split (one UPF per slice), but counted on the N6 side of the UPF (inner IP packets), not GTP-U PDUs on N3. |
| `kpi_owd_ms{slice,stat}` (kpi_collector) | 5.4.9 one way packet delay between PSA UPF and UE (UL: `GTP.DelayUlPsaUpfUeMean/Dist`) | Different span (§1). |
| `kpi_packets_received_total` / `kpi_packets_expected_total` | 28.554 6.8 PSR inputs (`GTP.InDataPktN3UPF`, `…PacketLossN3UPF`) | End to end, not per interface. |
| `ue_probe_rtt_ms` (ue_probe_exporter) | 28.552 has RTT only between PSA UPF and NG-RAN (`GTP.RttDelayPsaUpfNgranMean/Dist`) | ICMP round trip UE ↔ data network; no 28.552 counterpart. |
| `ran_ue`, `gnb`, `pfcp_peers_active` (Open5GS) | none identified | Implementation gauges. (28.554 6.2.10 `PFCPSessionEstSR` is about PFCP *session* establishment, not peers.) |

## 3. NEST attributes against TS 28.541 ServiceProfile

The NESTs are `deployments/open5gs/slice-templates/*.nest.yaml`, validated by
`tools/standards/check_nest.py`. NG.116 Annex A and TS 28.541 Annex L both say GST attributes are
*input* to the ServiceProfile, which is then translated into SliceProfiles per domain. TS 28.541
Annex L Table L.2.1 maps only four GST parameters explicitly; the other rows below are matches by
name and definition (TS 28.541 6.3.3.2 attribute list, and the YANG descriptions in Annex N),
marked as such.

| NG.116 v10.0 parameter (clause) | Our value (URLLC / eMBB / mMTC) | TS 28.541 V17.16.0 ServiceProfile attribute | Basis |
|---|---|---|---|
| Maximum number of UEs (3.4.17) | 10 / 20 / 70 | `maxNumberofUEs` | Annex L Table L.2.1 |
| Maximum number of PDU sessions (3.4.16) | 30 / 30 / 70 | `maxNumberofPDUSessions` (printed "maxNumberofConns" in Table L.2.1) | Annex L Table L.2.1 |
| Maximum downlink throughput per UE value (3.4.6) | 20 000 / 200 000 / 1 000 kbps | `dLThptPerUE` | Annex L Table L.2.1 |
| Maximum Uplink Throughput per UE value (3.4.32) | 20 000 / 200 000 / 1 000 kbps | `uLThptPerUE` | Annex L Table L.2.1 |
| Maximum / Guaranteed downlink throughput (3.4.5) | 20 000 / 500 000 (GBR quota 0) / 10 000 kbps | `dLThptPerSlice` (data type with guaranteed and maximum rate) | by name |
| Supported device velocity (3.4.28) | 0 km/h | `uESpeed` | by name |
| 3GPP 5QI, Resource Type (3.4.26) | 82 Delay critical GBR / 9 Non-GBR / 9 Non-GBR | none in the 6.3.3.2 list | QoS is configured in the core (subscriber records), not carried in the ServiceProfile |
| Packet Delay Budget (3.4.26) | 0.01 s / — / 0.3 s | nearest: `uLLatency`, `dLLatency` ("required UL packet transmission latency (millisecond) through the RAN, CN, and TN part … See clause 6.3.1 of 28.554") | by definition; PDB is UE ↔ N6 per 5QI, `uLLatency` is per slice |
| Packet Error Rate (3.4.26) | 10⁻⁴ / — / 10⁻⁶ | nearest: `reliability` ("percentage value of the amount of sent network layer packets successfully delivered to a given system entity within the time constraint … divided by the total number of sent network layer packets", ref. TS 22.261, TS 22.104) | by definition; the complement, and `reliability` includes the time constraint |
| Supported data network list (3.4.39) | urllc / internet / iot | none in the 6.3.3.2 list | — |
| S-NSSAI (nest.sst / nest.sd) | 2/112233, 1/010203, 3/334455 | `sST` (Mandatory); SD presumably within `pLMNInfoList` (Mandatory) — **its content was not read** | — |

Not used in our NESTs, though GSMA's example NESTs (NG.116 clause 4) list them: **Availability**
(99.999 for URLLC, 99,9 for MIoT; ServiceProfile `availability`) — a 150-second run cannot measure
availability; **UE density** (100 000 for MIoT; `termDensity`) — a laptop has no area. The ARP
priorities in `slices.env` have **no GST attribute**; in particular they are *not* the 5QI
"Priority Level" (3.4.26), which orders QoS flows in scheduling (TS 23.501 5.7.3.3), whereas ARP
decides admission and pre-emption (5.7.2.2).

## 4. Latency, loss and jitter against RFC 7679, RFC 7680 and RFC 3393

| Point | RFC | Ours |
|---|---|---|
| What is timed | RFC 7679 3.4: wire time from the first bit sent to the last bit received; 3.7.2 calls software timestamps "host times" and bounds their error separately. | Host time: `time.monotonic_ns()` in user space before `sendto()` and after `recvfrom()`, plus Python scheduling. |
| Clock synchronisation | RFC 7679 3.7.1: synchronisation error between source and destination clocks adds to the error. | Sender and receiver read the **same** kernel `CLOCK_MONOTONIC` (containers share a kernel; no time namespace), so this error term is zero — the reason one-way delay is exact here (kpi_collector.py). |
| Sampling | RFC 7679 4: Type-P-One-way-Delay-Poisson-Stream; "Poisson sampling is only one way of defining a sample". | Periodic: one packet per device every 20 ms (URLLC), every 10 s (mMTC), random start phase per device. Periodic streams (RFC 3432) were not read. |
| Lost packets in percentiles | RFC 7679 5.1–5.2: "undefined values are treated as infinitely large" when computing percentiles and the median. | Percentiles are computed over **received packets only**. With no loss this is the same thing. With loss it is optimistic: FIFO run `140404Z` lost 2.9 % of URLLC packets, so its RFC 7679 p99 is *undefined (infinite)*, not the 8.0 ms reported. |
| Loss threshold | RFC 7680 2.4/2.6: a packet not received within Tmax is lost; "If the packet fails to arrive within a reasonable period of time, Tmax, the one-way packet loss is taken to be one". | **No Tmax.** Any packet received before the run's statistics are read counts as received. Loss is counted from sequence gaps: expected = last − first + 1 per (device, epoch). |
| Loss statistic | RFC 7680 4.1 Type-P-One-way-Packet-Loss-Ratio: "the ratio of losses to total packets in the stream". | Same ratio, over the run, per slice. Packets lost after a device's last received packet are invisible (tail loss), negligible over thousands of packets. |
| Duplicates | RFC 7680 2.5: a duplicated packet "is counted as received" (once). | Each arrival increments `received`; a duplicate therefore *offsets* a real loss in the same stream (expected is clamped to ≥ received). No duplicates were seen, but the counter cannot tell. |
| Jitter | RFC 3393 2.4: ipdv is the (signed) difference of the one-way delays of two packets chosen by a selection function F; 2.5 lists "consecutive Type-P packets within the specified interval" as one F; 4.1 discards pairs where one packet has undefined delay; 4.5 (`Type-P-One-way-ipdv-jitter`) uses consecutive packets and the absolute value of ipdv. RFC 3393 4.5 also describes RFC 1889's (RTP) estimate: the absolute ipdv passed through an exponential filter with parameter 1/16. | `kpi_collector.py`: `|D_i − D_(i−1)|` for consecutive **received** packets of the same device, in **arrival** order, averaged over all devices' pairs. This is **RFC 3393 ipdv with the consecutive-packet selection function, reported as the mean absolute value** (RFC 3393 does not fix the aggregate; 4.5 takes absolute values). It is **not** the RTP interarrival jitter (no 1/16 filter, no RTP timestamps). Two departures: (1) a pair spans a lost packet instead of being discarded (RFC 3393 4.1 discards pairs with an undefined delay and does not address pairing across a gap); (2) pairs follow arrival order, so a reordered packet changes the pairing. With no loss and no reordering — every run except the two FIFO runs — it equals the RFC 3393 statistic. |

## 5. URLLC requirements against TS 22.261 and TS 23.501

| Requirement | Where | What it means for our measurement |
|---|---|---|
| 5QI 82: Delay-critical GBR, priority 19, PDB 10 ms, PER 10⁻⁴, MDBV 255 bytes, averaging window 2000 ms | TS 23.501 Table 5.7.4-1 | Our URLLC packets are 64 bytes every 20 ms per device, well inside 255-byte bursts. |
| A delay-critical packet later than the PDB is lost | TS 23.501 5.7.3.4 and 5.7.3.5 ("a packet which is delayed more than PDB is counted as lost, and included in the PER unless the data burst is exceeding the MDBV … or the QoS Flow is exceeding the GFBR") | Loss and lateness must be judged together. A run with p99 > 10 ms has > 1 % of packets lost in this sense, 100 × the PER, even with zero sequence loss. Four of the nine same-day runs — every sliced run of the controlled block — are in that state (statistics.md). |
| CN PDB 1 ms (static), the rest for the radio | TS 23.501 Table 5.7.4-1 NOTE 4 | Our 10 ms target is the whole PDB; a radio would get 9 ms of it. Our path also includes N6. |
| Reliability = packets delivered within the service's time constraint ÷ packets sent | TS 22.261 3.1 (and TS 28.554 6.8, TS 28.541 `reliability`) | Our loss ratio is not reliability: it has no time constraint. Reliability ≤ 1 − loss − late fraction. |
| 99,999 % reliability | TS 22.261 V17.11.0 Table 7.2.3.2-1 (wireless road-side infrastructure backhaul: max end-to-end latency 30 ms, survival time 100 ms, communication service availability 99,9999 %, reliability 99,999 %); NOTE 7: "All the values in this table are example values and not strict requirements". | This is the only 99,999 % reliability figure in TS 22.261 V17.11.0; its clause 7.2.2 refers URLLC KPI tables to TS 22.104, which was not read. The commonly quoted "URLLC = 99.999 %" is therefore cited here as this example row, not as a general requirement. GSMA's URLLC NEST (NG.116 4.2) lists **availability** 99.999, a different quantity (3.4.1: time the service is delivered at agreed QoS ÷ time it is expected to be). |
| Demonstrating 99.999 % | statistics (rule of three) | With zero failures, ≥ 299 572 consecutive packets are needed for a one-sided 95 % bound of 10⁻⁵ — about 10 minutes of the URLLC profile, with **no packet lost and none over 10 ms**. See [statistics.md](evidence/open5gs-kpi/statistics.md). |
