#!/usr/bin/env python3
"""
ue_probe_exporter.py — per-slice reachability and latency, measured from the UEs themselves.

WHY THIS EXISTS
Stopping every UE abruptly left the core's metrics unchanged: the AMF still reported 20 UEs
on the RAN and the UPF still held 20 sessions, because nothing told the core the UEs had
gone. A KPI gate built only on core-side metrics passed with zero working UEs (measured, not
assumed — see docs/open5gs.md). Liveness has to be observed from the UE side.

Every INTERVAL seconds this pings the slice's UPF gateway from each UE's own PDU-session
interface, so every probe crosses the real path: UE TUN -> gNB (GTP-U) -> UPF -> slice TUN.

It runs inside the UE container, started by ue-entrypoint.sh, because the uesimtunN
interfaces only exist in that network namespace. It never touches the UE processes.

Exported per slice:
    ue_probe_interfaces{slice}        UE interfaces holding an address from the slice's pool
    ue_probe_reachable{slice}         of those, how many answered the last round
    ue_probe_rtt_ms{slice,stat}       min / avg / max round-trip time of the last round
    ue_probe_rounds_total, ue_probe_round_duration_seconds
"""

import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "9121"))
INTERVAL = float(os.environ.get("INTERVAL", "5"))
TIMEOUT_S = int(os.environ.get("PROBE_TIMEOUT", "1"))

# prefix:gateway:name — the pools from deployments/open5gs/config/smf.yaml
SLICES = [tuple(s.split(":")) for s in
          os.environ.get("SLICE_POOLS", "10.45.:10.45.0.1:eMBB 10.46.:10.46.0.1:URLLC").split()]

ADDR_RE = re.compile(r"^\d+:\s+(uesimtun\d+)\s+inet\s+([\d.]+)/", re.M)
# busybox prints "min/avg/max = a/b/c ms"; iputils prints "min/avg/max/mdev = a/b/c/d ms".
# The first version only matched busybox, and reported 0 reachable UEs while its probes were
# visibly arriving at the UPF.
RTT_RE = re.compile(r"= [\d.]+/([\d.]+)/[\d.]+(?:/[\d.]+)? ms")

state = {"slices": {}, "rounds": 0, "duration": 0.0}
lock = threading.Lock()


def interfaces():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
    return ADDR_RE.findall(out)


def probe_round():
    started = time.time()
    ifaces = interfaces()
    procs = []
    for prefix, gw, name in SLICES:
        for dev, ip in ifaces:
            if ip.startswith(prefix):
                p = subprocess.Popen(["ping", "-I", dev, "-c", "1", "-W", str(TIMEOUT_S), gw],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                procs.append((name, p))

    result = {name: {"interfaces": 0, "reachable": 0, "rtts": []} for _, _, name in SLICES}
    for name, p in procs:
        try:
            out, _ = p.communicate(timeout=TIMEOUT_S + 2)
        except subprocess.TimeoutExpired:
            p.kill()
            out = ""
        r = result[name]
        r["interfaces"] += 1
        m = RTT_RE.search(out or "")
        if p.returncode == 0 and m:
            r["reachable"] += 1
            r["rtts"].append(float(m.group(1)))

    with lock:
        state["slices"] = result
        state["rounds"] += 1
        state["duration"] = time.time() - started


def loop():
    while True:
        t0 = time.time()
        try:
            probe_round()
        except Exception as e:                        # never let one bad round kill the exporter
            print("probe round failed: %s" % e, flush=True)
        time.sleep(max(0.0, INTERVAL - (time.time() - t0)))


def render():
    with lock:
        s = dict(state)
    lines = [
        "# HELP ue_probe_interfaces UE PDU-session interfaces holding an address from the slice's pool.",
        "# TYPE ue_probe_interfaces gauge",
        "# HELP ue_probe_reachable UE interfaces whose probe through the UPF succeeded in the last round.",
        "# TYPE ue_probe_reachable gauge",
        "# HELP ue_probe_rtt_ms Round-trip time UE -> gNB -> UPF gateway in the last round.",
        "# TYPE ue_probe_rtt_ms gauge",
    ]
    for _, _, name in SLICES:
        r = s["slices"].get(name, {"interfaces": 0, "reachable": 0, "rtts": []})
        lines.append('ue_probe_interfaces{slice="%s"} %d' % (name, r["interfaces"]))
        lines.append('ue_probe_reachable{slice="%s"} %d' % (name, r["reachable"]))
        if r["rtts"]:
            rt = r["rtts"]
            for stat, v in (("min", min(rt)), ("avg", sum(rt) / len(rt)), ("max", max(rt))):
                lines.append('ue_probe_rtt_ms{slice="%s",stat="%s"} %.3f' % (name, stat, v))
    lines += [
        "# HELP ue_probe_rounds_total Probe rounds completed.",
        "# TYPE ue_probe_rounds_total counter",
        "ue_probe_rounds_total %d" % s["rounds"],
        "# HELP ue_probe_round_duration_seconds Wall time of the last probe round.",
        "# TYPE ue_probe_round_duration_seconds gauge",
        "ue_probe_round_duration_seconds %.3f" % s["duration"],
    ]
    return "\n".join(lines) + "\n"


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
    print("ue_probe_exporter on :%d, every %.0fs, slices: %s"
          % (PORT, INTERVAL, ", ".join("%s via %s" % (n, g) for _, g, n in SLICES)), flush=True)
    threading.Thread(target=loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
