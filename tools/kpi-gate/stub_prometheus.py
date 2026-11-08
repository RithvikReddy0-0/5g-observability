#!/usr/bin/env python3
"""
stub_prometheus.py — a fake Prometheus that answers /api/v1/query with known values.

Its only purpose is to let CI prove the KPI gate actually rejects a bad deployment, without
standing up a 5G core on a GitHub runner. It mirrors the integrity workflow's existing
"verify-only FAILS on a drifted tree" step: a gate is only trustworthy once you have watched
it refuse something.

    stub_prometheus.py <port> [--mode good|bad]
    stub_prometheus.py <port> --stack open5gs [--mode good|<fault>]

    good  every KPI conforms                      -> the gate must exit 0
    bad   two NFs down, sustained 5xx, slice full -> the gate must exit 1

--stack open5gs serves the series of a healthy 3-slice Open5GS stack instead, evaluated per
query rather than looked up (stub_open5gs.py, which also lists the fault modes);
test_open5gs_gate.py runs the Open5GS gate against every one of them.

Standard library only, so it runs anywhere the gate itself runs.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# Values are keyed by the gate's own expressions, so a renamed expression shows up here as
# "no data" rather than silently reusing a stale number.
GOOD = {
    'count(up{job="free5gc"} == 1)': 8,
    "count(up == 0) or vector(0)": 0,
    "sum(free5gc_slice_ues_observed_registered)": 20,
    "count(free5gc_slice_ues_observed_registered > 0)": 2,
    "sum(free5gc_slice_provisioned_subscribers)": 20,
    "count(slice_capacity_mbps > 0)": 2,
    "max(slice_utilization_ratio)": 0.41,
    "_sbi_5xx": 0.0,
    "_sbi_p95": 0.12,
    "_latency_rejections": 0,
}

BAD = dict(GOOD)
BAD.update({
    'count(up{job="free5gc"} == 1)': 6,      # two NFs unscrapeable -> must FAIL
    "_sbi_5xx": 1.7,                         # sustained server errors -> must FAIL
    "max(slice_utilization_ratio)": 0.99,    # advisory -> must WARN, must NOT fail the build
})


def value_for(expr, table):
    if expr == "up":                       # the gate's reachability probe
        return 1
    if "status_code" in expr:
        return table["_sbi_5xx"]
    if "histogram_quantile" in expr:
        return table["_sbi_p95"]
    if "rejected_total" in expr:
        return table["_latency_rejections"]
    return table.get(expr)


def make_handler(table):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            expr = parse_qs(urlparse(self.path).query).get("query", [""])[0]
            value = value_for(expr, table)
            result = [] if value is None else [{"metric": {}, "value": [0, str(value)]}]
            body = json.dumps({
                "status": "success",
                "data": {"resultType": "vector", "result": result},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass                            # keep CI logs about the gate, not the stub

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("port", type=int)
    ap.add_argument("--stack", choices=("free5gc", "open5gs"), default="free5gc")
    ap.add_argument("--mode", default="good")
    args = ap.parse_args()
    if args.stack == "open5gs":
        import stub_open5gs
        if args.mode not in stub_open5gs.SCENARIOS:
            ap.error("--mode for open5gs: one of %s" % ", ".join(stub_open5gs.SCENARIOS))
        handler = stub_open5gs.make_handler(args.mode)
    else:
        if args.mode not in ("good", "bad"):
            ap.error("--mode for free5gc: good or bad")
        handler = make_handler(GOOD if args.mode == "good" else BAD)
    HTTPServer(("127.0.0.1", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
