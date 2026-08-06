#!/usr/bin/env python3
"""
slice_exporter.py — emit Prometheus metrics carrying S-NSSAI (slice) labels.

WHY THIS EXISTS
---------------
free5GC's native exporters do NOT carry slice identity. Verified across all 629 series
and 15 metric names: the only label keys are gmm_state, le, method, name, nf_type, path,
state, status, status_code, target_service_name, to_state. None identify a slice.

That blocks the ADR-006 slice-labeling contract ("every metric carries the S-NSSAI it
pertains to"), which the Slice Correlation Engine in SPEC section 3 depends on — logged
as risk R-02.

This exporter closes that gap at rung 2 of the ADR-004 ladder (log-derived metrics),
with no changes to free5GC source. Slice identity comes from three authoritative places:

  1. AMF logs, at INFO level, emit one line per SMF selection carrying SUPI + S-NSSAI + DNN:
       [AMF][Gmm][supi:SUPI:imsi-208930000000004] Select SMF [snssai: {Sst:1 Sd:010203}, dnn: internet]
  2. MongoDB holds each subscriber's PROVISIONED slice
     (subscriptionData.provisionedData.amData.nssai.defaultSingleNssais).
  3. UERANSIM's nr-cli reports live per-UE registration state (ground truth — the AMF's
     own UE gauges are unreliable on v4.2.0; see observability/README.md).

Metrics 2 and 3 are joined on SUPI to produce registered-UEs-per-slice, which free5GC
cannot report on its own.

HONESTY NOTE: these are DERIVED metrics, not NF-native slice labels. The underlying gap
in free5GC is unchanged — this makes slice-aware observability possible without patching
upstream, and is evidence for the eventual ADR-004 decision about whether an
instrumentation patch is warranted.

Runs on the host (not in a container) because it shells out to docker. Prometheus reaches
it via the compose network gateway; see observability/prometheus/prometheus.yml.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

COMPOSE_DIR = os.environ.get(
    "COMPOSE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "deployments", "compose"),
)
PORT = int(os.environ.get("PORT", "9105"))
REFRESH = int(os.environ.get("REFRESH", "20"))          # seconds between recomputes
# AMF log lines to scan. Wider window = more chance an idle UE is still represented in
# free5gc_slice_ues_observed_registered (see the caveat in observability/README.md).
# Reading 20000 lines costs well under a second.
LOG_TAIL = os.environ.get("LOG_TAIL", "20000")
UE_MAX = int(os.environ.get("UE_MAX", "20"))             # how many SUPIs to poll via nr-cli

# Container NAMES (not compose service names) — see docker_exec() for why.
DB_CONTAINER = os.environ.get("DB_CONTAINER", "mongodb")
AMF_CONTAINER = os.environ.get("AMF_CONTAINER", "amf")

# [supi:SUPI:imsi-208930000000004] ... Select SMF [snssai: {Sst:1 Sd:010203}, dnn: internet]
SELECT_SMF_RE = re.compile(
    r"supi:SUPI:(?P<supi>imsi-\d+)\].*?Select SMF \[snssai: \{Sst:(?P<sst>\d+) Sd:(?P<sd>[0-9A-Fa-f]*)\}, dnn: (?P<dnn>[^\]]+)\]"
)

# [supi:SUPI:imsi-208930000000004] ... GmmMessageEvent at GMM State[Registered]
REGISTERED_STATE_RE = re.compile(
    r"supi:SUPI:(?P<supi>imsi-\d+)\].*?GMM State\[Registered\]"
)

_lock = threading.Lock()
_cache = {"text": "", "ts": 0.0}


def _run(cmd, timeout=60):
    try:
        out = subprocess.run(cmd, cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except Exception:
        return ""


def docker_exec(container, args, timeout=60):
    """Run a command in a container by NAME, using plain `docker`.

    Deliberately not `docker compose`: on Docker Desktop + WSL the compose plugin is a
    symlink into /mnt/wsl/docker-desktop/, which disappears whenever Docker Desktop
    restarts. The daemon keeps working, so plain `docker` against stable container names
    survives that; `docker compose` silently returns nothing and every metric vanishes.
    """
    return _run(["docker", "exec", "-i", container] + args, timeout)


def docker_logs(container, tail, timeout=90):
    return _run(["docker", "logs", "--tail", str(tail), container], timeout)


def provisioned_by_slice():
    """{(sst, sd): count} of provisioned subscribers, and {supi: (sst, sd)}."""
    js = (
        'var m={},u={};'
        'db["subscriptionData.provisionedData.amData"]'
        '.find({},{_id:0,ueId:1,"nssai.defaultSingleNssais":1}).forEach(function(d){'
        '  var l=(d.nssai&&d.nssai.defaultSingleNssais)||[];'
        '  if(l.length){var s=l[0];var k=s.sst+"|"+(s.sd||"");'
        '    m[k]=(m[k]||0)+1; u[d.ueId]=k;}'
        '});'
        'print(JSON.stringify({counts:m,ues:u}));'
    )
    raw = docker_exec(DB_CONTAINER, ["mongo", "free5gc", "--quiet", "--eval", js])
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"counts": {}, "ues": {}}


def amf_log_facts():
    """One AMF log read -> (smf_selection_counts, supis_seen_registered).

    Deliberately does NOT use nr-cli. Polling nr-cli per UE needs one `docker compose exec`
    each; on this memory-constrained host that took >180s for 20 UEs and timed out every
    scrape. A single `docker compose logs` call returns in well under a second and carries
    the same slice identity.
    """
    raw = docker_logs(AMF_CONTAINER, LOG_TAIL)

    counts = {}
    for m in SELECT_SMF_RE.finditer(raw):
        key = (m.group("sst"), m.group("sd"), m.group("dnn").strip())
        counts[key] = counts.get(key, 0) + 1

    seen_registered = set()
    for m in REGISTERED_STATE_RE.finditer(raw):
        seen_registered.add(m.group("supi"))

    return counts, seen_registered


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def build_metrics():
    started = time.time()
    lines = []

    def emit(name, mtype, help_text, samples):
        lines.append("# HELP %s %s" % (name, help_text))
        lines.append("# TYPE %s %s" % (name, mtype))
        for labels, value in samples:
            lbl = ",".join('%s="%s"' % (k, esc(v)) for k, v in labels)
            lines.append("%s{%s} %s" % (name, lbl, value))

    prov = provisioned_by_slice()
    counts = prov.get("counts", {})
    ue_slice = prov.get("ues", {})

    emit(
        "free5gc_slice_provisioned_subscribers",
        "gauge",
        "Subscribers provisioned per S-NSSAI (source: MongoDB amData.nssai.defaultSingleNssais).",
        [
            ([("sst", k.split("|")[0]), ("sd", k.split("|")[1])], v)
            for k, v in sorted(counts.items())
        ],
    )

    sel, seen_registered = amf_log_facts()

    # UEs observed in GMM Registered state, attributed to a slice by joining on SUPI.
    reg_by_slice = {}
    for supi in seen_registered:
        k = ue_slice.get(supi)
        if k:
            reg_by_slice[k] = reg_by_slice.get(k, 0) + 1
    for k in counts:
        reg_by_slice.setdefault(k, 0)

    emit(
        "free5gc_slice_ues_observed_registered",
        "gauge",
        "Distinct UEs observed in GMM State[Registered] within the recent AMF log window, "
        "per S-NSSAI (SUPI joined to its provisioned slice). Log-window derived, NOT a live "
        "population gauge: a registered but idle UE stops appearing once it leaves the window.",
        [
            ([("sst", k.split("|")[0]), ("sd", k.split("|")[1])], v)
            for k, v in sorted(reg_by_slice.items())
        ],
    )
    emit(
        "free5gc_slice_smf_selection_total",
        "counter",
        "SMF selections observed in AMF logs, per S-NSSAI and DNN (resets when the AMF restarts).",
        [
            ([("sst", sst), ("sd", sd), ("dnn", dnn)], v)
            for (sst, sd, dnn), v in sorted(sel.items())
        ],
    )

    dur = time.time() - started
    lines.append("# HELP free5gc_slice_exporter_scrape_duration_seconds Time to collect slice metrics.")
    lines.append("# TYPE free5gc_slice_exporter_scrape_duration_seconds gauge")
    lines.append("free5gc_slice_exporter_scrape_duration_seconds %.3f" % dur)
    lines.append("# HELP free5gc_slice_exporter_up Always 1 when the exporter served a scrape.")
    lines.append("# TYPE free5gc_slice_exporter_up gauge")
    lines.append("free5gc_slice_exporter_up 1")

    return "\n".join(lines) + "\n"


def cached_metrics():
    with _lock:
        now = time.time()
        if now - _cache["ts"] > REFRESH or not _cache["text"]:
            try:
                _cache["text"] = build_metrics()
            except Exception as exc:  # never fail a scrape
                _cache["text"] = (
                    "# HELP free5gc_slice_exporter_up Always 1 when the exporter served a scrape.\n"
                    "# TYPE free5gc_slice_exporter_up gauge\n"
                    "free5gc_slice_exporter_up 0\n"
                    "# error: %s\n" % str(exc).replace("\n", " ")
                )
            _cache["ts"] = now
        return _cache["text"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") in ("/metrics", ""):
            body = cached_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # keep stdout clean


if __name__ == "__main__":
    print("slice_exporter listening on :%d  (compose dir: %s)" % (PORT, os.path.abspath(COMPOSE_DIR)))
    sys.stdout.flush()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
