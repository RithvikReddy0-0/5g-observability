#!/usr/bin/env python3
"""
orchestrator.py — decides which network slice a traffic demand should be served by, and
refuses it when no suitable slice has capacity left.

WHAT THIS IS, PRECISELY
-----------------------
This is the piece the original goal asked for: "when traffic comes in — video, blog, file —
divide it into the empty resources which are available."

It is deliberately NOT pretending to be part of the 3GPP core. Be clear about the split:

  REAL 3GPP, done by free5GC:
    * a UE is provisioned with the slices it is allowed to use (subscriber record in the UDR)
    * the UE requests a slice; the NSSF authorises it for that location
    * that is ALL standard 5G does — the network never picks a slice for you by load

  THIS SERVICE, sitting above the core (orchestration / SMO territory):
    * matches a traffic class to slices that can actually meet its latency requirement
    * enforces admission control against each slice's remaining capacity
    * refuses demands that would oversubscribe a slice
    * exposes the result as Prometheus metrics

  WHAT IT IS NOT:
    * it does not carry user traffic. There is no UPF on a host without gtp5g, so no packets
      traverse the 5G data path. Capacity here is modelled from the AMBR actually provisioned
      into the subscriber records, not measured from a live user plane.

Everything it decides is anchored in real deployment state: slice parameters come from
deployments/slices.env (the same file that provisions the subscribers), and the
UE -> allowed-slice mapping is read from the live MongoDB subscriber database. A UE can only
be admitted to a slice it is genuinely provisioned for.

TWO MODES, ONE DECISION LOGIC
-----------------------------
  free5GC (default)  — as described above: capacity modelled, nothing executed.
  Open5GS            — CORE=open5gs, with a real user plane (ADR-010/011):
    * PROMETHEUS_URL : admission also counts what each slice's UPF is MEASURED to carry, so
                       load the orchestrator did not admit still uses up capacity. Headroom is
                       capacity - max(admitted, measured).
    * EXECUTE=1      : every admitted demand becomes a real UDP flow at the demanded bitrate
                       on a device session of the chosen slice, through that slice's gNB and
                       UPF; delivered throughput and loss are measured per flow.
  Boundary in Open5GS mode: each device holds a PDU session on its home slice only, and a
  slice's gNB serves only that slice (ADR-011). An admitted flow therefore runs on the
  least-busy device of the CHOSEN slice — the requesting subscriber must still be permitted
  on that slice, but the packets need not leave from that subscriber's own device.

DYNAMIC ALLOCATION (Phase 2b, ADR-016)
--------------------------------------
  Priority inside a slice — each subscriber's ARP (priority level, may it pre-empt, may it be
      pre-empted) is read from the core. When a slice is full, a demand whose ARP may pre-empt
      stops admitted flows of strictly lower priority that may be pre-empted, until it fits.
      This is the 3GPP rule (TS 23.501 §5.7.2.2); the user plane here does not schedule by ARP,
      so the orchestrator is where it is enforced.
  Feedback controller (CONTROL=1) — every CONTROL_INTERVAL seconds it reads the protected
      slice's recent latency (URLLC p95) and how many of each throttled slice's flows (by default
      eMBB) fell short of their rate, and moves their ADMISSION LIMITS: cut multiplicatively
      from the operating point when either signal shows congestion, raised additively when both
      are healthy and demand is pressing on the limit (AIMD, as TCP does). The policy capacity
      in slices.env is the ceiling; the limit is what this deployment can serve right now.
  Planning (/plan) — for a set of demands, or the demand actually observed over the last few
      minutes, which slices are needed and how much capacity each needs.

Run with: scripts/start_orchestrator.sh              (free5GC)
          scripts/open5gs/start_orchestrator.sh      (Open5GS, measured + executed)
"""

import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SLICES_ENV = os.environ.get("SLICES_ENV", os.path.join(REPO, "deployments", "slices.env"))
PORT = int(os.environ.get("PORT", "9110"))
DB_CONTAINER = os.environ.get("DB_CONTAINER", "mongodb")
FLOW_TTL = int(os.environ.get("FLOW_TTL", "45"))          # seconds a demand holds capacity
SUB_REFRESH = int(os.environ.get("SUB_REFRESH", "60"))    # seconds between subscriber reloads

CORE = os.environ.get("CORE", "free5gc")                   # free5gc | open5gs
SPILLOVER = os.environ.get("SPILLOVER", "0") == "1"        # let demands take tighter slices when theirs is full
# Per-slice capacity override, "eMBB=120 URLLC=20": what the deployment can actually DELIVER,
# from scripts/open5gs/calibrate_capacity.sh, when that is below the policy in slices.env.
CAPACITY_OVERRIDE = dict(
    (k, float(v)) for k, v in (item.split("=", 1) for item in os.environ.get("CAPACITY_OVERRIDE", "").split()))
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "")      # measured headroom when set
EXECUTE = os.environ.get("EXECUTE", "0") == "1"            # run admitted demands as real flows
FLOW_SECONDS = int(os.environ.get("FLOW_SECONDS", "20"))   # duration of an executed flow
UE_CONTAINER = os.environ.get("UE_CONTAINER", "o5gs-ue")
# First of 300 ports for executed flows, kept clear of the KPI collectors' UDP ports (5400 URLLC,
# 5401 mMTC, ADR-015). With iperf3, whose UDP test uses its control port number, flows that drew
# 5400 failed instantly, freed it, and the next flow drew it again — found in the first Phase 2b
# priority run. flowgen.py receives on the device side, but the range stays clear regardless.
FLOW_PORT_FIRST = int(os.environ.get("FLOW_PORT_FIRST", "5410"))
# The paced UDP flow tool, shipped to the containers on stdin (no volume mount needed).
FLOWGEN = open(os.path.join(HERE, "flowgen.py"), encoding="utf-8").read()
# sst/sd=device-pool-prefix:data-network-ip:data-network-container — where each slice's device
# sessions live and where its traffic goes (a per-slice data network reached through that
# slice's UPF, ADR-012).
SLICE_PATHS = dict(
    item.split("=", 1) for item in os.environ.get(
        "SLICE_PATHS",
        "1/010203=10.45.:10.53.0.51:o5gs-dn-embb 2/112233=10.46.:10.53.0.52:o5gs-dn-urllc "
        "3/334455=10.47.:10.53.0.53:o5gs-dn-mmtc").split())

# Feedback controller (ADR-016). Off unless CONTROL=1; needs PROMETHEUS_URL for the latency signal.
CONTROL = os.environ.get("CONTROL", "0") == "1"
CONTROL_INTERVAL = float(os.environ.get("CONTROL_INTERVAL", "5"))      # seconds between steps
CONTROL_PROTECT = os.environ.get("CONTROL_PROTECT", "URLLC")           # slice whose latency is protected
# Slices whose admission limit moves. eMBB only, from a clean measurement on this stack: URLLC's
# p95 is 2.5 ms idle, 2.8 ms with URLLC demand only, 8.9 ms with eMBB demand only. eMBB is what
# hurts URLLC — 300 Mbps through the userspace gNB and UE takes CPU every slice shares (ADR-011).
# (An earlier measurement blamed URLLC's own flows; it was taken while iperf3 flows were burning
# the host's CPU, and is kept as superseded evidence.) Add URLLC here to also cap URLLC's own
# admission under latency pressure; ARP then decides which URLLC devices get the smaller capacity.
CONTROL_THROTTLE = os.environ.get("CONTROL_THROTTLE", "eMBB").split()
CONTROL_STAT = os.environ.get("CONTROL_STAT", "p95")                   # latency statistic watched
CONTROL_HIGH = float(os.environ.get("CONTROL_HIGH", "0.8"))   # cut above this share of the budget
CONTROL_LOW = float(os.environ.get("CONTROL_LOW", "0.5"))     # may grow below this share
CONTROL_DECREASE = float(os.environ.get("CONTROL_DECREASE", "0.7"))   # multiplicative cut
CONTROL_INCREASE = float(os.environ.get("CONTROL_INCREASE", "0.05"))  # additive step, share of ceiling
CONTROL_FLOOR = float(os.environ.get("CONTROL_FLOOR", "0.1"))         # never below this share of ceiling
CONTROL_SHORT_MAX = float(os.environ.get("CONTROL_SHORT_MAX", "0.2")) # tolerated share of short flows
# Seconds after a cut before the next: a cut only takes effect as flows already admitted end, so
# by default as long as one executed flow lasts.
CONTROL_HOLD = float(os.environ.get("CONTROL_HOLD", str(FLOW_SECONDS if EXECUTE else 15)))
CONTROL_WINDOW = float(os.environ.get("CONTROL_WINDOW", "30"))  # flows finished this recently count
PLAN_WINDOW = float(os.environ.get("PLAN_WINDOW", "300"))     # observed demand used by GET /plan
PLAN_HEADROOM = float(os.environ.get("PLAN_HEADROOM", "1.25"))

_lock = threading.Lock()


# ─────────────────────────── deployment definitions ───────────────────────────

def load_slices():
    """Parse deployments/slices.env — the same file used to provision subscribers."""
    env = {}
    with open(SLICES_ENV, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            # Quoted values may legitimately contain '#'; unquoted ones carry a trailing
            # comment that must be stripped or int() chokes on '9   # non-GBR'.
            if v[:1] in ('"', "'"):
                q = v[0]
                end = v.find(q, 1)
                v = v[1:end] if end > 0 else v[1:]
            else:
                v = v.split("#", 1)[0].strip()
            env[k.strip()] = v

    slices = []
    for letter in ("A", "B", "C", "D"):
        p = "SLICE_%s_" % letter
        if p + "SST" not in env:
            continue
        slices.append({
            "key": letter,
            "name": env.get(p + "NAME", letter),
            "sst": int(env[p + "SST"]),
            "sd": env[p + "SD"],
            "five_qi": int(env.get(p + "5QI", 9)),
            "arp": int(env.get(p + "ARP_PRIORITY", 8)),
            "capacity_mbps": float(env.get(p + "CAPACITY_MBPS", 100)),
            "max_latency_ms": float(env.get(p + "MAX_LATENCY_MS", 300)),
            # Set by the network orchestrator (ADR-017): an inactive slice is deployed but parked
            # or being drained, and admits nothing. Planning still considers it.
            "active": True,
            # The slice's default ARP, for subscribers whose own record does not say otherwise.
            "arp_default": {"priority": int(env.get(p + "ARP_PRIORITY", 8)),
                            "may_preempt": env.get(p + "ARP_PREEMPT_CAP") == "MAY_PREEMPT",
                            "preemptable": env.get(p + "ARP_PREEMPT_VULN") == "PRE_EMPTABLE"},
        })

    classes = {}
    for spec in env.get("TRAFFIC_CLASSES", "").split():
        parts = spec.split(":")
        if len(parts) == 3:
            classes[parts[0]] = {"mbps": float(parts[1]), "max_latency_ms": float(parts[2])}

    return slices, classes


SLICES, TRAFFIC_CLASSES = load_slices()

# Deliverable capacity, if calibrated below policy. The policy value is kept for /state.
for _s in SLICES:
    _s["policy_capacity_mbps"] = _s["capacity_mbps"]
    if _s["name"] in CAPACITY_OVERRIDE:
        _s["capacity_mbps"] = min(_s["capacity_mbps"], CAPACITY_OVERRIDE[_s["name"]])


def snssai(s):
    return "%d/%s" % (s["sst"], s["sd"])


# ─────────────────────────── live subscriber state ───────────────────────────

_subs = {"map": {}, "ts": 0.0}
_arp = {}           # supi -> {snssai: {"priority", "may_preempt", "preemptable"}} from the core


def load_subscribers():
    """{supi: ['sst/sd', ...]} — every slice the subscriber is PERMITTED to use.

    This is what keeps the orchestrator honest: a UE may only be admitted to a slice it is
    genuinely provisioned for in the core, not one we wish it were on.

    Reads both `defaultSingleNssais` (the default slice) and `singleNssais` (the other
    permitted ones), because in 3GPP a subscriber normally has several. Reading only the
    default would make the choice predetermined — which defeats the point of an allocator.
    """
    js = (
        'var o={};'
        'db["subscriptionData.provisionedData.amData"]'
        '.find({},{_id:0,ueId:1,nssai:1}).forEach(function(d){'
        '  var out=[];'
        '  var add=function(l){(l||[]).forEach(function(s){'
        '      var k=s.sst+"/"+(s.sd||""); if(out.indexOf(k)<0){out.push(k);}});};'
        '  if(d.nssai){add(d.nssai.defaultSingleNssais); add(d.nssai.singleNssais);}'
        '  if(out.length){o[d.ueId]=out;}'
        '});'
        'print(JSON.stringify(o));'
    )
    db = "free5gc"
    if CORE == "open5gs":
        # Open5GS keeps every permitted S-NSSAI in subscribers.slice[]; the IMSI has no prefix.
        # Each slice entry also carries the subscriber's ARP on that slice (ADR-016); Open5GS
        # encodes pre-emption capability/vulnerability as 1 = disabled, 2 = enabled.
        db = "open5gs"
        js = (
            'var o={},a={};'
            'db.subscribers.find({},{_id:0,imsi:1,slice:1}).forEach(function(d){'
            '  var out=[],arp={};'
            '  (d.slice||[]).forEach(function(s){var k=s.sst+"/"+(s.sd||"");'
            '      if(out.indexOf(k)<0){out.push(k);}'
            '      var q=(s.session&&s.session[0]&&s.session[0].qos)||{};'
            '      if(q.arp){arp[k]=[q.arp.priority_level,q.arp.pre_emption_capability,'
            '                        q.arp.pre_emption_vulnerability];}});'
            '  if(out.length){o["imsi-"+d.imsi]=out; a["imsi-"+d.imsi]=arp;}'
            '});'
            'print(JSON.stringify({slices:o,arp:a}));'
        )
    try:
        out = subprocess.run(
            ["docker", "exec", "-i", DB_CONTAINER, "mongo", db, "--quiet", "--eval", js],
            capture_output=True, text=True, timeout=30,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                if CORE == "open5gs":
                    arp = {supi: {sn: {"priority": int(v[0]), "may_preempt": int(v[1]) == 2,
                                       "preemptable": int(v[2]) == 2}
                                  for sn, v in per.items()}
                           for supi, per in data.get("arp", {}).items()}
                    return data.get("slices", {}), arp
                return data, {}
    except Exception:
        pass
    return {}, {}


def subscribers():
    now = time.time()
    if now - _subs["ts"] > SUB_REFRESH or not _subs["map"]:
        m, arp = load_subscribers()
        if m:
            _subs["map"] = m
            _arp.clear()
            _arp.update(arp)
        _subs["ts"] = now
    return _subs["map"]


def arp_of(supi, s):
    """The subscriber's ARP on slice s: its own record if the core has one, else the slice's."""
    return _arp.get(supi, {}).get(snssai(s), s["arp_default"])


# ─────────────────────────── measured load (Open5GS) ───────────────────────────

_measured = {"by_slice": {}, "ts": 0.0, "ok": False}


def measured_mbps():
    """{slice name: Mbps} actually carried by each slice's UPF over the last 15 s, both
    directions. Cached for 3 s. Empty when PROMETHEUS_URL is unset or unreachable — the
    orchestrator then falls back to admitted demand alone, and says so in /state."""
    if not PROMETHEUS_URL:
        return {}
    now = time.time()
    if now - _measured["ts"] < 3:
        return _measured["by_slice"]
    q = ("sum by (slice) (rate(upf_slice_downlink_bytes_total[15s]) + "
         "rate(upf_slice_uplink_bytes_total[15s])) * 8 / 1e6")
    try:
        url = PROMETHEUS_URL.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
        with urllib.request.urlopen(url, timeout=3) as r:
            res = json.loads(r.read())["data"]["result"]
        _measured["by_slice"] = {x["metric"].get("slice", ""): float(x["value"][1]) for x in res}
        _measured["ok"] = True
    except Exception:
        _measured["ok"] = False
    _measured["ts"] = now
    return _measured["by_slice"]


# ─────────────────────────── allocation state ───────────────────────────

flows = []          # active demands holding capacity
counters = {}       # (event, snssai, cls, reason) -> count
by_priority = {}    # (slice, arp priority, decision) -> count; decision admitted|refused|preempted
demand_log = []     # (time, cls, mbps) of every demand with a known class, for GET /plan
limits = {}         # slice name -> admission limit (Mbps) set by the controller; absent = capacity


def bump(event, sn="", cls="", reason=""):
    k = (event, sn, cls, reason)
    counters[k] = counters.get(k, 0) + 1


def expire_flows():
    """Drop demands whose hold has ended. Executed flows are removed when their traffic
    actually finishes (see run_flow), so their expiry is only a safety net."""
    now = time.time()
    global flows
    flows = [f for f in flows if f["expires"] > now]


def allocated(sn):
    return sum(f["mbps"] for f in flows if f["snssai"] == sn)


def in_use(s):
    """Capacity counted against a slice: admitted demand, or measured traffic if higher."""
    return max(allocated(snssai(s)), measured_mbps().get(s["name"], 0.0))


def limit(s):
    """What a slice admits up to now: the controller's limit, never above the policy capacity."""
    return min(s["capacity_mbps"], limits.get(s["name"], s["capacity_mbps"]))


def count_priority(s, arp, decision):
    k = (s["name"], arp["priority"], decision)
    by_priority[k] = by_priority.get(k, 0) + 1


def preemption_victims(s, arp, need):
    """Admitted flows on slice s that a demand with this ARP may stop to make room for `need`.

    TS 23.501 §5.7.2.2: only a demand whose ARP may pre-empt, and only flows that may be
    pre-empted AND have a strictly lower priority (a higher ARP priority level). Lowest priority
    first, newest first within a level. None if even all of them would not free enough."""
    if not arp["may_preempt"]:
        return None
    sn = snssai(s)
    cands = sorted((f for f in flows if f["snssai"] == sn and f["arp"]["preemptable"]
                    and f["arp"]["priority"] > arp["priority"]),
                   key=lambda f: (-f["arp"]["priority"], -f["admitted_at"]))
    alloc, meas = allocated(sn), measured_mbps().get(s["name"], 0.0)
    chosen, freed = [], 0.0
    for f in cands:
        if max(alloc - freed, meas - freed) + need <= limit(s):
            break
        chosen.append(f)
        freed += f["mbps"]
    if max(alloc - freed, meas - freed) + need <= limit(s):
        return chosen
    return None


def preempt(victims, by_supi):
    """Release the victims' capacity now and stop their traffic."""
    global flows
    ids = {f["id"] for f in victims}
    flows = [f for f in flows if f["id"] not in ids]
    for f in victims:
        f["preempted"] = by_supi
        bump("preempted", f["snssai"], f["cls"], "arp")
        count_priority(next(s for s in SLICES if snssai(s) == f["snssai"]), f["arp"], "preempted")
        if f.get("device"):
            threading.Thread(target=stop_flow, args=(f,), daemon=True).start()


def decide(supi, cls, mbps=None):
    """Pick a slice for this demand, or refuse it.

    Order of checks matters and mirrors how a real admission decision would go:
      1. is the traffic class known?
      2. which slices is this subscriber actually allowed on?
      3. of those, which can meet the latency the traffic needs?
      4. of those, which still has room?
      5. among survivors prefer the tightest fit, so a low-latency slice is not consumed by
         traffic that a best-effort slice could have carried.
    """
    expire_flows()

    spec = TRAFFIC_CLASSES.get(cls)
    if not spec:
        bump("rejected", cls=cls, reason="unknown_class")
        return {"admitted": False, "reason": "unknown traffic class '%s'" % cls}

    need = float(mbps) if mbps is not None else spec["mbps"]
    need_latency = spec["max_latency_ms"]
    demand_log.append((time.time(), cls, need))
    del demand_log[:-20000]

    allowed = subscribers().get(supi)
    if not allowed:
        bump("rejected", cls=cls, reason="unknown_subscriber")
        return {"admitted": False, "reason": "subscriber %s is not provisioned" % supi}

    candidates = [s for s in SLICES if snssai(s) in allowed]
    if not candidates:
        bump("rejected", cls=cls, reason="no_subscribed_slice")
        return {"admitted": False,
                "reason": "subscriber is permitted on %s, none of which is a defined slice"
                          % ",".join(allowed)}
    # Slices the network orchestrator has deactivated admit nothing (ADR-017). The demand is still
    # in demand_log, so /plan sees it — that is how a dormant slice gets activated again.
    if not any(s["active"] for s in candidates):
        s = candidates[0]
        bump("rejected", snssai(s), cls, "inactive")
        return {"admitted": False,
                "reason": "slice %s (%s) is not active" % (snssai(s), ",".join(c["name"] for c in candidates))}
    candidates = [s for s in candidates if s["active"]]

    fits_latency = [s for s in candidates if s["max_latency_ms"] <= need_latency]
    if not fits_latency:
        best = min(candidates, key=lambda s: s["max_latency_ms"])
        bump("rejected", snssai(best), cls, "latency")
        return {"admitted": False,
                "reason": "%s needs <=%gms; best permitted slice %s offers only %gms"
                          % (cls, need_latency, snssai(best), best["max_latency_ms"])}

    # Protect premium capacity. A demand belongs on the LEAST specialised slices that meet its
    # latency need (the loosest guarantee still good enough). If those are full it is refused —
    # it does not spill into a tighter slice. Found on real traffic: with eMBB full, video was
    # admitted onto URLLC and took 16 of its 20 Mbps, leaving 4 for the control traffic URLLC
    # exists for. SPILLOVER=1 restores the permissive behaviour deliberately.
    if not SPILLOVER:
        loosest = max(s["max_latency_ms"] for s in fits_latency)
        fits_latency = [s for s in fits_latency if s["max_latency_ms"] == loosest]

    has_room = [s for s in fits_latency if in_use(s) + need <= limit(s)]
    victims = []
    if not has_room:
        # Full. A demand whose ARP may pre-empt can still take the place of lower-priority flows
        # (priority inside a slice, ADR-016); on the slice where that costs the least.
        options = [(s, preemption_victims(s, arp_of(supi, s), need)) for s in fits_latency]
        options = [(s, v) for s, v in options if v is not None]
        if options:
            s, victims = min(options, key=lambda o: (sum(f["mbps"] for f in o[1]), -o[0]["max_latency_ms"]))
            has_room = [s]
    if not has_room:
        s = fits_latency[0]
        sn = snssai(s)
        bump("rejected", sn, cls, "capacity")
        count_priority(s, arp_of(supi, s), "refused")
        return {"admitted": False,
                "reason": "slice %s (%s) full: %.1f/%.1f Mbps in use (admitted %.1f, measured %.1f, policy %.0f), %s needs %.1f"
                          % (sn, s["name"], in_use(s), limit(s), allocated(sn),
                             measured_mbps().get(s["name"], 0.0), s["capacity_mbps"], cls, need)}

    # CHEAPEST ADEQUATE slice, not the tightest fit.
    #
    # Sort by loosest latency guarantee that still satisfies the requirement, then by most
    # spare headroom. The point is to avoid burning scarce low-latency capacity on traffic
    # that does not need it: video tolerates 300 ms, so it belongs on the 200 Mbps eMBB
    # slice, leaving the 20 Mbps URLLC slice for control traffic that genuinely needs 10 ms.
    #
    # An earlier version minimised spare headroom, which did the exact opposite — it packed
    # video onto URLLC because that slice was smaller, then starved control traffic.
    chosen = max(
        has_room,
        key=lambda s: (s["max_latency_ms"], limit(s) - in_use(s)),
    )
    sn = snssai(chosen)
    arp = arp_of(supi, chosen)
    if victims:
        preempt(victims, supi)
    flow = {"id": "%x" % random.getrandbits(32), "supi": supi, "cls": cls, "mbps": need,
            "snssai": sn, "slice": chosen["name"], "arp": arp, "admitted_at": time.time(),
            "expires": time.time() + (FLOW_SECONDS + 30 if EXECUTE else FLOW_TTL)}
    flows.append(flow)
    bump("admitted", sn, cls)
    count_priority(chosen, arp, "admitted")
    execution = None
    if EXECUTE:
        execution = start_flow(flow)
    return {
        "admitted": True, "supi": supi, "traffic_class": cls,
        "slice": {"name": chosen["name"], "sst": chosen["sst"], "sd": chosen["sd"],
                  "5qi": chosen["five_qi"], "arp": chosen["arp"]},
        "arp": arp,
        "preempted": [{"id": f["id"], "cls": f["cls"], "mbps": f["mbps"], "arp": f["arp"]["priority"]}
                      for f in victims],
        "mbps": need,
        "slice_used_mbps": round(allocated(sn), 2),
        "slice_measured_mbps": round(measured_mbps().get(chosen["name"], 0.0), 2),
        "slice_capacity_mbps": chosen["capacity_mbps"],
        "slice_limit_mbps": round(limit(chosen), 2),
        "flow": execution,
    }


# ─────────────────────────── real traffic (Open5GS, EXECUTE=1) ───────────────────────────

_ports = set()
flow_results = []        # last completed flows, newest last
busy_devices = {}        # device ip -> number of executing flows


def device_sessions(sn):
    """[(interface, ip)] device sessions currently up on this slice's address pool."""
    prefix = SLICE_PATHS.get(sn, "::").split(":")[0]
    try:
        out = subprocess.run(["docker", "exec", UE_CONTAINER, "ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    found = re.findall(r"^\d+:\s+(uesimtun\d+)\s+inet\s+([\d.]+)/", out, re.M)
    return [(dev, ip) for dev, ip in found if ip.startswith(prefix)]


def start_flow(flow):
    """Pick the least-busy device on the chosen slice and launch the flow in the background.
    Called with _lock held; the flow itself runs outside the lock."""
    sessions = device_sessions(flow["snssai"])
    if not sessions:
        flow["expires"] = 0          # nothing can carry it; release the capacity immediately
        bump("exec_failed", flow["snssai"], flow["cls"], "no_device_session")
        return {"started": False, "reason": "no device session up on this slice"}
    ip = min(sessions, key=lambda d: (busy_devices.get(d[1], 0), random.random()))[1]
    port = next(p for p in range(FLOW_PORT_FIRST, FLOW_PORT_FIRST + 300) if p not in _ports)
    _ports.add(port)
    busy_devices[ip] = busy_devices.get(ip, 0) + 1
    flow.update({"device": ip, "port": port})
    threading.Thread(target=run_flow, args=(flow,), daemon=True).start()
    return {"started": True, "device": ip, "seconds": FLOW_SECONDS, "id": flow["id"]}


def stop_flow(flow):
    """Pre-emption on the real data path: kill the flow's sender in the data network. The
    device's receiver notices the silence, reports what arrived, and run_flow records 'preempted'."""
    _, _, dn = SLICE_PATHS[flow["snssai"]].split(":")
    pattern = "python3 - send %s %d " % (flow["device"], flow["port"])
    for _ in range(10):      # the sender may not have started yet when pre-emption comes early
        if subprocess.run(["docker", "exec", dn, "pkill", "-f", pattern],
                          capture_output=True, timeout=10).returncode == 0:
            return
        if flow.get("done"):
            return
        time.sleep(0.5)


def run_flow(flow):
    """UDP at exactly the admitted bitrate, downlink, through the slice's gNB and UPF.
    UDP rather than TCP: TCP expands to fill whatever it finds, which would measure the path,
    not whether the admitted rate was delivered.

    flowgen.py, not iperf3: iperf3 3.16's UDP sender busy-waits, 45-90 % of a core per flow at
    any rate, and that CPU load inflated the URLLC latency the controller steers by (ADR-016).
    Both ends run under `timeout -s KILL` INSIDE their containers — a Python timeout only kills the
    `docker exec` wrapper, and a leaked flow process once kept spinning for five minutes."""
    _, _, dn = SLICE_PATHS[flow["snssai"]].split(":")    # the slice's data-network container
    port, ip = flow["port"], flow["device"]
    outcome, delivered, loss = "failed", 0.0, 100.0
    hard = str(FLOW_SECONDS + 20)
    try:
        rx = subprocess.Popen(
            ["docker", "exec", "-i", UE_CONTAINER, "timeout", "-s", "KILL", hard,
             "python3", "-", "recv", ip, str(port), str(FLOW_SECONDS)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        rx.stdin.write(FLOWGEN)
        rx.stdin.close()
        if "READY" not in rx.stdout.readline():
            raise RuntimeError("receiver did not start: %s" % rx.stderr.read()[:100])
        subprocess.run(["docker", "exec", "-i", dn, "timeout", "-s", "KILL", hard,
                        "python3", "-", "send", ip, str(port), "%g" % flow["mbps"], str(FLOW_SECONDS)],
                       input=FLOWGEN, capture_output=True, text=True, timeout=FLOW_SECONDS + 30)
        out = rx.stdout.read()               # until the receiver exits (idle end, or its hard timeout)
        rx.wait(timeout=30)
        res = json.loads(out.strip().splitlines()[-1])
        loss, delivered = res["loss_percent"], res["mbps"]
        # "met": the network delivered what was admitted (>= 95 % of the rate, <= 2 % loss).
        outcome = "met" if delivered >= 0.95 * flow["mbps"] and loss <= 2.0 else "short"
    except Exception as exc:
        flow["error"] = str(exc)[:120]
    finally:
        if flow.get("preempted"):
            outcome = "preempted"
        flow["done"] = True
        with _lock:
            _ports.discard(port)
            busy_devices[ip] = max(0, busy_devices.get(ip, 1) - 1)
            flow["expires"] = 0                          # capacity released when traffic ends
            bump("completed", flow["snssai"], flow["cls"], outcome)
            flow_results.append({"id": flow["id"], "cls": flow["cls"], "slice": flow["slice"],
                                 "supi": flow["supi"], "arp": flow["arp"]["priority"],
                                 "preempted_by": flow.get("preempted"), "ended": time.time(),
                                 "device": ip, "requested_mbps": flow["mbps"],
                                 "delivered_mbps": round(delivered, 3),
                                 "loss_percent": round(loss, 3), "outcome": outcome,
                                 "error": flow.get("error"),
                                 "finished": time.strftime("%H:%M:%S")})
            del flow_results[:-500]


# ─────────────────────────── feedback controller (ADR-016) ───────────────────────────

control = {"latency_ms": None, "latency_ok": False, "short_ratio": {}, "last_cut": {},
           "steps": 0, "log": []}
control_actions = {}     # (slice, action) -> count; action cut|raise|hold


def protected_latency():
    """Recent latency statistic of the protected slice, from its KPI collector via Prometheus
    (kpi_owd_recent_ms, a 10 s window). None when unknown — the controller then never raises."""
    if not PROMETHEUS_URL:
        return None
    q = 'kpi_owd_recent_ms{slice="%s",stat="%s"}' % (CONTROL_PROTECT, CONTROL_STAT)
    try:
        url = PROMETHEUS_URL.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
        with urllib.request.urlopen(url, timeout=3) as r:
            res = json.loads(r.read())["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def short_ratio(name, now):
    """Share of the slice's flows that finished in the last CONTROL_WINDOW seconds without
    receiving their admitted rate. Pre-empted flows are excluded: they were stopped on purpose.
    None with fewer than 4 flows — too few to judge."""
    recent = [r for r in flow_results if r["slice"] == name and r.get("ended", 0) >= now - CONTROL_WINDOW
              and r["outcome"] != "preempted"]
    if len(recent) < 4:
        return None
    return sum(r["outcome"] != "met" for r in recent) / len(recent)


def control_step(latency, now=None):
    """One controller step. `latency` is the protected slice's recent statistic (ms) or None.
    Called with _lock held. Returns the actions taken, for the log and the tests."""
    now = time.time() if now is None else now
    protect = next((s for s in SLICES if s["name"] == CONTROL_PROTECT), None)
    budget = protect["max_latency_ms"] if protect else None
    control["latency_ms"], control["latency_ok"] = latency, latency is not None
    actions = []
    for s in SLICES:
        if s["name"] not in CONTROL_THROTTLE:
            continue
        name, ceiling = s["name"], s["capacity_mbps"]
        cur = limit(s)
        sr = short_ratio(name, now)
        control["short_ratio"][name] = sr
        hot = latency is not None and budget and latency > CONTROL_HIGH * budget
        short = sr is not None and sr > CONTROL_SHORT_MAX
        if hot or short:
            if now - control["last_cut"].get(name, 0) < CONTROL_HOLD:
                action, new = "hold", cur          # the last cut has not had time to show yet
            else:
                # Cut from the operating point, not from an unused limit: a limit of 500 with
                # 300 in use is already no constraint, and cutting it to 350 would change nothing.
                new = max(CONTROL_FLOOR * ceiling, min(cur, in_use(s)) * CONTROL_DECREASE)
                action = "cut"
                control["last_cut"][name] = now
            why = ("%s %s %.1f ms > %.1f ms" % (CONTROL_PROTECT, CONTROL_STAT, latency, CONTROL_HIGH * budget)
                   if hot else "%.0f%% of %s flows short" % (100 * sr, name))
        elif (latency is not None and budget and latency < CONTROL_LOW * budget
              and (sr is None or sr <= CONTROL_SHORT_MAX) and in_use(s) >= 0.8 * cur and cur < ceiling):
            # Healthy, and demand is pressing on the limit: probe upwards.
            new, action = min(ceiling, cur + CONTROL_INCREASE * ceiling), "raise"
            why = "%s %s %.1f ms, %s at %.0f/%.0f Mbps" % (CONTROL_PROTECT, CONTROL_STAT, latency,
                                                           name, in_use(s), cur)
        else:
            new, action, why = cur, "hold", ""
        limits[name] = round(new, 2)
        control_actions[(name, action)] = control_actions.get((name, action), 0) + 1
        if action != "hold":
            entry = {"t": time.strftime("%H:%M:%S", time.localtime(now)), "slice": name, "action": action,
                     "from": round(cur, 1), "to": round(new, 1), "why": why}
            actions.append(entry)
            control["log"].append(entry)
            del control["log"][:-200]
            print("CONTROL %-5s %-5s %6.1f -> %6.1f Mbps  (%s)" % (action, name, cur, new, why), flush=True)
    control["steps"] += 1
    return actions


def control_loop():
    while True:
        time.sleep(CONTROL_INTERVAL)
        latency = protected_latency()          # network I/O outside the lock
        with _lock:
            expire_flows()
            try:
                control_step(latency)
            except Exception as exc:
                print("control step failed: %s" % exc, flush=True)


# ─────────────────────────── planning (ADR-016) ───────────────────────────

def plan(demands, headroom=PLAN_HEADROOM):
    """Which slices a set of demands needs, and how much capacity each.

    demands: [{"class": name, "count": n}] (n concurrent demands of that class) or
             [{"class": name, "mbps": total}] (aggregate rate of that class).
    Each class goes to the CHEAPEST ADEQUATE slice — the loosest latency guarantee that still
    meets it, the rule admission uses — so a slice is needed only if some demand requires what
    no looser slice offers. Required capacity is the demand times `headroom`, compared with the
    policy ceiling and with the current admission limit (what the deployment serves now)."""
    per, unservable = {}, []
    for d in demands:
        spec = TRAFFIC_CLASSES.get(d.get("class"))
        if not spec:
            unservable.append({"class": d.get("class"), "reason": "unknown traffic class"})
            continue
        total = float(d["mbps"]) if "mbps" in d else spec["mbps"] * float(d.get("count", 1))
        fits = [s for s in SLICES if s["max_latency_ms"] <= spec["max_latency_ms"]]
        if not fits:
            unservable.append({"class": d["class"], "reason": "needs <=%gms, no slice offers it"
                               % spec["max_latency_ms"]})
            continue
        s = max(fits, key=lambda x: (x["max_latency_ms"], x["capacity_mbps"]))
        p = per.setdefault(s["name"], {"demand_mbps": 0.0, "classes": {}})
        p["demand_mbps"] += total
        p["classes"][d["class"]] = round(p["classes"].get(d["class"], 0) + total, 3)
    slices = []
    for s in SLICES:
        p = per.get(s["name"])
        need = round(p["demand_mbps"] * headroom, 2) if p else 0.0
        slices.append({
            "slice": s["name"], "snssai": snssai(s), "needed": bool(p),
            "demand_mbps": round(p["demand_mbps"], 2) if p else 0.0,
            "required_mbps": need, "classes": p["classes"] if p else {},
            "policy_capacity_mbps": s["capacity_mbps"], "current_limit_mbps": round(limit(s), 2),
            "fits_policy": need <= s["capacity_mbps"], "fits_now": need <= limit(s),
        })
    return {"slices_needed": sum(1 for x in slices if x["needed"]),
            "headroom": headroom, "slices": slices, "unservable": unservable}


def observed_demands(window=PLAN_WINDOW, now=None):
    """The demand offered over the last `window` seconds, as an average concurrent rate per class.
    Little's law: concurrent load = arrival rate x holding time, and each admitted demand holds
    its rate for FLOW_SECONDS (executed) or FLOW_TTL. Refused demands count too: they are demand."""
    now = time.time() if now is None else now
    hold = FLOW_SECONDS if EXECUTE else FLOW_TTL
    totals = {}
    for t, cls, mbps in demand_log:
        if t >= now - window:
            totals[cls] = totals.get(cls, 0.0) + mbps
    return [{"class": c, "mbps": round(v * hold / window, 3)} for c, v in sorted(totals.items())]


# ─────────────────────────── metrics ───────────────────────────

def metrics():
    expire_flows()
    out = []

    def add(name, typ, help_text, rows):
        out.append("# HELP %s %s" % (name, help_text))
        out.append("# TYPE %s %s" % (name, typ))
        out.extend(rows)

    cap, alloc, util, nflows = [], [], [], []
    for s in SLICES:
        sn = snssai(s)
        lbl = 'sst="%d",sd="%s",slice="%s"' % (s["sst"], s["sd"], s["name"])
        a = allocated(sn)
        cap.append("slice_capacity_mbps{%s} %g" % (lbl, s["capacity_mbps"]))
        alloc.append("slice_allocated_mbps{%s} %.3f" % (lbl, a))
        util.append("slice_utilization_ratio{%s} %.4f"
                    % (lbl, (a / s["capacity_mbps"]) if s["capacity_mbps"] else 0))
        nflows.append("slice_active_flows{%s} %d"
                      % (lbl, len([f for f in flows if f["snssai"] == sn])))

    add("slice_capacity_mbps", "gauge",
        "Capacity of each slice, from the AMBR provisioned into its subscribers.", cap)
    add("slice_allocated_mbps", "gauge",
        "Bandwidth currently admitted onto each slice by the orchestrator.", alloc)
    add("slice_utilization_ratio", "gauge",
        "Allocated divided by capacity, per slice (1.0 = full).", util)
    add("slice_active_flows", "gauge",
        "Number of admitted demands currently holding capacity on each slice.", nflows)
    add("slice_active", "gauge",
        "1 when the slice admits demands; 0 when the network orchestrator has deactivated it (ADR-017).",
        ['slice_active{sst="%d",sd="%s",slice="%s"} %d' % (s["sst"], s["sd"], s["name"], 1 if s["active"] else 0)
         for s in SLICES])
    add("slice_admission_limit_mbps", "gauge",
        "What each slice admits up to now: the feedback controller's limit, at most the capacity.",
        ['slice_admission_limit_mbps{sst="%d",sd="%s",slice="%s"} %.2f'
         % (s["sst"], s["sd"], s["name"], limit(s)) for s in SLICES])
    add("slice_decisions_by_priority_total", "counter",
        "Demands by slice, the requester's ARP priority level and decision (admitted, refused, preempted).",
        ['slice_decisions_by_priority_total{slice="%s",arp="%d",decision="%s"} %d' % (k[0], k[1], k[2], v)
         for k, v in sorted(by_priority.items())])
    if CONTROL:
        rows = []
        if control["latency_ms"] is not None:
            rows.append('slice_control_latency_ms{slice="%s",stat="%s"} %.3f'
                        % (CONTROL_PROTECT, CONTROL_STAT, control["latency_ms"]))
        add("slice_control_latency_ms", "gauge",
            "The protected slice's recent latency the controller last acted on.", rows)
        add("slice_control_short_ratio", "gauge",
            "Share of a throttled slice's recent flows that missed their rate.",
            ['slice_control_short_ratio{slice="%s"} %.4f' % (n, r)
             for n, r in sorted(control["short_ratio"].items()) if r is not None])
        add("slice_control_actions_total", "counter",
            "Controller steps by slice and action (cut, raise, hold).",
            ['slice_control_actions_total{slice="%s",action="%s"} %d' % (k[0], k[1], v)
             for k, v in sorted(control_actions.items())])

    if PROMETHEUS_URL:
        m = measured_mbps()
        add("slice_measured_mbps", "gauge",
            "Traffic each slice's UPF actually carried over the last 15 s, both directions.",
            ['slice_measured_mbps{sst="%d",sd="%s",slice="%s"} %.3f'
             % (s["sst"], s["sd"], s["name"], m.get(s["name"], 0.0)) for s in SLICES])
        add("slice_measurement_ok", "gauge",
            "1 when the last measured-load query to Prometheus succeeded.",
            ["slice_measurement_ok %d" % (1 if _measured["ok"] else 0)])

    adm, rej, done, failed, pre = [], [], [], [], []
    for (event, sn, cls, reason), count in sorted(counters.items()):
        if event == "preempted":
            pre.append('slice_preempted_total{sst_sd="%s",traffic_class="%s"} %d' % (sn, cls, count))
        if event == "completed":
            done.append('slice_flows_completed_total{sst_sd="%s",traffic_class="%s",outcome="%s"} %d'
                        % (sn, cls, reason, count))
        elif event == "exec_failed":
            failed.append('slice_flow_start_failed_total{sst_sd="%s",traffic_class="%s",reason="%s"} %d'
                          % (sn, cls, reason, count))
        if event == "admitted":
            adm.append('slice_admitted_total{sst_sd="%s",traffic_class="%s"} %d'
                       % (sn, cls, count))
        elif event == "rejected":
            rej.append('slice_rejected_total{sst_sd="%s",traffic_class="%s",reason="%s"} %d'
                       % (sn, cls, reason, count))
    add("slice_admitted_total", "counter",
        "Traffic demands admitted, by slice and traffic class.", adm or [])
    add("slice_rejected_total", "counter",
        "Traffic demands refused, by reason (capacity, latency, unknown_class, ...).", rej or [])
    add("slice_preempted_total", "counter",
        "Admitted demands stopped to make room for a higher-priority one (ARP pre-emption).", pre)
    if EXECUTE:
        add("slice_flows_completed_total", "counter",
            "Executed flows by outcome: met = >=95% of the admitted rate delivered with <=2% loss.",
            done)
        add("slice_flow_start_failed_total", "counter",
            "Admitted demands that could not be started (no device session on the slice).", failed)
        recent = flow_results[-200:]
        rows = []
        for s in SLICES:
            mine = [r for r in recent if r["slice"] == s["name"]]
            if mine:
                ratio = sum(min(1.0, r["delivered_mbps"] / r["requested_mbps"]) for r in mine) / len(mine)
                rows.append('slice_flow_delivery_ratio{slice="%s"} %.4f' % (s["name"], ratio))
        add("slice_flow_delivery_ratio", "gauge",
            "Mean delivered/admitted rate over the last 200 executed flows, per slice.", rows)

    out.append("# HELP slice_orchestrator_up 1 when the orchestrator served a scrape.")
    out.append("# TYPE slice_orchestrator_up gauge")
    out.append("slice_orchestrator_up 1")
    return "\n".join(out) + "\n"


# ─────────────────────────── HTTP ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        with _lock:
            if path == "/metrics":
                self._send(200, metrics(), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/state":
                expire_flows()
                self._send(200, json.dumps({
                    "slices": [{**s, "allocated_mbps": round(allocated(snssai(s)), 2),
                                "admission_limit_mbps": round(limit(s), 2)}
                               for s in SLICES],
                    "control": dict({k: control[k] for k in ("latency_ms", "short_ratio", "steps")},
                                    protect=CONTROL_PROTECT, stat=CONTROL_STAT, throttle=CONTROL_THROTTLE,
                                    recent_actions=control["log"][-10:]) if CONTROL else None,
                    "traffic_classes": TRAFFIC_CLASSES,
                    "active_flows": len(flows),
                    "subscribers_known": len(subscribers()),
                    "core": CORE,
                    "measured_mbps": measured_mbps() if PROMETHEUS_URL else None,
                    "measurement_ok": _measured["ok"] if PROMETHEUS_URL else None,
                    "executing": EXECUTE,
                    "recent_flows": flow_results[-10:],
                }, indent=2))
            elif path == "/flows":
                self._send(200, json.dumps(flow_results, indent=2))
            elif path == "/control":
                self._send(200, json.dumps(control["log"], indent=2))
            elif path == "/plan":
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                window = float(q.get("window", [PLAN_WINDOW])[0])
                demands = observed_demands(window)
                self._send(200, json.dumps({"window_s": window, "observed_demand": demands,
                                            **plan(demands)}, indent=2))
            elif path == "/":
                self._send(200, json.dumps({
                    "service": "slice-orchestrator",
                    "endpoints": ["/request (POST)", "/plan (GET observed, POST a demand set)",
                                  "/metrics", "/state", "/flows", "/control"],
                }, indent=2))
            else:
                self._send(404, '{"error":"not found"}')

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/request", "/plan", "/slices"):
            self._send(404, '{"error":"not found"}')
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, '{"error":"invalid JSON"}')
            return
        if path == "/plan":
            with _lock:
                result = plan(req.get("demands", []), float(req.get("headroom", PLAN_HEADROOM)))
            self._send(200, json.dumps(result, indent=2))
            return
        if path == "/slices":
            # The network orchestrator's switch (ADR-017): {"name": "mMTC", "active": false}.
            with _lock:
                s = next((x for x in SLICES if x["name"] == req.get("name")), None)
                if s is None or not isinstance(req.get("active"), bool):
                    self._send(400, '{"error":"need {\\"name\\": <slice>, \\"active\\": true|false}"}')
                    return
                s["active"] = req["active"]
                body = {"name": s["name"], "active": s["active"],
                        "active_flows": len([f for f in flows if f["snssai"] == snssai(s)]),
                        "allocated_mbps": round(allocated(snssai(s)), 2)}
            self._send(200, json.dumps(body))
            return
        supi = req.get("supi", "")
        cls = req.get("traffic_class", req.get("type", ""))
        with _lock:
            result = decide(supi, cls, req.get("mbps"))
        self._send(200 if result.get("admitted") else 409, json.dumps(result))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("slice-orchestrator on :%d — core %s, %d slices, %d traffic classes, measured=%s, execute=%s, control=%s"
          % (PORT, CORE, len(SLICES), len(TRAFFIC_CLASSES), bool(PROMETHEUS_URL), EXECUTE, CONTROL), flush=True)
    for s in SLICES:
        print("  %-6s %s  capacity %gMbps  latency<=%gms  5QI %d"
              % (s["name"], snssai(s), s["capacity_mbps"], s["max_latency_ms"], s["five_qi"]),
              flush=True)
    if CONTROL:
        print("  controller: protects %s %s (cut above %.0f%% of its budget), throttles %s, every %gs"
              % (CONTROL_PROTECT, CONTROL_STAT, 100 * CONTROL_HIGH, " ".join(CONTROL_THROTTLE),
                 CONTROL_INTERVAL), flush=True)
        threading.Thread(target=control_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
