#!/usr/bin/env python3
"""Generate deployments/open5gs/observability/grafana/dashboards/open5gs-slices.json.

Run: python3 gen_dashboard.py dashboards/open5gs-slices.json

Generate the Open5GS slice dashboard. Every query uses metric and label names observed on
the running stack (snssai="1-010203", slice="eMBB"), not names assumed from documentation."""
import json, sys

DS = {"type": "prometheus", "uid": "prom-o5gs"}
panels, pid = [], [0]


def nid():
    pid[0] += 1
    return pid[0]


def row(title, y):
    panels.append({"type": "row", "title": title, "id": nid(), "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}})


def stat(title, expr, x, y, w=6, desc=""):
    panels.append({
        "type": "stat", "title": title, "id": nid(), "datasource": DS, "description": desc,
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value",
                    "graphMode": "none", "textMode": "value"},
        "fieldConfig": {"defaults": {"unit": "none", "decimals": 0,
                                     "color": {"mode": "fixed", "fixedColor": "#2E6E8E"}}},
        "targets": [{"refId": "A", "datasource": DS, "expr": expr, "instant": True}],
    })


def ts(title, targets, x, y, w=12, h=8, unit="none", desc="", stack=False):
    panels.append({
        "type": "timeseries", "title": title, "id": nid(), "datasource": DS, "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                     "custom": {"lineWidth": 2, "fillOpacity": 12,
                                                "stacking": {"mode": "normal" if stack else "none"}}},
                        "overrides": [
                            {"matcher": {"id": "byRegexp", "options": ".*(eMBB|1-010203|internet).*"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#2E6E8E"}}]},
                            {"matcher": {"id": "byRegexp", "options": ".*(URLLC|2-112233|urllc).*"},
                             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#C7601F"}}]},
                        ]},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi"}},
        "targets": [dict(refId=chr(65 + i), datasource=DS, **t) for i, t in enumerate(targets)],
    })


y = 0
row("Core — control plane (native Open5GS metrics)", y); y += 1
stat("gNBs connected", "sum(gnb)", 0, y, desc="AMF metric `gnb`.")
stat("UEs on the RAN", "sum(ran_ue)", 6, y, desc="AMF metric `ran_ue`.")
stat("PDU sessions at the UPF", "sum(fivegs_upffunction_upf_sessionnbr)", 12, y,
     desc="UPF gauge. A lower bound: after a UE restart it read 21 for 20 real sessions. The exact per-slice count is the SMF panel below.")
stat("Scrape targets up", 'sum(up)', 18, y, desc="All Open5GS exporters plus the slice traffic exporter.")
y += 4

ts("PDU sessions per slice", [
    {"expr": "sum by (snssai) (fivegs_smffunction_sm_sessionnbr)", "legendFormat": "S-NSSAI {{snssai}}"}],
   0, y, desc="Native SMF gauge, labelled by S-NSSAI — slice identity that free5GC's metrics never carried.")
ts("User-plane QoS flows per slice (UPF, by DNN)", [
    {"expr": "sum by (dnn) (fivegs_upffunction_upf_qosflows)", "legendFormat": "DNN {{dnn}}"}],
   12, y, desc=("UPF gauge; DNN internet = eMBB, urllc = URLLC. Presence per slice; can over-count after a UE restart. The SMF's qos_flow_nbr{snssai,fiveqi} is NOT "
               "shown: it read 20/20 against 10/10 real flows because it is never decremented on re-attach."))
y += 8

row("Liveness and latency — measured from the UEs", y); y += 1
stat("eMBB UEs reachable", 'sum(ue_probe_reachable{slice="eMBB"})', 0, y,
     desc="UEs whose probe through the UPF answered in the last 5 s round. Core metrics do not notice vanished UEs; this does.")
stat("URLLC UEs reachable", 'sum(ue_probe_reachable{slice="URLLC"})', 6, y)
stat("URLLC worst RTT (budget 10 ms)", 'max(ue_probe_rtt_ms{slice="URLLC",stat="max"})', 12, y,
     desc="Worst UE of the last round, UE -> gNB -> UPF gateway. SLICE_B_MAX_LATENCY_MS = 10.")
stat("eMBB worst RTT (budget 300 ms)", 'max(ue_probe_rtt_ms{slice="eMBB",stat="max"})', 18, y)
panels[-2]["fieldConfig"]["defaults"].update({"unit": "ms", "decimals": 2})
panels[-1]["fieldConfig"]["defaults"].update({"unit": "ms", "decimals": 2})
y += 4
ts("Round-trip time per slice (avg and worst UE)", [
    {"expr": 'max by (slice) (ue_probe_rtt_ms{stat="avg"})', "legendFormat": "{{slice}} avg"},
    {"expr": 'max by (slice) (ue_probe_rtt_ms{stat="max"})', "legendFormat": "{{slice}} worst"}],
   0, y, w=24, h=10, unit="ms", desc=("Probed every 5 s from every UE interface. Measured: with eMBB saturated, URLLC average RTT did not rise "
                      "but up to 4 of 10 URLLC probes per round went unanswered; its worst case exceeded 10 ms even at idle."))
y += 10

row("User plane — measured traffic per slice", y); y += 1
ts("Downlink throughput per slice", [
    {"expr": "sum by (slice) (rate(upf_slice_downlink_bytes_total[15s])) * 8 / 1e6", "legendFormat": "{{slice}}"}],
   0, y, unit="Mbits", desc=("Measured from the slice's own UPF TUN device (ogstun = eMBB, ogstun2 = URLLC). "
                            "Open5GS's native UPF volume counters are labelled only by QFI and cannot separate slices."))
ts("Uplink throughput per slice", [
    {"expr": "sum by (slice) (rate(upf_slice_uplink_bytes_total[15s])) * 8 / 1e6", "legendFormat": "{{slice}}"}],
   12, y, unit="Mbits", desc="Measured from the slice's own UPF TUN device.")
y += 8
ts("Packets per second per slice", [
    {"expr": "sum by (slice) (rate(upf_slice_downlink_packets_total[15s]))", "legendFormat": "{{slice}} downlink"},
    {"expr": "sum by (slice) (rate(upf_slice_uplink_packets_total[15s]))", "legendFormat": "{{slice}} uplink"}],
   0, y, h=10, unit="pps")
ts("Dropped packets per slice", [
    {"expr": "sum by (slice) (rate(upf_slice_dropped_packets_total[1m]))", "legendFormat": "{{slice}}"}],
   12, y, h=10, unit="pps", desc="Drops on the slice's UPF device. AMBR policing happens in the gNB, so it does not show here.")
y += 10

row("Slice orchestrator — demands admitted by measured capacity, run as real flows", y); y += 1
ts("eMBB: capacity, admitted, measured", [
    {"expr": 'max(slice_capacity_mbps{slice="eMBB"})', "legendFormat": "capacity"},
    {"expr": 'max(slice_allocated_mbps{slice="eMBB"})', "legendFormat": "admitted"},
    {"expr": 'max(slice_measured_mbps{slice="eMBB"})', "legendFormat": "measured at the UPF"}],
   0, y, unit="Mbits", desc=("Admission uses max(admitted, measured): load the orchestrator never admitted still "
                            "uses up capacity. Requires scripts/open5gs/start_orchestrator.sh."))
ts("URLLC: capacity, admitted, measured", [
    {"expr": 'max(slice_capacity_mbps{slice="URLLC"})', "legendFormat": "capacity"},
    {"expr": 'max(slice_allocated_mbps{slice="URLLC"})', "legendFormat": "admitted"},
    {"expr": 'max(slice_measured_mbps{slice="URLLC"})', "legendFormat": "measured at the UPF"}],
   12, y, unit="Mbits", desc="Best-effort demand never spills into URLLC when eMBB is full (ADR-012).")
y += 8
stat("Executed flows that got their rate", 'sum(slice_flows_completed_total{outcome="met"}) / sum(slice_flows_completed_total)', 0, y, w=8,
     desc="met = >= 95 % of the admitted bitrate delivered with <= 2 % loss.")
panels[-1]["fieldConfig"]["defaults"].update({"unit": "percentunit", "decimals": 1})
stat("Demands refused (capacity), last 15 min", 'sum(increase(slice_rejected_total{reason="capacity"}[15m])) or vector(0)', 8, y, w=8)
stat("Demands admitted, last 15 min", 'sum(increase(slice_admitted_total[15m])) or vector(0)', 16, y, w=8)
y += 4
ts("Delivered / admitted rate per slice (last 200 flows)", [
    {"expr": "max by (slice) (slice_flow_delivery_ratio)", "legendFormat": "{{slice}}"}],
   0, y, w=24, unit="percentunit")
y += 8

dash = {
    "uid": "open5gs-slices", "title": "Open5GS — slices, sessions and traffic", "editable": True,
    "schemaVersion": 39, "version": 1, "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"}, "tags": ["open5gs", "slicing"],
    "panels": panels,
}
json.dump(dash, open(sys.argv[1], "w"), indent=2)
print("panels:", len(panels))
