#!/usr/bin/env python3
"""
upf_slice_exporter.py — measured, per-slice user-plane traffic from the Open5GS UPF.

WHY THIS EXISTS
Open5GS's UPF exports traffic volume (fivegs_ep_n3_gtp_{in,out}datavolumeqosleveln3upf), but
the only label is `qfi`. The default QoS flow of every PDU session is QFI 1 regardless of
slice, so those counters mix eMBB and URLLC traffic together. They cannot answer "how much
is each slice carrying" — the same R-02 gap free5GC had, narrower but still real.

This deployment gives each slice its own DNN, address pool and UPF TUN device
(deployments/open5gs/config/upf.yaml). Every packet on `ogstun` is eMBB and every packet on
`ogstun2` is URLLC, by construction. Reading the kernel's counters for those devices is
therefore an exact per-slice measurement, not an estimate.

It runs inside the UPF container, started by upf-entrypoint.sh, because those devices only
exist in the UPF's network namespace. It reads /sys and never touches the UPF process.

Direction, as seen by the UPF's TUN device:
    rx on ogstunN  = packets the UPF WROTE into the device (decapsulated from the UE)  = UPLINK
    tx on ogstunN  = packets the kernel handed the UPF to encapsulate towards the UE    = DOWNLINK
For a TUN device, what the userspace program writes is what the kernel "receives". This is
counter-intuitive, and the first version of this exporter had it backwards. It was settled
by measurement, not by reasoning: 20 uplink ICMP packets of 1028 bytes moved rx_bytes by
exactly 20560 (docs/open5gs.md, "Verification").
"""

import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9120"))

# device:sst:sd:name — set in docker-compose.yaml from the same slice plan as the configs.
SLICES = [tuple(s.split(":")) for s in
          os.environ.get("SLICE_DEVICES",
                         "ogstun:1:010203:eMBB ogstun2:2:112233:URLLC").split()]


def read(dev, stat):
    try:
        with open("/sys/class/net/%s/statistics/%s" % (dev, stat)) as f:
            return int(f.read())
    except OSError:
        return None


def render():
    out = []
    emit = out.append

    emit("# HELP upf_slice_up 1 if the slice's UPF TUN device exists and is readable.")
    emit("# TYPE upf_slice_up gauge")
    for dev, sst, sd, name in SLICES:
        emit('upf_slice_up{sst="%s",sd="%s",slice="%s",device="%s"} %d'
             % (sst, sd, name, dev, 0 if read(dev, "rx_bytes") is None else 1))

    series = [
        ("upf_slice_uplink_bytes_total", "rx_bytes", "Bytes UEs on this slice sent out through the UPF."),
        ("upf_slice_downlink_bytes_total", "tx_bytes", "Bytes delivered back to UEs on this slice through the UPF."),
        ("upf_slice_uplink_packets_total", "rx_packets", "Uplink packets through the UPF on this slice."),
        ("upf_slice_downlink_packets_total", "tx_packets", "Downlink packets through the UPF on this slice."),
        ("upf_slice_dropped_packets_total", None, "Packets dropped on this slice's UPF device, both directions."),
    ]
    for metric, stat, help_text in series:
        emit("# HELP %s %s" % (metric, help_text))
        emit("# TYPE %s counter" % metric)
        for dev, sst, sd, name in SLICES:
            if stat is None:
                a, b = read(dev, "rx_dropped"), read(dev, "tx_dropped")
                value = None if a is None or b is None else a + b
            else:
                value = read(dev, stat)
            if value is not None:
                emit('%s{sst="%s",sd="%s",slice="%s",device="%s"} %d'
                     % (metric, sst, sd, name, dev, value))

    emit("# HELP upf_slice_exporter_scrape_timestamp_seconds When this scrape was produced.")
    emit("# TYPE upf_slice_exporter_scrape_timestamp_seconds gauge")
    emit("upf_slice_exporter_scrape_timestamp_seconds %.3f" % time.time())
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("upf_slice_exporter on :%d for %s" % (PORT, ", ".join("%s=%s" % (d, n) for d, _, _, n in SLICES)),
          flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
