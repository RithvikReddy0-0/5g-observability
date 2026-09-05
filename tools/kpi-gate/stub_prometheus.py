#!/usr/bin/env python3
"""
stub_prometheus.py — a fake Prometheus that answers /api/v1/query with known values.

Its only purpose is to let CI prove the KPI gate actually rejects a bad deployment, without
standing up a 5G core on a GitHub runner. It mirrors the integrity workflow's existing
"verify-only FAILS on a drifted tree" step: a gate is only trustworthy once you have watched
it refuse something.

    stub_prometheus.py <port> [--mode good|bad]

    good  every KPI conforms                      -> the gate must exit 0
    bad   two NFs down, sustained 5xx, slice full -> the gate must exit 1

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
    "count(up == 0)": 0,
    "sum(slice_ues_observed_registered)": 20,
    "count(slice_ues_observed_registered > 0)": 2,
    "sum(slice_provisioned_subscribers)": 20,
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
    ap.add_argument("--mode", choices=("good", "bad"), default="good")
    args = ap.parse_args()
    table = GOOD if args.mode == "good" else BAD
    HTTPServer(("127.0.0.1", args.port), make_handler(table)).serve_forever()


if __name__ == "__main__":
    main()
