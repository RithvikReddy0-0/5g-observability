#!/usr/bin/env python3
"""
kpi_collector.py — receives a slice's measurement traffic in its data network and turns it into
KPIs: one-way latency, packet loss, active devices.

Phase 2b, Track 2 (docs/adr/ADR-015-traffic-profiles-and-kpi-scorecard.md). It runs in the
slice's data-network container (o5gs-dn-urllc, o5gs-dn-mmtc). traffic_agent.py in the UE
container sends the packets, from each UE's own PDU-session address, so every packet crosses the
slice's real path: UE -> gNB (GTP-U) -> UPF -> data network.

WHY ONE-WAY LATENCY IS EXACT HERE
Sender and receiver run in containers on one kernel, and Docker does not give containers their
own time namespace, so CLOCK_MONOTONIC is the same clock on both sides (checked: readings taken
UE, DN, UE came out in order). Latency is receive time minus the send time carried in the packet
— the "time from the source UE to the destination" the Phase 2b KPI notes define — with no clock
synchronisation error, and immune to the wall-clock steps this VM suffers (ADR-014).

PACKET FORMATS (big-endian)
  URLLC: device u16 · epoch u8 · seq u32 · send-time u64 (ns)            padded to its size
  mMTC : device u16 · epoch u8 · seq u24 · send-time u32 (ms, mod 2^32)  exactly 10 bytes
The device id is the last four digits of the UE's IMSI. The epoch changes whenever the sender
restarts, so a restarted sequence is not counted as loss.

LOSS. For each (device, epoch) the expected count is last seq - first seq + 1; loss is
1 - received / expected. A packet lost after the last one received is not yet visible; over a
run of thousands of packets that is noise, and it never inflates loss.

Exposes, on METRICS_PORT:
  /metrics      Prometheus: kpi_packets_{received,expected}_total, kpi_owd_ms{stat} over the
                last WINDOW seconds, kpi_owd_recent_ms{stat} over the last RECENT_WINDOW
                seconds, kpi_devices_active, kpi_devices_seen
  /run/start    starts a measurement run (clears the run accumulators)
  /run/stats    JSON for the run so far: counts, loss, latency percentiles, jitter, per device

Environment: SLICE (URLLC|mMTC), MODE (urllc|mmtc), UDP_PORT, METRICS_PORT (9130),
WINDOW (60 s), RECENT_WINDOW (10 s), ACTIVE_WINDOW (seconds a device counts as active after its last packet).
"""

import array
import collections
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLICE = os.environ.get("SLICE", "URLLC")
MODE = os.environ.get("MODE", "urllc")
UDP_PORT = int(os.environ.get("UDP_PORT", "5400"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9130"))
WINDOW = float(os.environ.get("WINDOW", "60"))
# A short window for control rather than display: the slice orchestrator's feedback controller
# (ADR-016) steers admission from it every few seconds, and a 60 s view reacts too late.
RECENT_WINDOW = float(os.environ.get("RECENT_WINDOW", "10"))
ACTIVE_WINDOW = float(os.environ.get("ACTIVE_WINDOW", "10" if MODE == "urllc" else "90"))

URLLC = struct.Struct("!HBIQ")      # 15 bytes
MMTC_HEAD = struct.Struct("!HB")    # + 3-byte seq + 4-byte ms = 10 bytes

lock = threading.Lock()


def socket_drops(port=None, path="/proc/net/udp"):
    """Datagrams the kernel dropped at our socket (its receive buffer was full), from the last
    column of /proc/net/udp. These are lost by the instrument, not by the network."""
    port = UDP_PORT if port is None else port
    try:
        with open(path) as f:
            next(f)
            for line in f:
                cols = line.split()
                if len(cols) > 12 and int(cols[1].split(":")[1], 16) == port:
                    return int(cols[-1])
    except (OSError, ValueError, StopIteration):
        pass
    return None


# Samples kept per run. A run's tally lives until the next /run/start, and between KPI runs nobody
# calls it: at 500 URLLC packets/s, ten idle hours were 19 million samples in Python lists (552 MiB),
# and replacing them under the lock stalled the receive loop for ~9 s at the start of the next run,
# so every device lost the same ~430 packets (2026-11-09, docs/evidence/open5gs-cell-capacity).
# Samples are now flat arrays (8 bytes each, freed at once) and capped; counts stay exact past it.
MAX_RUN_SAMPLES = int(os.environ.get("KPI_MAX_RUN_SAMPLES", "4000000"))   # ~2.2 h of URLLC


class Tally:
    """Counters and latency samples for one span of time (the life of the process, or a run)."""

    def __init__(self, max_samples=None):
        self.started = time.monotonic()
        self.drops_at_start = socket_drops() if max_samples is not None else None
        self.streams = {}            # (device, epoch) -> [first, last, received]
        self.owd = array.array("d")      # one-way delay samples, ms
        self.jitter = array.array("d")   # |owd - previous owd of the same device|, ms
        self.max_samples = max_samples
        self.samples_truncated = False
        self.last_owd = {}
        self.per_device = collections.defaultdict(lambda: {"received": 0, "owd_sum": 0.0})
        self.malformed = 0

    def add(self, dev, epoch, seq, owd_ms):
        s = self.streams.get((dev, epoch))
        if s is None:
            self.streams[(dev, epoch)] = [seq, seq, 1]
        else:
            s[0] = min(s[0], seq)
            s[1] = max(s[1], seq)
            s[2] += 1
        if self.max_samples is None or len(self.owd) < self.max_samples:
            self.owd.append(owd_ms)
            if dev in self.last_owd:
                self.jitter.append(abs(owd_ms - self.last_owd[dev]))
        else:
            self.samples_truncated = True
        self.last_owd[dev] = owd_ms
        d = self.per_device[dev]
        d["received"] += 1
        d["owd_sum"] += owd_ms

    def counts(self):
        received = sum(s[2] for s in self.streams.values())
        expected = sum(s[1] - s[0] + 1 for s in self.streams.values())
        return received, max(expected, received)

    def stats(self):
        received, expected = self.counts()
        per_dev = {}
        for (dev, _), (first, last, rcv) in self.streams.items():
            p = per_dev.setdefault(dev, {"received": 0, "expected": 0})
            p["received"] += rcv
            p["expected"] += last - first + 1
        for dev, p in per_dev.items():
            d = self.per_device[dev]
            p["owd_mean_ms"] = round(d["owd_sum"] / d["received"], 3) if d["received"] else None
        return {
            "slice": SLICE, "mode": MODE,
            "duration_s": round(time.monotonic() - self.started, 1),
            "received": received, "expected": expected,
            "loss_ratio": round(1 - received / expected, 6) if expected else None,
            "devices": len(per_dev),
            "owd_ms": summary(self.owd),
            # how many packets arrived later than each bound: reliability in TS 22.261's sense is
            # delivery within the service's latency bound, not just delivery
            "owd_over_ms": {str(t): sum(1 for x in self.owd if x > t) for t in OVER_MS},
            "jitter_ms_mean": round(sum(self.jitter) / len(self.jitter), 3) if self.jitter else None,
            "malformed": self.malformed,
            # latency figures cover the first MAX_RUN_SAMPLES packets only; counts and loss cover all
            "samples_truncated": self.samples_truncated,
            # dropped by the kernel at this collector's own socket (buffer full): part of the loss
            # above was the instrument's, not the network's
            "receiver_drops": (None if self.drops_at_start is None or socket_drops() is None
                               else socket_drops() - self.drops_at_start),
            "rcvbuf_bytes": rcvbuf_bytes,
            "per_device": {str(k): v for k, v in sorted(per_dev.items())},
        }


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[i], 3)


OVER_MS = (1, 2, 5, 10, 20, 30, 50, 100)   # 10: URLLC slice budget (5QI 82); 30: TS 22.261 Table 7.2.3.2-1


def summary(vals):
    if not vals:
        return {"n": 0}
    v = sorted(vals)
    return {"n": len(v), "mean": round(sum(v) / len(v), 3), "min": round(v[0], 3),
            "p50": percentile(v, 0.50), "p95": percentile(v, 0.95), "p99": percentile(v, 0.99),
            "p999": percentile(v, 0.999), "max": round(v[-1], 3)}


life = Tally()
run = Tally(MAX_RUN_SAMPLES)
recent = collections.deque()         # (monotonic s, owd ms) for the rolling window
last_seen = {}                       # device -> monotonic s


def parse(data, now_ns):
    """-> (device, epoch, seq, one-way delay ms) or None."""
    if MODE == "urllc":
        if len(data) < URLLC.size:
            return None
        dev, epoch, seq, t_ns = URLLC.unpack_from(data)
        return dev, epoch, seq, (now_ns - t_ns) / 1e6
    if len(data) != 10:
        return None
    dev, epoch = MMTC_HEAD.unpack_from(data)
    seq = int.from_bytes(data[3:6], "big")
    t_ms = int.from_bytes(data[6:10], "big")
    owd = ((now_ns // 1_000_000) - t_ms) % (1 << 32)
    return dev, epoch, seq, float(owd)


RCVBUF = 4 << 20
SO_RCVBUFFORCE = getattr(socket, "SO_RCVBUFFORCE", 33)     # Linux; Python does not export it
rcvbuf_bytes = None                  # what the kernel actually granted


def open_socket():
    """The receive socket, with a buffer that rides out a few seconds of stall. A plain SO_RCVBUF is
    capped at net.core.rmem_max (212 992 here): 4 MiB asked, 416 KiB granted, about one second of
    URLLC. SO_RCVBUFFORCE ignores the cap; it needs CAP_NET_ADMIN, which the data-network
    containers have (they set their own routes)."""
    global rcvbuf_bytes
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, RCVBUF)
    except OSError:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
    rcvbuf_bytes = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    sock.bind(("0.0.0.0", UDP_PORT))
    return sock


def receive_loop():
    sock = open_socket()
    print("kpi-collector %s: UDP :%d, receive buffer %d bytes" % (SLICE, UDP_PORT, rcvbuf_bytes), flush=True)
    while True:
        data, _ = sock.recvfrom(2048)
        now_ns = time.monotonic_ns()
        p = parse(data, now_ns)
        with lock:
            if p is None:
                life.malformed += 1
                run.malformed += 1
                continue
            dev, epoch, seq, owd = p
            life.add(dev, epoch, seq, owd)
            run.add(dev, epoch, seq, owd)
            now = now_ns / 1e9
            recent.append((now, owd))
            last_seen[dev] = now
            # The lifetime tally keeps counts, not every sample.
            if len(life.owd) > 10000:
                del life.owd[:5000]
                del life.jitter[:5000]


def render_metrics():
    now = time.monotonic()
    with lock:
        while recent and recent[0][0] < now - WINDOW:
            recent.popleft()
        window = summary([o for _, o in recent])
        fast = summary([o for t, o in recent if t >= now - RECENT_WINDOW])
        received, expected = life.counts()
        active = sum(1 for t in last_seen.values() if t >= now - ACTIVE_WINDOW)
        seen = len(last_seen)
    lab = 'slice="%s"' % SLICE
    out = [
        "# HELP kpi_packets_received_total Measurement packets received from the slice's UEs.",
        "# TYPE kpi_packets_received_total counter",
        "kpi_packets_received_total{%s} %d" % (lab, received),
        "# HELP kpi_packets_expected_total Packets the senders' sequence numbers say were sent.",
        "# TYPE kpi_packets_expected_total counter",
        "kpi_packets_expected_total{%s} %d" % (lab, expected),
        "# HELP kpi_owd_ms One-way delay UE -> data network over the last WINDOW seconds.",
        "# TYPE kpi_owd_ms gauge",
    ]
    for stat in ("mean", "p50", "p95", "p99", "max"):
        if stat in window and window[stat] is not None:
            out.append('kpi_owd_ms{%s,stat="%s"} %s' % (lab, stat, window[stat]))
    out += [
        "# HELP kpi_owd_recent_ms One-way delay UE -> data network over the last RECENT_WINDOW seconds.",
        "# TYPE kpi_owd_recent_ms gauge",
    ]
    for stat in ("mean", "p95", "p99"):
        if stat in fast and fast[stat] is not None:
            out.append('kpi_owd_recent_ms{%s,stat="%s"} %s' % (lab, stat, fast[stat]))
    out += [
        "# HELP kpi_owd_samples One-way delay samples in the window.",
        "# TYPE kpi_owd_samples gauge",
        "kpi_owd_samples{%s} %d" % (lab, window.get("n", 0)),
        "# HELP kpi_devices_active Devices heard from within ACTIVE_WINDOW seconds.",
        "# TYPE kpi_devices_active gauge",
        "kpi_devices_active{%s} %d" % (lab, active),
        "# HELP kpi_devices_seen Devices heard from since the collector started.",
        "# TYPE kpi_devices_seen gauge",
        "kpi_devices_seen{%s} %d" % (lab, seen),
    ]
    drops = socket_drops()
    if drops is not None:
        out += ["# HELP kpi_receiver_drops_total Datagrams the kernel dropped at this collector's socket (buffer full).",
                "# TYPE kpi_receiver_drops_total counter",
                "kpi_receiver_drops_total{%s} %d" % (lab, drops)]
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        global run
        path = self.path.split("?")[0]
        if path == "/metrics":
            self._send(200, render_metrics(), "text/plain; version=0.0.4")
        elif path == "/run/start":
            fresh = Tally(MAX_RUN_SAMPLES)
            with lock:
                old, run = run, fresh
            del old                  # freed outside the lock: the receive loop never waits on it
            self._send(200, json.dumps({"started": True, "slice": SLICE}), "application/json")
        elif path == "/run/stats":
            with lock:
                body = json.dumps(run.stats())
            self._send(200, body, "application/json")
        elif path == "/healthz":
            self._send(200, "ok\n", "text/plain")
        else:
            self._send(404, "not found\n", "text/plain")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("kpi_collector: slice %s, %s packets on udp/%d, metrics on :%d"
          % (SLICE, MODE, UDP_PORT, METRICS_PORT), flush=True)
    threading.Thread(target=receive_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), Handler).serve_forever()
