#!/usr/bin/env python3
"""
traffic_agent.py — what the URLLC and mMTC devices actually send (Phase 2b, Track 2).

Runs in the UE container, started by ran/ue-entrypoint.sh. Every UE of a profiled slice sends
from its OWN PDU-session address, so each packet crosses that slice's gNB and UPF before it
reaches the slice's data network, where kpi_collector.py measures it
(docs/adr/ADR-015-traffic-profiles-and-kpi-scorecard.md).

Profiles (TRAFFIC_PROFILES, one per slice, space separated):
    <slice>:<pool prefix>:<destination>:<udp port>:<bytes>:<interval s>
Default:
    URLLC — 64-byte packets every 20 ms from each of its 10 devices: time-sensitive control
            traffic, small and frequent. One-way latency, its percentiles and jitter are the KPIs.
    mMTC  — 10 bytes every 10 s from each of its 70 devices, then silence until the next report:
            the guide's "IoT sends 10 bytes periodically". Delivery and report latency are KPIs.
eMBB is not here: its bulk transfers are run on demand by tools/kpi/run_kpis.py, because a
permanent bulk flood would saturate the laptop and distort every other measurement.

The interval and size are PROVISIONAL defaults (the KPI notes do not fix them); a sweep overrides
them at run time by writing /tmp/traffic-profile.json, e.g.
    {"mMTC": {"interval": 1}, "URLLC": {"enabled": false}}
which this agent re-reads every 2 s.

Each device starts at a random phase, so 70 IoT devices do not all report in the same instant.
Packet formats are documented in kpi_collector.py. Exposes sent counts on :9122 (/metrics).
"""

import heapq
import json
import os
import random
import re
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT = ("URLLC:10.46.:10.53.0.52:5400:64:0.02 "
           "mMTC:10.47.:10.53.0.53:5401:10:10")
OVERRIDE = "/tmp/traffic-profile.json"
PORT = int(os.environ.get("AGENT_PORT", "9122"))
URLLC = struct.Struct("!HBIQ")
EPOCH = random.randrange(256)          # changes on every restart: a new sequence is not loss

ADDR_RE = re.compile(r"^\d+:\s+(uesimtun(\d{4})\d)\s+inet\s+([\d.]+)/", re.M)

state_lock = threading.Lock()
sent = {}            # slice -> packets sent
errors = {}          # slice -> send errors
devices = {}         # slice -> devices currently sending


def parse_profiles(text):
    out = {}
    for item in text.split():
        name, prefix, dst, port, size, interval = item.split(":")
        out[name] = {"prefix": prefix, "dst": dst, "port": int(port), "bytes": int(size),
                     "interval": float(interval), "enabled": True}
    return out


BASE = parse_profiles(os.environ.get("TRAFFIC_PROFILES", DEFAULT))


def effective():
    """The base profiles with the run-time override applied."""
    prof = {k: dict(v) for k, v in BASE.items()}
    try:
        with open(OVERRIDE) as f:
            ov = json.load(f)
        for name, changes in ov.items():
            if name in prof and isinstance(changes, dict):
                prof[name].update({k: v for k, v in changes.items() if k in ("interval", "bytes", "enabled")})
    except (OSError, ValueError):
        pass
    return prof


def ue_addresses():
    """[(device id, address)] for every UE interface that holds an address."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
    return [(int(tag), ip) for _, tag, ip in ADDR_RE.findall(out)]


def resolve(host):
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


class Device:
    def __init__(self, slice_name, dev, ip):
        self.slice, self.dev, self.ip = slice_name, dev, ip
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((ip, 0))            # the UE's own address -> its PDU session -> its UPF

    def payload(self, size):
        now = time.monotonic_ns()
        if self.slice == "mMTC" or size == 10:
            body = (self.dev.to_bytes(2, "big") + bytes([EPOCH]) + (self.seq % (1 << 24)).to_bytes(3, "big")
                    + ((now // 1_000_000) % (1 << 32)).to_bytes(4, "big"))
            return body[:max(size, 10)]
        head = URLLC.pack(self.dev, EPOCH, self.seq % (1 << 32), now)
        return head + b"\0" * max(0, size - len(head))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def run():
    devs = {}                  # (slice, dev) -> Device
    heap = []                  # (due monotonic s, slice, dev)
    dst_cache = {}
    next_scan = 0.0
    prof = effective()
    while True:
        now = time.monotonic()
        if now >= next_scan:
            prof = effective()
            addrs = ue_addresses()
            wanted = {}
            for name, p in prof.items():
                if not p["enabled"]:
                    continue
                for dev, ip in addrs:
                    if ip.startswith(p["prefix"]):
                        wanted[(name, dev)] = ip
            for key in list(devs):                         # gone, disabled, or re-addressed
                if wanted.get(key) != devs[key].ip:
                    devs.pop(key).close()
            for key, ip in wanted.items():
                if key not in devs:
                    try:
                        devs[key] = Device(key[0], key[1], ip)
                    except OSError:
                        continue
                    interval = prof[key[0]]["interval"]
                    heapq.heappush(heap, (now + random.uniform(0, interval), key[0], key[1]))
            with state_lock:
                for name in prof:
                    devices[name] = sum(1 for k in devs if k[0] == name)
            next_scan = now + 2.0
        if not heap:
            time.sleep(0.5)
            continue
        due, name, dev = heap[0]
        if due > now:
            time.sleep(min(due - now, max(0.0, next_scan - now), 0.5))
            continue
        heapq.heappop(heap)
        d = devs.get((name, dev))
        if d is None:                                      # removed since it was scheduled
            continue
        p = prof[name]
        if p["dst"] not in dst_cache:
            dst_cache[p["dst"]] = resolve(p["dst"])
        dst = dst_cache[p["dst"]]
        try:
            if dst is None:
                raise OSError("cannot resolve %s" % p["dst"])
            d.sock.sendto(d.payload(p["bytes"]), (dst, p["port"]))
            d.seq += 1
            with state_lock:
                sent[name] = sent.get(name, 0) + 1
        except OSError:
            with state_lock:
                errors[name] = errors.get(name, 0) + 1
            dst_cache.pop(p["dst"], None)
        # Next send: on schedule, never in a burst to catch up after a stall.
        heapq.heappush(heap, (max(due + p["interval"], time.monotonic()), name, dev))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        prof = effective()
        with state_lock:
            lines = ["# HELP kpi_agent_sent_total Measurement packets sent by the traffic agent.",
                     "# TYPE kpi_agent_sent_total counter"]
            lines += ['kpi_agent_sent_total{slice="%s"} %d' % (n, sent.get(n, 0)) for n in prof]
            lines += ["# TYPE kpi_agent_send_errors_total counter"]
            lines += ['kpi_agent_send_errors_total{slice="%s"} %d' % (n, errors.get(n, 0)) for n in prof]
            lines += ["# HELP kpi_agent_devices Devices currently sending.", "# TYPE kpi_agent_devices gauge"]
            lines += ['kpi_agent_devices{slice="%s"} %d' % (n, devices.get(n, 0)) for n in prof]
            lines += ["# HELP kpi_agent_interval_seconds Current reporting interval per device.",
                      "# TYPE kpi_agent_interval_seconds gauge"]
            lines += ['kpi_agent_interval_seconds{slice="%s"} %s' % (n, p["interval"] if p["enabled"] else 0)
                      for n, p in prof.items()]
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("traffic_agent: %s (override file %s), metrics on :%d"
          % (", ".join("%s %dB every %gs -> %s:%d" % (n, p["bytes"], p["interval"], p["dst"], p["port"])
                       for n, p in BASE.items()), OVERRIDE, PORT), flush=True)
    threading.Thread(target=run, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
