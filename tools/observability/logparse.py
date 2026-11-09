#!/usr/bin/env python3
"""
logparse.py — per-slice parsing of the Open5GS and UERANSIM text logs (ADR-024).

Open5GS and UERANSIM write human-readable text, never JSON (docs/logging.md, ADR-005). This module
turns one text line into a structured event:

    {"ts": 1794212338.575, "component": "smf", "module": "smf", "level": "warning",
     "event": "pfcp_heartbeat_lost", "slice": "URLLC", "imsi": None, "fields": {"peer": "10.53.0.8"},
     "msg": "No Heartbeat from UPF [10.53.0.8]:8805"}

THE RULES ARE THE CONTRACT
`RULES` is the single list of parsing rules. It is used three ways, so they cannot drift apart:
  * here, by parse_line() — for the tests, and for the correlation engine reading `docker logs`
    directly when Loki is not running;
  * by `--alloy`, which GENERATES the Grafana Alloy pipeline (deployments/open5gs/observability/
    alloy/config.alloy) that labels the same lines on their way into Loki. CI regenerates it and
    fails if the committed file differs;
  * by the tests, on real lines captured from the running stack (tests/fixtures/observability/).
Every pattern is written in the common subset of Python `re` and Go RE2 (named groups `(?P<x>)`,
no look-around, no back-references), because Alloy evaluates them with RE2. Every message text
was taken from a real log line or from the pinned upstream source (Open5GS 157f611a = v2.8.0,
UERANSIM 6bf5a1a9 = v3.3.0); the source file is noted next to each rule.

SLICE IDENTITY, in order of trust
  1. S-NSSAI in the line (AMF: S_NSSAI[SST:2 SD:0x112233])            — exact
  2. DNN / APN in the line (internet, urllc, iot)                      — exact: one DNN per slice
  3. the container the line came from (o5gs-gnb-urllc, o5gs-upf-mmtc)  — exact: one gNB/UPF per slice
  4. a UE address (10.45/10.46/10.47) or a UPF's N4 address (10.53.0.7/8/9)
  5. the IMSI's HOME slice (…001-020 eMBB, …021-030 URLLC, …031-100 mMTC) — the home slice only:
     since ADR-020 eMBB and URLLC devices also hold a session on the other slice, so a line with
     an IMSI and no DNN is attributed to the device's home slice and marked `slice_from: imsi`.
The mapping is read from deployments/slices.env and the UE configs, not hard-coded.

    python3 tools/observability/logparse.py FILE [--container o5gs-smf]   parse a log, print JSON lines
    python3 tools/observability/logparse.py --alloy [OUT]                  print (or write) the Alloy pipeline
    docker logs --timestamps o5gs-smf 2>&1 | python3 tools/observability/logparse.py - --container o5gs-smf

Standard library only.
"""

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SLICES_ENV = os.environ.get("SLICES_ENV", os.path.join(REPO, "deployments", "slices.env"))
RAN_DIR = os.path.join(REPO, "deployments", "open5gs", "ran")
IMSI_PREFIX = "20893"          # PLMN 208/93, the test PLMN every subscriber here uses
FIRST_IMSI = 208930000000001

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# ─────────────────────────── slice identity ───────────────────────────


def read_text(path, **kw):
    with open(path, encoding="utf-8", **kw) as fh:
        return fh.read()


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^\s*([A-Z0-9_]+)=(?:"([^"]*)"|(\S*))', line)
                if m:
                    env[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    except OSError:
        pass
    return env


class SliceMap:
    """Everything that ties a log line to a slice, loaded from the repository's own definitions."""

    DEFAULT = [  # (letter, name, sst, sd, dnn, pool prefix, UPF N4 address)
        ("A", "eMBB", 1, "010203", "internet", "10.45.", "10.53.0.7"),
        ("B", "URLLC", 2, "112233", "urllc", "10.46.", "10.53.0.8"),
        ("C", "mMTC", 3, "334455", "iot", "10.47.", "10.53.0.9"),
    ]

    def __init__(self, env_path=SLICES_ENV, ran_dir=RAN_DIR):
        env = _read_env(env_path)
        self.slices = []
        for letter, name, sst, sd, dnn, pool, upf in self.DEFAULT:
            self.slices.append({
                "letter": letter,
                "name": env.get("SLICE_%s_NAME" % letter, name),
                "sst": int(env.get("SLICE_%s_SST" % letter, sst)),
                "sd": env.get("SLICE_%s_SD" % letter, sd).lower(),
                "dnn": dnn, "pool": pool, "upf": upf,
            })
        # The DNN of each slice comes from the UE configs: their first session's apn + S-NSSAI.
        for path in sorted(os.listdir(ran_dir)) if os.path.isdir(ran_dir) else []:
            if not re.match(r"ue-slice-\w\.yaml$", path):
                continue
            text = read_text(os.path.join(ran_dir, path))
            m = re.search(r'apn:\s*"(\w+)"\s*\n\s*slice:\s*\n\s*sst:\s*0x([0-9a-fA-F]+)\s*\n\s*sd:\s*0x([0-9a-fA-F]+)', text)
            if m:
                for s in self.slices:
                    if s["sst"] == int(m.group(2), 16) and s["sd"] == m.group(3).lower():
                        s["dnn"] = m.group(1)
        # Home slice of each IMSI: O5GS_UES="A=20 B=10 C=70", contiguous from …001 in that order.
        self.imsi_ranges = []
        first = FIRST_IMSI
        for part in env.get("O5GS_UES", "A=20 B=10 C=70").split():
            letter, _, n = part.partition("=")
            s = next((x for x in self.slices if x["letter"] == letter), None)
            if s and n.isdigit():
                self.imsi_ranges.append((first, first + int(n) - 1, s["name"]))
                first += int(n)
        self.by_snssai = {(s["sst"], s["sd"]): s["name"] for s in self.slices}
        self.by_dnn = {s["dnn"]: s["name"] for s in self.slices}
        self.by_upf = {s["upf"]: s["name"] for s in self.slices}
        self.by_container = {}
        for s in self.slices:
            for nf in ("gnb", "upf", "dn"):
                self.by_container["o5gs-%s-%s" % (nf, s["name"].lower())] = s["name"]

    def names(self):
        return [s["name"] for s in self.slices]

    def of_snssai(self, sst, sd):
        try:
            return self.by_snssai.get((int(sst), str(sd).lower().replace("0x", "").zfill(6)))
        except (TypeError, ValueError):
            return None

    def of_dnn(self, dnn):
        return self.by_dnn.get(dnn)

    def of_ip(self, ip):
        if not ip:
            return None
        if ip in self.by_upf:
            return self.by_upf[ip]
        for s in self.slices:
            if ip.startswith(s["pool"]):
                return s["name"]
        return None

    def of_imsi(self, imsi):
        digits = re.sub(r"\D", "", imsi or "")
        if not digits:
            return None
        n = int(digits)
        for lo, hi, name in self.imsi_ranges:
            if lo <= n <= hi:
                return name
        return None

    def of_container(self, container):
        return self.by_container.get((container or "").lstrip("/"))


# ─────────────────────────── the rules ───────────────────────────
#
# (event, components, pattern, level-if-not-in-line). `components` limits a rule to the log sources
# whose text it describes. Patterns are RE2-compatible (see the module docstring).

OPEN5GS_NFS = ("amf", "smf", "upf", "nrf", "scp", "ausf", "udm", "udr", "pcf", "bsf", "nssf")

RULES = [
    # --- registration (AMF; src/amf/gmm-sm.c, nas-path.c, context.c) ---
    ("registration_request", ("amf",), r"\] (?:INFO|WARNING): Registration request", None),
    ("registration_complete", ("amf",), r"\[(?P<imsi>imsi-\d+)\] Registration complete", None),
    ("registration_reject", ("amf",), r"\[(?P<suci>suci-[\d-]+)\] Registration reject \[(?P<cause>\d+)\]", None),
    ("auth_failure", ("amf",), r"Authentication (?:failure|reject)|NAS MAC verification failed|No Security Context", None),
    ("deregistration", ("amf",), r"\[(?P<imsi>imsi-\d+)\] (?:Deregistration request|Implicit De-registered|Explicit De-registered)", None),
    ("service_request", ("amf",), r"\[(?P<imsi>imsi-\d+)\] Service request", None),
    ("gnb_n2_accepted", ("amf",), r"gNB-N2 accepted\[(?P<peer>[\d.]+)\]", None),
    ("gnb_added", ("amf",), r"\[Added\] Number of gNBs is now (?P<count>\d+)", None),
    ("gnb_removed", ("amf",), r"\[Removed\] Number of gNBs is now (?P<count>\d+)", None),
    # --- PDU sessions (AMF gmm-handler.c, SMF npcf-handler.c / gsm-sm.c, UPF context.c) ---
    ("pdu_session_request", ("amf",),
     r"UE SUPI\[(?P<imsi>imsi-\d+)\] DNN\[(?P<dnn>[^\]]*)\] LBO\[\d+\] S_NSSAI\[SST:(?P<sst>\d+) SD:0x(?P<sd>[0-9a-fA-F]+)\]", None),
    ("pdu_session_established", ("smf",), r"UE SUPI\[(?P<imsi>imsi-\d+)\] DNN\[(?P<dnn>[^\]]*)\] IPv4\[(?P<ip>[\d.]*)\]", None),
    ("pdu_session_release", ("smf",), r"\[(?P<imsi>imsi-\d+):(?P<psi>\d+)\] Session Release", None),
    ("upf_session_added", ("upf",), r"\[Added\] Number of UPF-[Ss]essions is now (?P<count>\d+)", None),
    ("upf_session_removed", ("upf",), r"\[Removed\] Number of UPF-[Ss]essions is now (?P<count>\d+)", None),
    ("upf_session", ("upf",), r"UE F-SEID\[[^\]]*\] APN\[(?P<dnn>[^\]]*)\] PDN-Type\[\d+\] IPv4\[(?P<ip>[\d.]*)\]", None),
    ("ip_pool_exhausted", ("upf", "smf"), r"All IP addresses in all subnets are occupied|ue_ip_alloc\(\) failed", None),
    # --- N4 / PFCP (SMF and UPF pfcp-sm.c, lib/pfcp/xact.c, handler.c) ---
    ("pfcp_associated", ("smf", "upf"), r"PFCP associated \[(?P<peer>[\d.]+)\]", None),
    ("pfcp_deassociated", ("smf", "upf"), r"PFCP de-associated \[(?P<peer>[\d.]+)\]", None),
    ("pfcp_heartbeat_lost", ("smf", "upf"), r"No Heartbeat from (?:UPF|SMF) \[(?P<peer>[\d.]+)\]", None),
    ("pfcp_restoration", ("smf", "upf"), r"PFCP restoration|Remote PFCP restarted", None),
    ("pfcp_no_response", ("smf", "upf"), r"No Reponse\. Give up! for step \d+ type \d+ peer \[(?P<peer>[\d.]+)\]", None),
    ("pfcp_retry_failed", ("smf",), r"Retry association with peer failed \[(?P<peer>[\d.]+)\]", None),
    ("pfcp_node_unknown", ("smf", "upf"), r"Cannot find PFCP-Node", None),
    ("no_upf_associated", ("smf",), r"No UPFs are PFCP associated", None),
    # --- NRF and NF lifecycle (src/nrf/nf-sm.c, src/main.c) ---
    ("nf_registered", ("nrf",), r"\[(?P<nf_id>[0-9a-f-]{36})\] NF registered", None),
    ("nf_deregistered", ("nrf",), r"\[(?P<nf_id>[0-9a-f-]{36})\] NF de-registered", None),
    ("nf_terminating", OPEN5GS_NFS, r"SIGTERM received|daemon terminating", None),
    ("nf_started", OPEN5GS_NFS, r"initialize\.\.\.done", None),
    # --- gNB (UERANSIM src/gnb/sctp/task.cpp, ngap/interface.cpp, rls/task.cpp, gtp/task.cpp) ---
    ("ng_setup_ok", ("gnb",), r"NG Setup procedure is successful", None),
    ("ng_setup_failed", ("gnb",), r"NG Setup procedure is failed", None),
    ("n2_connect_failed", ("gnb",), r"Connecting to (?P<peer>[\d.]+):(?P<port>\d+) failed", None),
    ("n2_association_shutdown", ("gnb",), r"SCTP association shutdown", None),
    ("ue_signal_lost", ("gnb",), r"UE\[(?P<ue>\d+)\] signal lost", None),
    ("radio_link_failure", ("gnb", "ue"), r"[Rr]adio link failure", None),
    ("pdu_session_not_found", ("gnb",), r"Uplink data failure, PDU session not found", None),
    ("pdu_session_resource_setup", ("gnb",), r"PDU session resource\(s\) setup for UE\[(?P<ue>\d+)\]", None),
    # --- UE (UERANSIM src/ue/nas/mm/register.cpp, sm/establishment.cpp; per-UE files /tmp/ue-<imsi>.log) ---
    ("registration_complete", ("ue",), r"Initial Registration is successful", None),
    ("registration_reject", ("ue",), r"Initial Registration failed \[(?P<cause>[^\]]*)\]", None),
    ("pdu_session_established", ("ue",), r"PDU Session establishment is successful PSI\[(?P<psi>\d+)\]", None),
    ("pdu_session_reject", ("ue",), r"PDU Session Establishment Reject received \[(?P<cause>[^\]]*)\]", None),
    ("tun_up", ("ue",), r"TUN interface\[(?P<iface>[^,\]]+), (?P<ip>[\d.]+)\] is up", None),
    ("access_barred", ("ue",), r"considering access attempt is barred", None),
    ("cell_selection_failure", ("ue",), r"Cell selection failure", None),
    ("t3580_retransmit", ("ue",), r"Retransmitting PDU Session Establishment Request due to T3580 expiry", None),
    # --- the UE supervisor (deployments/open5gs/ran/ue-entrypoint.sh, the o5gs-ue container's stdout) ---
    ("ue_restart", ("ue-supervisor",),
     r"(?P<slice>eMBB|URLLC|mMTC) (?P<imsi>imsi-\d+) unhealthy \((?P<why>[^)]*)", "warning"),
    ("ue_recovered", ("ue-supervisor",), r"(?P<slice>eMBB|URLLC|mMTC) (?P<imsi>imsi-\d+) back on (?P<iface>\S+)", "info"),
    ("ue_start_timeout", ("ue-supervisor",), r"(?P<slice>eMBB|URLLC|mMTC) (?P<imsi>imsi-\d+) did not come up", "warning"),
    ("ues_up", ("ue-supervisor",), r"(?P<up>\d+)/(?P<total>\d+) UEs up in", "info"),
    # --- our own components' stdout ---
    ("scheduler_apply", ("scheduler",), r"applied mode=(?P<mode>\w+) cell=.*\(ok=(?P<ok>True|False)\)", "info"),
    ("scheduler_step_failed", ("scheduler",), r"step failed", "error"),
    ("controller_action", ("slice-orchestrator",), r"CONTROL (?P<action>cut|raise) +(?P<slice>eMBB|URLLC|mMTC)", "info"),
    ("lifecycle_operation", ("network-orchestrator",),
     r"NETORCH (?P<op>\w+) +(?P<slice>eMBB|URLLC|mMTC) +(?P<result>\w+)", "info"),
]

# Events that, in themselves, say something went wrong (used for the per-slice error panels and as
# anomaly candidates). Every other event is a normal procedure whose RATE may still be anomalous.
FAULT_EVENTS = {
    "registration_reject", "auth_failure", "gnb_removed", "ip_pool_exhausted", "pfcp_deassociated",
    "pfcp_heartbeat_lost", "pfcp_restoration", "pfcp_no_response", "pfcp_retry_failed", "pfcp_node_unknown",
    "no_upf_associated", "nf_deregistered", "nf_terminating", "ng_setup_failed", "n2_connect_failed",
    "n2_association_shutdown", "radio_link_failure", "pdu_session_not_found", "pdu_session_reject",
    "access_barred", "cell_selection_failure", "ue_restart", "ue_start_timeout", "scheduler_step_failed",
}

_COMPILED = [(ev, comps, re.compile(pat), lvl) for ev, comps, pat, lvl in RULES]

# Line shapes. Open5GS: "11/08 14:18:58.575: [smf] WARNING: msg (../src/x.c:101)".
# UERANSIM: "[2026-11-08 14:17:13.117] [nas] [info] msg". Docker --timestamps prefixes RFC 3339 UTC.
DOCKER_TS = re.compile(r"^(?P<dts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<dfrac>\d+))?Z\s")
O5GS_LINE = re.compile(r"^(?P<md>\d{2}/\d{2}) (?P<hms>\d{2}:\d{2}:\d{2})\.(?P<ms>\d{3}): \[(?P<module>[\w-]+)\] "
                       r"(?P<level>[A-Z]+): (?P<msg>.*?)(?: \(\.\./[^)]*\))?$")
URS_LINE = re.compile(r"^\[(?P<date>\d{4}-\d{2}-\d{2}) (?P<hms>\d{2}:\d{2}:\d{2})\.(?P<ms>\d{3})\] "
                      r"\[(?P<module>[\w-]+)\] \[(?P<level>\w+)\] (?P<msg>.*)$")
SUPERVISOR_LINE = re.compile(r"^ue-entrypoint: (?P<hms>\d{2}:\d{2}:\d{2}) (?P<msg>.*)$")
CLOCK_LINE = re.compile(r"^(?P<hms>\d{2}:\d{2}:\d{2})\s+(?P<msg>.*)$")

LEVELS = {"info": "info", "warning": "warning", "warn": "warning", "error": "error", "err": "error",
          "debug": "debug", "trace": "debug", "fatal": "error", "critical": "error"}


def component_of(container):
    """o5gs-gnb-urllc -> gnb, o5gs-amf -> amf, o5gs-ue -> ue-supervisor (its stdout is the supervisor's)."""
    c = (container or "").lstrip("/")
    if c.startswith("o5gs-"):
        c = c[5:]
    if c == "ue":
        return "ue-supervisor"
    for nf in ("gnb", "upf", "dn"):
        if c.startswith(nf + "-"):
            return nf
    return c or None


def _epoch(y, mo, d, hms, frac="0"):
    h, mi, s = (int(x) for x in hms.split(":"))
    return calendar.timegm((int(y), int(mo), int(d), h, mi, s, 0, 0, 0)) + float("0." + (frac or "0"))


def parse_line(line, container=None, component=None, year=None, slices=None, imsi=None, date=None):
    """One raw log line -> event dict, or None when no rule matches and the line has no level.

    container  the Docker container it came from (sets component and, for per-slice NFs, the slice)
    component  overrides the container-derived component ("ue" for a per-UE log file)
    year       for Open5GS lines, which carry no year; defaults to the docker timestamp's, or now
    imsi       for per-UE files (/tmp/ue-<imsi>.log), whose lines do not name their UE
    date       (y, m, d) for lines that carry only a time of day (supervisor, scheduler)
    Container times are taken as UTC (the images set no TZ); a docker --timestamps prefix wins."""
    slices = slices or _default_slices()
    raw = ANSI.sub("", line.rstrip("\r\n")).lstrip("﻿")
    ts = None
    m = DOCKER_TS.match(raw)
    if m:
        y, mo, d = m.group("dts")[:10].split("-")
        ts = _epoch(y, mo, d, m.group("dts")[11:], m.group("dfrac"))
        date = date or (y, mo, d)
        year = year or int(y)
        raw = raw[m.end():]
    comp = component or component_of(container)
    module = level = None
    msg = raw
    m = O5GS_LINE.match(raw)
    if m:
        module, level, msg = m.group("module"), m.group("level").lower(), m.group("msg")
        if ts is None:
            mo, d = m.group("md").split("/")
            ts = _epoch(year or time.gmtime().tm_year, mo, d, m.group("hms"), m.group("ms"))
    else:
        m = URS_LINE.match(raw)
        if m:
            module, level, msg = m.group("module"), m.group("level").lower(), m.group("msg")
            if ts is None:
                y, mo, d = m.group("date").split("-")
                ts = _epoch(y, mo, d, m.group("hms"), m.group("ms"))
        else:
            m = SUPERVISOR_LINE.match(raw) or CLOCK_LINE.match(raw)
            if m:
                msg = m.group("msg")
                if ts is None and date:
                    ts = _epoch(date[0], date[1], date[2], m.group("hms"))
    level = LEVELS.get(level, level)
    event, fields = None, {}
    for ev, comps, rx, lvl in _COMPILED:
        if comp not in comps:
            continue
        hit = rx.search(msg)
        if hit:
            event = ev
            fields = {k: v for k, v in hit.groupdict().items() if v is not None}
            level = level or lvl
            break
    if event is None and level is None:
        return None
    # slice identity, most trusted source first (module docstring)
    sl, src = None, None
    if "sst" in fields and "sd" in fields:
        sl, src = slices.of_snssai(fields["sst"], fields["sd"]), "snssai"
    if not sl and fields.get("dnn"):
        sl, src = slices.of_dnn(fields["dnn"]), "dnn"
    if not sl and fields.get("slice") in slices.names():
        sl, src = fields["slice"], "line"
    if not sl and container:
        sl, src = slices.of_container(container), "container"
    if not sl and (fields.get("ip") or fields.get("peer")):
        sl, src = slices.of_ip(fields.get("ip") or fields.get("peer")), "address"
    ue = fields.get("imsi") or imsi
    if not sl and ue:
        sl, src = slices.of_imsi(ue), "imsi"
    return {"ts": ts, "component": comp, "container": (container or "").lstrip("/") or None, "module": module,
            "level": level, "event": event, "slice": sl, "slice_from": src if sl else None,
            "imsi": ue, "fields": fields, "msg": msg.strip()}


_SLICES = []


def _default_slices():
    if not _SLICES:
        _SLICES.append(SliceMap())
    return _SLICES[0]


def parse_lines(lines, **kw):
    out = []
    for line in lines:
        e = parse_line(line, **kw)
        if e is not None:
            out.append(e)
    return out


# ─────────────────────────── reading logs: docker (no Loki) and Loki ───────────────────────────

# Containers whose logs carry slice-relevant events. o5gs-ue's stdout is the UE supervisor; the UEs'
# own logs are files inside it (read_ue_logs).
CONTAINERS = ["o5gs-amf", "o5gs-smf", "o5gs-nrf", "o5gs-upf-embb", "o5gs-upf-urllc", "o5gs-upf-mmtc",
              "o5gs-gnb-embb", "o5gs-gnb-urllc", "o5gs-gnb-mmtc", "o5gs-ue", "o5gs-scheduler"]
DOCKER = os.environ.get("DOCKER", "docker")


def _rfc3339(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def read_docker_logs(container, since, until=None, slices=None, timeout=20):
    """Events from `docker logs --timestamps --since --until` (read-only). [] if docker is absent."""
    args = [DOCKER, "logs", "--timestamps", "--since", _rfc3339(since)]
    if until:
        args += ["--until", _rfc3339(until)]
    try:
        r = subprocess.run(args + [container], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = (r.stdout + r.stderr).splitlines()
    return [e for e in parse_lines(lines, container=container, slices=slices)
            if e["ts"] is None or since <= e["ts"] <= (until or e["ts"])]


# Only the lines that matter are pulled out of the 100+ per-UE files, so this stays cheap.
UE_LOG_PATTERN = ("Radio link failure|Registration is successful|Registration failed|"
                  "Establishment Reject|access attempt is barred|Cell selection failure")


def read_ue_logs(since, until=None, container="o5gs-ue", slices=None, timeout=20):
    """Radio-link failures and registration outcomes from the UEs' own log files (read-only grep)."""
    script = "grep -H -E '%s' /tmp/ue-[0-9]*.log 2>/dev/null | tail -n 5000" % UE_LOG_PATTERN
    try:
        r = subprocess.run([DOCKER, "exec", container, "sh", "-c", script], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ue_grep(r.stdout.splitlines(), since, until, slices)


def parse_ue_grep(lines, since=None, until=None, slices=None):
    out = []
    for line in lines:
        path, sep, rest = line.partition(":")
        if not sep:
            continue
        m = re.search(r"ue-(\d+)\.log$", path)
        e = parse_line(rest, component="ue", container="o5gs-ue", slices=slices,
                       imsi=("imsi-" + m.group(1)) if m else None)
        if e and (since is None or (e["ts"] or 0) >= since) and (until is None or (e["ts"] or 0) <= until):
            out.append(e)
    return out


def read_all_docker(since, until=None, containers=None, slices=None, ue_files=True):
    events = []
    for c in containers or CONTAINERS:
        events += read_docker_logs(c, since, until, slices)
    if ue_files:
        events += read_ue_logs(since, until, slices=slices)
    events.sort(key=lambda e: e["ts"] or 0)
    return events


def read_loki(base, since, until, selector='{job="o5gs"}', limit=5000, timeout=20, slices=None):
    """Lines from Loki's query_range, re-parsed with the same rules (labels are not trusted blindly:
    a line Alloy failed to label is still classified here)."""
    q = urllib.parse.urlencode({"query": selector, "start": "%d" % (since * 1e9), "end": "%d" % (until * 1e9),
                                "limit": limit, "direction": "forward"})
    with urllib.request.urlopen(base.rstrip("/") + "/loki/api/v1/query_range?" + q, timeout=timeout) as r:
        doc = json.loads(r.read().decode())
    events = []
    for stream in doc.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            e = parse_line(line, container=labels.get("container"),
                           component=labels.get("component") if labels.get("component") == "ue" else None,
                           slices=slices)
            if e is None:
                continue
            if e["ts"] is None:
                e["ts"] = int(ts_ns) / 1e9
            events.append(e)
    events.sort(key=lambda e: e["ts"] or 0)
    return events


def loki_ready(base, timeout=3):
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/ready", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ─────────────────────────── the generated Alloy pipeline ───────────────────────────


def _alloy_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logql_str(s):
    """A LogQL double-quoted (Go) string literal holding s verbatim."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logql_line_filter(pattern):
    # A double-quoted (Go) string: the match-stage selector parser of Alloy v1.20.1 rejects a backtick
    # raw string here as well ("unexpected IDENTIFIER, expecting STRING"), measured on the stack.
    return "|~ " + _logql_str(pattern)


def alloy_config(slices=None):
    """The Grafana Alloy pipeline: Docker containers of this stack -> labels -> Loki.

    Labels (low cardinality, safe for Loki): job, container, component, slice, level, event.
    IMSI and addresses stay in the line (LogQL can filter on them) and are never labels."""
    slices = slices or _default_slices()
    out = []
    w = out.append
    w("// GENERATED by tools/observability/logparse.py --alloy - do not edit by hand.")
    w("// The patterns are logparse.RULES; CI regenerates this file and fails if it differs (ADR-024).")
    w("")
    w('discovery.docker "o5gs" {')
    w('  host = "unix:///var/run/docker.sock"')
    w("  filter {")
    w('    name   = "name"')
    w('    values = ["o5gs-"]')
    w("  }")
    w("}")
    w("")
    w('discovery.relabel "o5gs" {')
    w("  targets = discovery.docker.o5gs.targets")
    w("  rule {")
    w('    source_labels = ["__meta_docker_container_name"]')
    w('    regex         = "/(.*)"')
    w('    target_label  = "container"')
    w("  }")
    w("  rule {")
    w('    source_labels = ["__meta_docker_container_name"]')
    w('    regex         = "/o5gs-(gnb|upf|dn)-.*"')
    w('    target_label  = "component"')
    w('    replacement   = "$1"')
    w("  }")
    w("  rule {")
    w('    source_labels = ["__meta_docker_container_name"]')
    w('    regex         = "/o5gs-ue"')
    w('    target_label  = "component"')
    w('    replacement   = "ue-supervisor"')
    w("  }")
    w("  rule {")
    w('    source_labels = ["__meta_docker_container_name", "component"]')
    w('    regex         = "/o5gs-([a-z]+);"')
    w('    target_label  = "component"')
    w('    replacement   = "$1"')
    w("  }")
    for s in slices.slices:
        w("  rule {")
        w('    source_labels = ["__meta_docker_container_name"]')
        w('    regex         = %s' % _alloy_str("/o5gs-(gnb|upf|dn)-%s" % s["name"].lower()))
        w('    target_label  = "slice"')
        w('    replacement   = %s' % _alloy_str(s["name"]))
        w("  }")
    w("}")
    w("")
    w('loki.source.docker "o5gs" {')
    w('  host          = "unix:///var/run/docker.sock"')
    w("  targets       = discovery.docker.o5gs.targets")
    w("  relabel_rules = discovery.relabel.o5gs.rules")
    w('  labels        = {"job" = "o5gs"}')
    w("  forward_to    = [loki.process.o5gs.receiver]")
    w("}")
    w("")
    w('loki.process "o5gs" {')
    w("  forward_to = [loki.write.local.receiver]")
    w("")
    w("  // On first start Alloy reads each container's whole history (weeks); Loki rejects anything older")
    w("  // than its window with a 400 per batch. Drop locally what retention (loki.yaml, 72h) would delete.")
    w("  stage.drop {")
    w('    older_than          = "72h"')
    w('    drop_counter_reason = "older_than_retention"')
    w("  }")
    w("")
    w("  // Open5GS colours its output even without a TTY.")
    w("  stage.replace {")
    w('    expression = "(\\\\x1b\\\\[[0-9;]*m)"')
    w('    replace    = ""')
    w("  }")
    w("  // Level: Open5GS \"[smf] WARNING:\", UERANSIM \"[nas] [warning]\".")
    w("  stage.regex {")
    w('    expression = %s' % _alloy_str(r"^\d{2}/\d{2} [\d:.]+: \[(?P<module>[\w-]+)\] (?P<level>[A-Z]+): "))
    w("  }")
    w("  stage.regex {")
    w('    expression = %s' % _alloy_str(r"^\[[\d-]+ [\d:.]+\] \[(?P<module>[\w-]+)\] \[(?P<level>\w+)\] "))
    w("  }")
    w("  stage.template {")
    w('    source   = "level"')
    w('    template = "{{ ToLower .Value }}"')
    w("  }")
    w("  stage.labels {")
    w('    values = { level = "" }')
    w("  }")
    w("  // Events. Applied in reverse so that, as in parse_line(), the FIRST matching rule wins.")
    for ev, comps, pat, lvl in reversed(RULES):
        comp_re = "|".join(re.escape(c) for c in comps)
        # LogQL reads a matcher value as a Go string (double quotes only: a backtick is a syntax error
        # in Alloy's match stage), where re.escape's "\-" is an invalid escape and Alloy refuses the
        # whole pipeline ("invalid char escape"). So every regex is escaped once more for Go.
        sel = '{component=~%s} %s' % (_logql_str(comp_re), _logql_line_filter(pat))
        w("  stage.match {")
        w("    selector = %s" % _alloy_str(sel))
        w("    stage.static_labels {")
        w('      values = { event = %s }' % _alloy_str(ev))
        w("    }")
        w("  }")
    w("  // Slice from the line itself (S-NSSAI, DNN/APN, supervisor's slice word), for shared NFs.")
    for s in slices.slices:
        alts = [r"S_NSSAI\[SST:%d SD:0x%s\]" % (s["sst"], s["sd"]), r"DNN\[%s\]" % s["dnn"],
                r"APN\[%s\]" % s["dnn"], r"ue-entrypoint: [\d:]+ %s imsi-" % s["name"],
                r"IPv4\[%s" % re.escape(s["pool"]), r"peer \[%s\]" % re.escape(s["upf"]),
                r"(?:associated|Heartbeat from UPF) \[%s\]" % re.escape(s["upf"])]
        w("  stage.match {")
        w("    selector = %s" % _alloy_str('{job="o5gs", slice=""} %s' % _logql_line_filter("|".join(alts))))
        w("    stage.static_labels {")
        w('      values = { slice = %s }' % _alloy_str(s["name"]))
        w("    }")
        w("  }")
    w("}")
    w("")
    w('loki.write "local" {')
    w("  endpoint {")
    w('    url = "http://10.53.0.43:3100/loki/api/v1/push"')
    w("  }")
    w("}")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", nargs="?", help="log file, or - for stdin")
    ap.add_argument("--container", help="container the lines came from (sets component and slice)")
    ap.add_argument("--component", help="override the component, e.g. ue for /tmp/ue-*.log")
    ap.add_argument("--alloy", action="store_true", help="print the generated Alloy pipeline")
    ap.add_argument("--only-events", action="store_true", help="drop lines that matched no rule")
    a = ap.parse_args(argv)
    if a.alloy:
        if a.file:          # write the file directly: LF and UTF-8 whatever the platform
            with open(a.file, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(alloy_config())
        else:
            sys.stdout.write(alloy_config())
        return 0
    if not a.file:
        ap.error("a log file (or -) is required")
    fh = sys.stdin if a.file == "-" else open(a.file, encoding="utf-8", errors="replace")
    imsi = None
    m = re.search(r"ue-(\d+)\.log$", a.file or "")
    if m:
        imsi = "imsi-" + m.group(1)
    for line in fh:
        e = parse_line(line, container=a.container, component=a.component, imsi=imsi)
        if e and (e["event"] or not a.only_events):
            print(json.dumps(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
