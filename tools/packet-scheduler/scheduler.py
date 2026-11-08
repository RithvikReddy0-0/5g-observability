#!/usr/bin/env python3
"""
scheduler.py — the packet scheduler: classifies every radio packet by its headers and schedules the
slices on a shared cell (ADR-019).

WHY IT EXISTS
The slice orchestrator (ADR-016) decides per FLOW: which slice, whether it fits. Once a flow is in,
nothing decided which packet goes first: the Open5GS UPF does not schedule by 5QI or ARP, and
UERANSIM has no MAC scheduler. In a real network the gNB's MAC scheduler does that job every slot,
from each packet's QoS flow. This is the equivalent for this testbed, at the IP layer.

WHERE IT SCHEDULES
UERANSIM's "radio" is UDP between the UE container and the gNBs (RLS, port 4997). Every UE's radio
traffic, of every slice, crosses one link: the UE container's network interface. That link is the
cell. Downlink is scheduled on the host side of it (the veth peer, before packets enter the UE
container); uplink on the container side (its eth0). The cell gets a capacity, SCHED_CELL_DL_MBPS /
SCHED_CELL_UL_MBPS in slices.env, so there is a queue to schedule — on an unshaped link the
bottleneck is wherever the CPU happens to run out, and nothing can be scheduled there.

HOW IT CLASSIFIES — by reading the packet, not by being told
Every RLS message has a fixed layout (UERANSIM v3.3.0, src/lib/rls/rls_pdu.cpp):
    byte 0      0x03            bytes 1-3   version 3.3.0      byte 4   message type
    bytes 5-12  sender id       then, for a PDU transmission:
    byte 13     PDU type (1 RRC signalling, 2 user data)       bytes 14-17  PDU id
    bytes 18-21 PDU session id  bytes 22-25 length             byte 26 on   the UE's own IP packet
So u32 filters on fixed offsets read: whether a packet is signalling (heartbeats, acks, RRC/NAS) or
user data, and for user data the INNER IP address of the UE, whose pool says the slice
(10.45 eMBB, 10.46 URLLC, 10.47 mMTC) and whose exact address says the subscriber, and with it the
ARP tier. Offsets are counted from the outer IP header (20 bytes, no options — Docker never adds
any) plus UDP (8): the RLS message starts at 28.

HOW IT SCHEDULES — HTB, one class per slice (and per ARP tier), on the cell
    signalling              prio 0   so radio links and registrations survive any data load
    URLLC critical (ARP 1)  prio 1   short FIFO
    URLLC standard (ARP 2)  prio 2   short FIFO
    eMBB                    prio 3   fq_codel (fair per UE, short queues for small flows)
    mMTC                    prio 4   fq_codel
    anything else           prio 6   (scrapes, unknown) never starved: it has a guaranteed rate
Each class has a guaranteed rate (SLICE_<X>_SCHED_SHARE, % of the cell) and may borrow up to the
whole cell; HTB serves classes within their guarantee in priority order, and lends what is idle to
the highest-priority class that wants more. Shares and priorities are the knobs to tune.

MODES (for comparing like with like, at the same cell capacity)
    sliced   the tree above
    fifo     one drop-tail FIFO at the cell capacity: what a shared cell does without a scheduler
    off      no shaping at all (the stack as it was before ADR-019)

It runs as a privileged container on the host's network and PID namespaces
(scripts/open5gs/start_scheduler.sh) because it changes queues on the host and inside the UE
container. It re-applies on its own when the UE container is recreated or a critical UE's address
changes. Metrics and API on :9114 — GET /metrics, GET /state, POST /config.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SLICES_ENV = os.environ.get("SLICES_ENV", os.path.join(REPO, "deployments", "slices.env"))
COMPOSE = os.environ.get("COMPOSE", os.path.join(REPO, "deployments", "open5gs", "docker-compose.yaml"))
PORT = int(os.environ.get("PORT", "9114"))
INTERVAL = float(os.environ.get("SCHED_INTERVAL", "5"))
FIRST_IMSI = int(os.environ.get("FIRST_IMSI", "208930000000001"))   # provision_subscribers.py
FIFO_LIMIT = int(os.environ.get("SCHED_FIFO_LIMIT", "1000"))         # packets: Linux's usual txqueuelen

# ─────────────────────────── the packet format ───────────────────────────

RLS_PORT = 4997                 # cons::RadioLinkPort
RLS = 20 + 8                    # outer IPv4 header (no options) + UDP header
OFF_PROTO = 9                   # outer IP protocol
OFF_SPORT, OFF_DPORT = 20, 22   # outer UDP ports
OFF_MSG_TYPE = RLS + 4          # 32
OFF_PDU_TYPE = RLS + 13         # 41
OFF_PSI = RLS + 18              # 46: PDU session id (DATA) or RRC channel (RRC)
OFF_INNER = RLS + 26            # 54: the UE's own IP packet
OFF_INNER_SRC = OFF_INNER + 12  # 66
OFF_INNER_DST = OFF_INNER + 16  # 70
HEARTBEAT, HEARTBEAT_ACK, PDU_TRANSMISSION, PDU_TRANSMISSION_ACK = 4, 5, 6, 7
PDU_RRC, PDU_DATA = 1, 2

DIRECTIONS = ("downlink", "uplink")


def u8(value, off):
    return (1, value, 0xff, off)


def u16(value, off):
    return (2, value, 0xffff, off)


def ip_keys(addr, off):
    """An exact IPv4 address as two 16-bit keys (u32 keys must be 4-byte aligned; 66 and 70 are not)."""
    a = [int(x) for x in addr.split(".")]
    return [u16((a[0] << 8) | a[1], off), u16((a[2] << 8) | a[3], off + 2)]


def prefix16_keys(prefix, off):
    """A /16 pool such as '10.46.' — exactly one 16-bit key."""
    a = [int(x) for x in prefix.strip(".").split(".")[:2]]
    return [u16((a[0] << 8) | a[1], off)]


def radio_keys(direction):
    """UDP to or from the RLS port: downlink comes FROM a gNB's port, uplink goes TO it."""
    return [u8(17, OFF_PROTO), u16(RLS_PORT, OFF_SPORT if direction == "downlink" else OFF_DPORT)]


def inner_addr_off(direction):
    """The UE's own address in the inner packet: destination downlink, source uplink."""
    return OFF_INNER_DST if direction == "downlink" else OFF_INNER_SRC


def matches(packet, keys):
    """Evaluate keys exactly as the kernel's u32 classifier does: offsets from the IP header."""
    for width, value, mask, off in keys:
        if off + width > len(packet):
            return False
        if int.from_bytes(packet[off:off + width], "big") & mask != value & mask:
            return False
    return True


def classify(packet, filters, default):
    """First matching filter, in filter-priority order — the same order tc uses."""
    for f in sorted(filters, key=lambda f: f["prio"]):
        if matches(packet, f["keys"]):
            return f["classid"]
    return default


# ─────────────────────────── configuration ───────────────────────────

def read_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        m = re.match(r'\s*([A-Z0-9_]+)=("?)(.*?)\2\s*(#.*)?$', line)
        if m:
            env[m.group(1)] = m.group(3)
    return env


def load_config(env_path=SLICES_ENV, compose_path=COMPOSE):
    """The scheduler's view of the slices, from slices.env (the single slice definition) and the
    compose UE plan (which gives each slice's address pool)."""
    env = read_env(env_path)
    counts = dict(x.split("=") for x in env.get("O5GS_UES", "").split())
    with open(compose_path, encoding="utf-8") as f:
        plan = re.search(r'UE_PLAN:\s*"([^"]+)"', f.read())
    pools = {}
    for item in (plan.group(1).split() if plan else []):
        _, _, gw, name = item.split(":")
        pools[name] = ".".join(gw.split(".")[:2]) + "."
    slices, imsi = [], FIRST_IMSI
    for letter in "ABCD":
        p = "SLICE_%s_" % letter
        if p + "NAME" not in env or letter not in counts:
            continue
        n = int(counts[letter])
        tiers = []
        for spec in env.get(p + "ARP_TIERS", "").split():
            count, level, cap, vuln = spec.split(":")
            tiers.append({"count": int(count), "arp": int(level), "may_preempt": cap == "MAY_PREEMPT"})
        if not tiers:
            tiers = [{"count": n, "arp": int(env.get(p + "ARP_PRIORITY", "8")), "may_preempt": False}]
        first = imsi
        for t in tiers:                       # tiers are applied in IMSI order (provision_subscribers.py)
            t["imsis"] = [first + i for i in range(t["count"])]
            first += t["count"]
        name = env[p + "NAME"]
        slices.append({
            "letter": letter, "name": name, "pool": pools.get(name, ""),
            "share": float(env.get(p + "SCHED_SHARE", "0")),
            "prio": int(env.get(p + "SCHED_PRIO", "3")),
            "max_latency_ms": float(env.get(p + "MAX_LATENCY_MS", "300")),
            "tiers": sorted(tiers, key=lambda t: t["arp"]),
        })
        imsi += n
    return {
        "mode": env.get("SCHED_MODE", "sliced"),
        "cell": {"downlink": float(env.get("SCHED_CELL_DL_MBPS", "250")),
                 "uplink": float(env.get("SCHED_CELL_UL_MBPS", "100"))},
        "signalling_share": float(env.get("SCHED_SIGNALLING_SHARE", "2")),
        "default_share": float(env.get("SCHED_DEFAULT_SHARE", "3")),
        "slices": slices,
    }


def validate(cfg):
    errors = []
    if cfg["mode"] not in ("sliced", "fifo", "off"):
        errors.append("mode must be sliced, fifo or off, not %r" % cfg["mode"])
    for d in DIRECTIONS:
        if not cfg["cell"][d] > 0:
            errors.append("%s cell capacity must be positive" % d)
    total = cfg["signalling_share"] + cfg["default_share"] + sum(s["share"] for s in cfg["slices"])
    if total > 100.0 + 1e-9:
        errors.append("guaranteed shares add up to %.1f %% of the cell; at most 100 %% can be guaranteed" % total)
    for s in cfg["slices"]:
        if s["share"] <= 0:
            errors.append("%s has no guaranteed share (SLICE_%s_SCHED_SHARE)" % (s["name"], s["letter"]))
        if not re.match(r"^\d+\.\d+\.$", s["pool"]):
            errors.append("%s has no address pool in the UE plan" % s["name"])
        if not 0 <= s["prio"] <= 6:
            errors.append("%s priority must be 0-6 (HTB)" % s["name"])
    return errors


# ─────────────────────────── the class tree ───────────────────────────

def kbit(mbps):
    return "%dkbit" % max(8, int(round(mbps * 1000)))


def build(cfg, direction, tier_addresses):
    """The HTB tree and filters for one direction, as data: classes, leaf qdiscs, filters.

    tier_addresses: {(slice name, tier index): [inner addresses]} for every tier except a slice's
    last, which is matched by the slice's pool (so a UE whose address is not yet known still lands
    in its slice, in the lowest tier)."""
    cell = cfg["cell"][direction]
    classes, filters = [], []
    if cfg["mode"] == "off":
        return {"mode": "off", "classes": [], "filters": [], "default": None}
    classes.append({"classid": "1:1", "parent": "1:", "rate": cell, "ceil": cell, "label": ("cell", "")})
    if cfg["mode"] == "fifo":
        classes.append({"classid": "1:10", "parent": "1:1", "rate": cell, "ceil": cell, "prio": 0,
                        "leaf": "pfifo limit %d" % FIFO_LIMIT, "label": ("all", "")})
        return {"mode": "fifo", "classes": classes, "filters": [], "default": "1:10"}

    share = lambda pct: cell * pct / 100.0
    classes.append({"classid": "1:10", "parent": "1:1", "rate": share(cfg["signalling_share"]), "ceil": cell,
                    "prio": 0, "leaf": "pfifo limit 256", "label": ("signalling", "")})
    classes.append({"classid": "1:99", "parent": "1:1", "rate": share(cfg["default_share"]), "ceil": cell,
                    "prio": 6, "leaf": "fq_codel", "label": ("other", "")})
    radio = radio_keys(direction)
    fprio = 1
    for t in (HEARTBEAT, HEARTBEAT_ACK, PDU_TRANSMISSION_ACK):
        filters.append({"prio": fprio, "classid": "1:10", "keys": radio + [u8(t, OFF_MSG_TYPE)], "what": "rls msg %d" % t})
    fprio += 1
    filters.append({"prio": fprio, "classid": "1:10", "what": "rrc",
                    "keys": radio + [u8(PDU_TRANSMISSION, OFF_MSG_TYPE), u8(PDU_RRC, OFF_PDU_TYPE)]})
    data = radio + [u8(PDU_TRANSMISSION, OFF_MSG_TYPE), u8(PDU_DATA, OFF_PDU_TYPE)]
    addr_off = inner_addr_off(direction)
    pool_filters = []
    for i, s in enumerate(cfg["slices"]):
        base = 20 + 10 * i
        rate = share(s["share"])
        short = s["max_latency_ms"] <= 20          # delay-critical: a short FIFO, not a fair queue
        leaf = "pfifo limit 64" if short else "fq_codel"
        tiers = s["tiers"]
        if len(tiers) == 1:
            classes.append({"classid": "1:%d" % base, "parent": "1:1", "rate": rate, "ceil": cell,
                            "prio": s["prio"], "leaf": leaf, "label": (s["name"], "")})
            pool_filters.append({"classid": "1:%d" % base, "keys": data + prefix16_keys(s["pool"], addr_off),
                                 "what": "%s pool" % s["name"]})
            continue
        classes.append({"classid": "1:%d" % base, "parent": "1:1", "rate": rate, "ceil": cell,
                        "label": (s["name"], "all")})
        total = sum(t["count"] for t in tiers)
        for j, t in enumerate(tiers):
            cid = "1:%d" % (base + 1 + j)
            classes.append({"classid": cid, "parent": "1:%d" % base, "rate": rate * t["count"] / total,
                            "ceil": cell, "prio": min(6, s["prio"] + j), "leaf": leaf,
                            "label": (s["name"], "arp%d" % t["arp"])})
            if j < len(tiers) - 1:
                for addr in sorted(tier_addresses.get((s["name"], j), [])):
                    fprio_addr = 3
                    filters.append({"prio": fprio_addr, "classid": cid, "keys": data + ip_keys(addr, addr_off),
                                    "what": "%s arp%d %s" % (s["name"], t["arp"], addr)})
            else:
                pool_filters.append({"classid": cid, "keys": data + prefix16_keys(s["pool"], addr_off),
                                     "what": "%s pool (arp%d)" % (s["name"], t["arp"])})
    for f in pool_filters:
        f["prio"] = 4
        filters.append(f)
    return {"mode": "sliced", "classes": classes, "filters": filters, "default": "1:99"}


def tc_batch(tree, dev):
    """tc -batch lines that install the tree on dev (the old root qdisc is removed first, separately)."""
    if tree["mode"] == "off":
        return []
    default = tree["default"].split(":")[1]
    lines = ["qdisc add dev %s root handle 1: htb default %s" % (dev, default)]
    for c in tree["classes"]:
        line = "class add dev %s parent %s classid %s htb rate %s ceil %s" % (
            dev, c["parent"], c["classid"], kbit(c["rate"]), kbit(c["ceil"]))
        if c["classid"] == "1:1":
            line += " burst 256k cburst 256k"
        else:
            line += " burst 64k cburst 256k quantum 15140"
            if "prio" in c:
                line += " prio %d" % c["prio"]
        lines.append(line)
        if "leaf" in c:
            lines.append("qdisc add dev %s parent %s handle %s: %s" % (
                dev, c["classid"], c["classid"].split(":")[1], c["leaf"]))
    for f in tree["filters"]:
        m = []
        for width, value, mask, off in f["keys"]:
            kind = {1: "u8", 2: "u16", 4: "u32"}[width]
            m.append("match %s 0x%x 0x%x at %d" % (kind, value, mask, off))
        lines.append("filter add dev %s parent 1: protocol ip prio %d u32 %s flowid %s" % (
            dev, f["prio"], " ".join(m), f["classid"]))
    return lines


# ─────────────────────────── the live system ───────────────────────────

def run(args, stdin=None, timeout=20):
    try:
        r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, str(e)


def find_ue_container():
    """(pid, netns inode) of the UE container, found through any nr-ue process: all of them share it."""
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % d) as f:
                comm = f.read().strip()
            if comm == "nr-ue":
                return int(d), os.stat("/proc/%s/ns/net" % d).st_ino
        except OSError:
            continue
    return None, None


def host_device(pid):
    """The host side of the UE container's eth0: its veth peer, by interface index."""
    rc, out = run(["nsenter", "-t", str(pid), "-n", "ip", "-o", "link", "show", "eth0"])
    m = re.search(r"eth0@if(\d+)", out) if rc == 0 else None
    if not m:
        return None
    rc, out = run(["ip", "-o", "link"])
    for line in out.splitlines():
        mm = re.match(r"(\d+):\s+([^:@]+)", line)
        if mm and mm.group(1) == m.group(1):
            return mm.group(2)
    return None


def ue_addresses(pid, imsi, pool):
    """The UE's current session addresses in a pool, from its own log (truncated at every start)."""
    try:
        with open("/proc/%d/root/tmp/ue-%d.log" % (pid, imsi), errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    addrs = re.findall(r"TUN interface\[[^,\]]+, ([0-9.]+)\]", text)
    return sorted({a for a in addrs if a.startswith(pool)})


def tier_addresses(cfg, pid):
    out = {}
    for s in cfg["slices"]:
        for j, t in enumerate(s["tiers"][:-1]):
            out[(s["name"], j)] = sorted({a for i in t["imsis"] for a in ue_addresses(pid, i, s["pool"])})
    return out


def tc_in(direction, pid, host_dev):
    """Command prefix and device for a direction: downlink on the host veth, uplink inside the UE netns."""
    if direction == "downlink":
        return ["tc"], host_dev
    return ["nsenter", "-t", str(pid), "-n", "tc"], "eth0"


class Scheduler:
    def __init__(self):
        self.cfg = load_config()
        errs = validate(self.cfg)
        if errs:
            raise SystemExit("invalid scheduler configuration:\n  " + "\n  ".join(errs))
        self.lock = threading.Lock()
        # One step at a time: the main loop and POST /config (HTTP thread) both call step(), and two
        # concurrent rebuilds of the same qdisc fail ("Exclusivity flag on, cannot modify").
        self.step_lock = threading.Lock()
        self.applied = None             # signature of what is installed
        self.trees = {}
        self.devices = {}
        self.stats = {}
        self.applies = 0
        self.errors = 0
        self.last_error = ""
        self.applied_at = 0.0
        self.pid = None
        self.tracked = {}

    def desired_signature(self, pid, ino, host_dev, tiers):
        return json.dumps([self.cfg, pid and ino, host_dev, {"%s/%d" % k: v for k, v in tiers.items()}],
                          sort_keys=True)

    def step(self):
        with self.step_lock:
            self._step()

    def _step(self):
        pid, ino = find_ue_container()
        if pid is None:
            with self.lock:
                self.pid, self.devices = None, {}
            return
        host_dev = host_device(pid)
        if not host_dev:
            return
        tiers = tier_addresses(self.cfg, pid)
        sig = self.desired_signature(pid, ino, host_dev, tiers)
        with self.lock:
            cfg = json.loads(json.dumps(self.cfg))
        if sig != self.applied:
            self.apply(cfg, pid, host_dev, tiers, sig)
        self.collect(pid, host_dev)

    def apply(self, cfg, pid, host_dev, tiers, sig):
        ok, trees, devs = True, {}, {}
        for d in DIRECTIONS:
            prefix, dev = tc_in(d, pid, host_dev)
            run(prefix + ["qdisc", "del", "dev", dev, "root"])          # absent is fine
            tree = build(cfg, d, tiers)
            lines = tc_batch(tree, dev)
            if lines:
                rc, out = run(prefix + ["-batch", "-"], stdin="\n".join(lines) + "\n")
                if rc != 0:
                    ok = False
                    self.last_error = "%s: %s" % (d, out[-400:])
            trees[d], devs[d] = tree, dev
        with self.lock:
            self.trees, self.devices, self.pid = trees, devs, pid
            self.tracked = {"%s/%d" % k: v for k, v in tiers.items()}
            self.applies += 1
            self.applied_at = time.time()
            if ok:
                self.applied = sig
                self.last_error = ""
            else:
                self.errors += 1
        print("%s applied mode=%s cell=%s on %s (ok=%s)%s" % (
            time.strftime("%H:%M:%S"), cfg["mode"], cfg["cell"], devs, ok,
            "" if ok else " " + self.last_error), flush=True)

    def collect(self, pid, host_dev):
        """Per-class counters. Bytes and packets come from the HTB class; drops and backlog from the
        class's LEAF queue. HTB's own class counters only see enqueue failures: fq_codel drops a fat
        flow's packets inside the leaf (883 948 under one overload) while the class showed 886."""
        stats = {}
        for d in DIRECTIONS:
            prefix, dev = tc_in(d, pid, host_dev)
            rc, out = run(prefix + ["-s", "-j", "class", "show", "dev", dev])
            if rc != 0 or not out:
                continue
            leaves = {}
            rc2, qout = run(prefix + ["-s", "-j", "qdisc", "show", "dev", dev])
            try:
                for q in (json.loads(qout) if rc2 == 0 and qout else []):
                    if q.get("parent", "").startswith("1:"):
                        leaves[q["parent"]] = q
            except ValueError:
                leaves = {}
            try:
                for c in json.loads(out):
                    st = c.get("stats", {})
                    handle = c.get("handle", "?")
                    leaf = leaves.get(handle)
                    lq = leaf if leaf is not None else st      # tc -j puts qdisc stats at top level
                    stats[(d, handle)] = {
                        "bytes": st.get("bytes", 0), "packets": st.get("packets", 0),
                        "drops": lq.get("drops", 0) if leaf is not None else st.get("drops", 0),
                        "overlimits": st.get("overlimits", 0),
                        "backlog": lq.get("backlog", 0) if leaf is not None else st.get("backlog", 0),
                        "qlen": lq.get("qlen", 0) if leaf is not None else st.get("qlen", 0)}
            except ValueError:
                continue
        with self.lock:
            self.stats = stats

    def reconfigure(self, req):
        """POST /config: change the knobs at run time (not persisted; slices.env is the default)."""
        with self.lock:
            cfg = json.loads(json.dumps(self.cfg))
        if "mode" in req:
            cfg["mode"] = req["mode"]
        if "cell_dl_mbps" in req:
            cfg["cell"]["downlink"] = float(req["cell_dl_mbps"])
        if "cell_ul_mbps" in req:
            cfg["cell"]["uplink"] = float(req["cell_ul_mbps"])
        for k in ("signalling_share", "default_share"):
            if k in req:
                cfg[k] = float(req[k])
        by_name = {s["name"]: s for s in cfg["slices"]}
        for name, v in (req.get("shares") or {}).items():
            if name not in by_name:
                return 400, {"error": "unknown slice %r" % name}
            by_name[name]["share"] = float(v)
        for name, v in (req.get("prio") or {}).items():
            if name not in by_name:
                return 400, {"error": "unknown slice %r" % name}
            by_name[name]["prio"] = int(v)
        errs = validate(cfg)
        if errs:
            return 400, {"error": errs}
        with self.lock:
            self.cfg = cfg
        self.step()
        return 200, self.state()

    def state(self):
        with self.lock:
            return {
                "mode": self.cfg["mode"], "cell_mbps": self.cfg["cell"],
                "shares": {s["name"]: s["share"] for s in self.cfg["slices"]},
                "prio": {s["name"]: s["prio"] for s in self.cfg["slices"]},
                "signalling_share": self.cfg["signalling_share"], "default_share": self.cfg["default_share"],
                "devices": self.devices, "ue_pid": self.pid, "applies": self.applies, "errors": self.errors,
                "last_error": self.last_error, "applied_at": self.applied_at, "tracked_addresses": self.tracked,
                "classes": {d: [{"classid": c["classid"], "slice": c["label"][0], "tier": c["label"][1],
                                 "rate_mbps": round(c["rate"], 3), "ceil_mbps": round(c["ceil"], 3),
                                 "prio": c.get("prio"), "leaf": c.get("leaf")}
                                for c in t["classes"]] for d, t in self.trees.items()},
                "filters": {d: [{"prio": f["prio"], "classid": f["classid"], "what": f["what"]}
                                for f in t["filters"]] for d, t in self.trees.items()},
            }

    def metrics(self):
        with self.lock:
            trees, stats, cfg = self.trees, self.stats, self.cfg
            lines = [
                "# HELP packet_scheduler_info Scheduler mode (1 for the active one).",
                "# TYPE packet_scheduler_info gauge",
            ]
            for m in ("sliced", "fifo", "off"):
                lines.append('packet_scheduler_info{mode="%s"} %d' % (m, int(cfg["mode"] == m)))
            lines += ["# TYPE packet_scheduler_cell_bps gauge"]
            for d in DIRECTIONS:
                lines.append('packet_scheduler_cell_bps{direction="%s"} %d' % (d, cfg["cell"][d] * 1e6))
            lines += ["# TYPE packet_scheduler_applies_total counter",
                      "packet_scheduler_applies_total %d" % self.applies,
                      "# TYPE packet_scheduler_apply_errors_total counter",
                      "packet_scheduler_apply_errors_total %d" % self.errors,
                      "# TYPE packet_scheduler_tracked_addresses gauge"]
            for k, v in sorted(self.tracked.items()):
                lines.append('packet_scheduler_tracked_addresses{tier="%s"} %d' % (k, len(v)))
            series = {"bytes": "counter", "packets": "counter", "drops": "counter",
                      "overlimits": "counter", "backlog": "gauge", "qlen": "gauge"}
            names = {"bytes": "packet_scheduler_class_bytes_total", "packets": "packet_scheduler_class_packets_total",
                     "drops": "packet_scheduler_class_drops_total", "overlimits": "packet_scheduler_class_overlimits_total",
                     "backlog": "packet_scheduler_class_backlog_bytes", "qlen": "packet_scheduler_class_backlog_packets"}
            for key, kind in series.items():
                lines.append("# TYPE %s %s" % (names[key], kind))
                for d, t in trees.items():
                    for c in t["classes"]:
                        st = stats.get((d, c["classid"]))
                        if st is None:
                            continue
                        lines.append('%s{direction="%s",classid="%s",slice="%s",tier="%s"} %d' % (
                            names[key], d, c["classid"], c["label"][0], c["label"][1], st[key]))
            lines.append("# TYPE packet_scheduler_class_rate_bps gauge")
            for d, t in trees.items():
                for c in t["classes"]:
                    lines.append('packet_scheduler_class_rate_bps{direction="%s",classid="%s",slice="%s",tier="%s"} %d' % (
                        d, c["classid"], c["label"][0], c["label"][1], c["rate"] * 1e6))
        return "\n".join(lines) + "\n"


def serve(sched):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/metrics":
                self._send(200, sched.metrics(), "text/plain; version=0.0.4")
            elif path in ("/state", ""):
                self._send(200, json.dumps(sched.state(), indent=2))
            else:
                self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/config":
                self._send(404, '{"error":"not found"}')
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                self._send(400, '{"error":"invalid JSON"}')
                return
            code, body = sched.reconfigure(req)
            self._send(code, json.dumps(body, indent=2))

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def main():
    sched = Scheduler()
    print("packet-scheduler on :%d — mode %s, cell %s Mbps, slices %s" % (
        PORT, sched.cfg["mode"], sched.cfg["cell"],
        ", ".join("%s %g%% prio %d" % (s["name"], s["share"], s["prio"]) for s in sched.cfg["slices"])), flush=True)
    threading.Thread(target=serve, args=(sched,), daemon=True).start()
    while True:
        try:
            sched.step()
        except Exception as e:      # keep scheduling; report instead of dying
            sched.errors += 1
            sched.last_error = repr(e)
            print("step failed: %r" % e, flush=True)
        time.sleep(INTERVAL)


def show(api="http://localhost:%d" % PORT):
    """The running scheduler's class tree with live counters, for people (and screenshots)."""
    import urllib.request
    st = json.loads(urllib.request.urlopen(api + "/state", timeout=10).read())
    text = urllib.request.urlopen(api + "/metrics", timeout=10).read().decode()
    cnt = {}
    for m in re.finditer(r'packet_scheduler_class_(packets|drops)_total\{direction="(\w+)",classid="([^"]+)"[^}]*\} (\d+)', text):
        cnt[(m.group(2), m.group(3), m.group(1))] = int(m.group(4))
    print("packet scheduler: mode %s, cell %g Mbps down / %g Mbps up, on %s (host veth) and %s (UE container)"
          % (st["mode"], st["cell_mbps"]["downlink"], st["cell_mbps"]["uplink"],
             st["devices"].get("downlink"), st["devices"].get("uplink")))
    print("critical URLLC devices tracked from their logs: %s" % ", ".join(
        a for v in st["tracked_addresses"].values() for a in v))
    print()
    print("%-8s %-11s %-6s %-4s %10s %10s %-15s %12s %12s %8s" % (
        "class", "slice", "tier", "prio", "guar Mbps", "ceil Mbps", "queue", "pkts down", "pkts up", "drops"))
    for c in st["classes"].get("downlink", []):
        if c["classid"] == "1:1":
            continue
        cid = c["classid"]
        print("%-8s %-11s %-6s %-4s %10.1f %10.1f %-15s %12d %12d %8d" % (
            cid, c["slice"], c["tier"] or "-", "-" if c["prio"] is None else c["prio"], c["rate_mbps"], c["ceil_mbps"],
            (c["leaf"] or "(parent)")[:15], cnt.get(("downlink", cid, "packets"), 0), cnt.get(("uplink", cid, "packets"), 0),
            cnt.get(("downlink", cid, "drops"), 0) + cnt.get(("uplink", cid, "drops"), 0)))
    print()
    print("filters (downlink; uplink reads the inner SOURCE address instead of the destination):")
    for f in st["filters"].get("downlink", []):
        print("  prio %d  %-40s -> %s" % (f["prio"], f["what"], f["classid"]))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--show":
        show()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("--print", "--check"):
        cfg = load_config()
        errs = validate(cfg)
        if errs:
            print("\n".join(errs))
            sys.exit(1)
        if sys.argv[1] == "--print":
            for d in DIRECTIONS:
                print("# %s" % d)
                print("\n".join(tc_batch(build(cfg, d, {}), "DEV")))
        else:
            print("scheduler configuration valid: mode %s, cell %s Mbps, %d slices" % (
                cfg["mode"], cfg["cell"], len(cfg["slices"])))
        sys.exit(0)
    main()
