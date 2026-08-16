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

Run with: scripts/start_orchestrator.sh
"""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SLICES_ENV = os.environ.get("SLICES_ENV", os.path.join(REPO, "deployments", "slices.env"))
PORT = int(os.environ.get("PORT", "9110"))
DB_CONTAINER = os.environ.get("DB_CONTAINER", "mongodb")
FLOW_TTL = int(os.environ.get("FLOW_TTL", "45"))          # seconds a demand holds capacity
SUB_REFRESH = int(os.environ.get("SUB_REFRESH", "60"))    # seconds between subscriber reloads

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
        })

    classes = {}
    for spec in env.get("TRAFFIC_CLASSES", "").split():
        parts = spec.split(":")
        if len(parts) == 3:
            classes[parts[0]] = {"mbps": float(parts[1]), "max_latency_ms": float(parts[2])}

    return slices, classes


SLICES, TRAFFIC_CLASSES = load_slices()


def snssai(s):
    return "%d/%s" % (s["sst"], s["sd"])


# ─────────────────────────── live subscriber state ───────────────────────────

_subs = {"map": {}, "ts": 0.0}


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
    try:
        out = subprocess.run(
            ["docker", "exec", "-i", DB_CONTAINER, "mongo", "free5gc", "--quiet", "--eval", js],
            capture_output=True, text=True, timeout=30,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except Exception:
        pass
    return {}


def subscribers():
    now = time.time()
    if now - _subs["ts"] > SUB_REFRESH or not _subs["map"]:
        m = load_subscribers()
        if m:
            _subs["map"] = m
        _subs["ts"] = now
    return _subs["map"]


# ─────────────────────────── allocation state ───────────────────────────

flows = []          # active demands holding capacity
counters = {}       # (event, snssai, cls, reason) -> count


def bump(event, sn="", cls="", reason=""):
    k = (event, sn, cls, reason)
    counters[k] = counters.get(k, 0) + 1


def expire_flows():
    now = time.time()
    global flows
    flows = [f for f in flows if f["expires"] > now]


def allocated(sn):
    return sum(f["mbps"] for f in flows if f["snssai"] == sn)


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

    fits_latency = [s for s in candidates if s["max_latency_ms"] <= need_latency]
    if not fits_latency:
        best = min(candidates, key=lambda s: s["max_latency_ms"])
        bump("rejected", snssai(best), cls, "latency")
        return {"admitted": False,
                "reason": "%s needs <=%gms; best permitted slice %s offers only %gms"
                          % (cls, need_latency, snssai(best), best["max_latency_ms"])}

    has_room = [s for s in fits_latency if allocated(snssai(s)) + need <= s["capacity_mbps"]]
    if not has_room:
        s = fits_latency[0]
        sn = snssai(s)
        bump("rejected", sn, cls, "capacity")
        return {"admitted": False,
                "reason": "slice %s (%s) full: %.1f/%.1f Mbps used, %s needs %.1f"
                          % (sn, s["name"], allocated(sn), s["capacity_mbps"], cls, need)}

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
        key=lambda s: (s["max_latency_ms"], s["capacity_mbps"] - allocated(snssai(s))),
    )
    sn = snssai(chosen)
    flows.append({"supi": supi, "cls": cls, "mbps": need, "snssai": sn,
                  "expires": time.time() + FLOW_TTL})
    bump("admitted", sn, cls)
    return {
        "admitted": True, "supi": supi, "traffic_class": cls,
        "slice": {"name": chosen["name"], "sst": chosen["sst"], "sd": chosen["sd"],
                  "5qi": chosen["five_qi"], "arp": chosen["arp"]},
        "mbps": need,
        "slice_used_mbps": round(allocated(sn), 2),
        "slice_capacity_mbps": chosen["capacity_mbps"],
    }


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

    adm, rej = [], []
    for (event, sn, cls, reason), count in sorted(counters.items()):
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
                    "slices": [{**s, "allocated_mbps": round(allocated(snssai(s)), 2)}
                               for s in SLICES],
                    "traffic_classes": TRAFFIC_CLASSES,
                    "active_flows": len(flows),
                    "subscribers_known": len(subscribers()),
                }, indent=2))
            elif path == "/":
                self._send(200, json.dumps({
                    "service": "slice-orchestrator",
                    "endpoints": ["/request (POST)", "/metrics", "/state"],
                }, indent=2))
            else:
                self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/request":
            self._send(404, '{"error":"not found"}')
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, '{"error":"invalid JSON"}')
            return
        supi = req.get("supi", "")
        cls = req.get("traffic_class", req.get("type", ""))
        with _lock:
            result = decide(supi, cls, req.get("mbps"))
        self._send(200 if result.get("admitted") else 409, json.dumps(result))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("slice-orchestrator on :%d — %d slices, %d traffic classes"
          % (PORT, len(SLICES), len(TRAFFIC_CLASSES)), flush=True)
    for s in SLICES:
        print("  %-6s %s  capacity %gMbps  latency<=%gms  5QI %d"
              % (s["name"], snssai(s), s["capacity_mbps"], s["max_latency_ms"], s["five_qi"]),
              flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
