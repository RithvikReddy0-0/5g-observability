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
# sst/sd=device-pool-prefix:data-network-ip:data-network-container — where each slice's device
# sessions live and where its traffic goes (a per-slice data network reached through that
# slice's UPF, ADR-012).
SLICE_PATHS = dict(
    item.split("=", 1) for item in os.environ.get(
        "SLICE_PATHS",
        "1/010203=10.45.:10.53.0.51:o5gs-dn-embb 2/112233=10.46.:10.53.0.52:o5gs-dn-urllc "
        "3/334455=10.47.:10.53.0.53:o5gs-dn-mmtc").split())

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

# Deliverable capacity, if calibrated below policy. The policy value is kept for /state.
for _s in SLICES:
    _s["policy_capacity_mbps"] = _s["capacity_mbps"]
    if _s["name"] in CAPACITY_OVERRIDE:
        _s["capacity_mbps"] = min(_s["capacity_mbps"], CAPACITY_OVERRIDE[_s["name"]])


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
    db = "free5gc"
    if CORE == "open5gs":
        # Open5GS keeps every permitted S-NSSAI in subscribers.slice[]; the IMSI has no prefix.
        db = "open5gs"
        js = (
            'var o={};'
            'db.subscribers.find({},{_id:0,imsi:1,slice:1}).forEach(function(d){'
            '  var out=[];'
            '  (d.slice||[]).forEach(function(s){var k=s.sst+"/"+(s.sd||"");'
            '      if(out.indexOf(k)<0){out.push(k);}});'
            '  if(out.length){o["imsi-"+d.imsi]=out;}'
            '});'
            'print(JSON.stringify(o));'
        )
    try:
        out = subprocess.run(
            ["docker", "exec", "-i", DB_CONTAINER, "mongo", db, "--quiet", "--eval", js],
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

    # Protect premium capacity. A demand belongs on the LEAST specialised slices that meet its
    # latency need (the loosest guarantee still good enough). If those are full it is refused —
    # it does not spill into a tighter slice. Found on real traffic: with eMBB full, video was
    # admitted onto URLLC and took 16 of its 20 Mbps, leaving 4 for the control traffic URLLC
    # exists for. SPILLOVER=1 restores the permissive behaviour deliberately.
    if not SPILLOVER:
        loosest = max(s["max_latency_ms"] for s in fits_latency)
        fits_latency = [s for s in fits_latency if s["max_latency_ms"] == loosest]

    has_room = [s for s in fits_latency if in_use(s) + need <= s["capacity_mbps"]]
    if not has_room:
        s = fits_latency[0]
        sn = snssai(s)
        bump("rejected", sn, cls, "capacity")
        return {"admitted": False,
                "reason": "slice %s (%s) full: %.1f/%.1f Mbps in use (admitted %.1f, measured %.1f), %s needs %.1f"
                          % (sn, s["name"], in_use(s), s["capacity_mbps"], allocated(sn),
                             measured_mbps().get(s["name"], 0.0), cls, need)}

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
        key=lambda s: (s["max_latency_ms"], s["capacity_mbps"] - in_use(s)),
    )
    sn = snssai(chosen)
    flow = {"id": "%x" % random.getrandbits(32), "supi": supi, "cls": cls, "mbps": need,
            "snssai": sn, "slice": chosen["name"],
            "expires": time.time() + (FLOW_SECONDS + 30 if EXECUTE else FLOW_TTL)}
    flows.append(flow)
    bump("admitted", sn, cls)
    execution = None
    if EXECUTE:
        execution = start_flow(flow)
    return {
        "admitted": True, "supi": supi, "traffic_class": cls,
        "slice": {"name": chosen["name"], "sst": chosen["sst"], "sd": chosen["sd"],
                  "5qi": chosen["five_qi"], "arp": chosen["arp"]},
        "mbps": need,
        "slice_used_mbps": round(allocated(sn), 2),
        "slice_measured_mbps": round(measured_mbps().get(chosen["name"], 0.0), 2),
        "slice_capacity_mbps": chosen["capacity_mbps"],
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
    port = next(p for p in range(5400, 5700) if p not in _ports)
    _ports.add(port)
    busy_devices[ip] = busy_devices.get(ip, 0) + 1
    flow.update({"device": ip, "port": port})
    threading.Thread(target=run_flow, args=(flow,), daemon=True).start()
    return {"started": True, "device": ip, "seconds": FLOW_SECONDS, "id": flow["id"]}


def run_flow(flow):
    """UDP at exactly the admitted bitrate, downlink, through the slice's gNB and UPF.
    UDP rather than TCP: TCP expands to fill whatever it finds, which would measure the path,
    not whether the admitted rate was delivered."""
    _, gw, upf = SLICE_PATHS[flow["snssai"]].split(":")   # gw/upf: the slice's data network
    port, ip = flow["port"], flow["device"]
    outcome, delivered, loss = "failed", 0.0, 100.0
    try:
        subprocess.run(["docker", "exec", upf, "iperf3", "-s", "-B", gw, "-p", str(port),
                        "-1", "-D"], capture_output=True, timeout=15)
        for _ in range(25):
            ok = subprocess.run(["docker", "exec", upf, "sh", "-c",
                                 "ss -ltn | grep -q '%s:%d'" % (gw, port)],
                                capture_output=True, timeout=10).returncode == 0
            if ok:
                break
            time.sleep(0.2)
        out = subprocess.run(
            ["docker", "exec", UE_CONTAINER, "iperf3", "-u", "-c", gw, "-B", ip, "-p", str(port),
             "-b", "%gM" % flow["mbps"], "-t", str(FLOW_SECONDS), "-R", "-J"],
            capture_output=True, text=True, timeout=FLOW_SECONDS + 30).stdout
        end = json.loads(out)["end"]["sum"]
        loss = end.get("lost_percent", 0.0)
        # With -R, bits_per_second is the sender's rate; what the device received is that
        # minus the loss it counted.
        delivered = end["bits_per_second"] * (1 - loss / 100.0) / 1e6
        # "met": the network delivered what was admitted (>= 95 % of the rate, <= 2 % loss).
        outcome = "met" if delivered >= 0.95 * flow["mbps"] and loss <= 2.0 else "short"
    except Exception as exc:
        flow["error"] = str(exc)[:120]
    finally:
        with _lock:
            _ports.discard(port)
            busy_devices[ip] = max(0, busy_devices.get(ip, 1) - 1)
            flow["expires"] = 0                          # capacity released when traffic ends
            bump("completed", flow["snssai"], flow["cls"], outcome)
            flow_results.append({"id": flow["id"], "cls": flow["cls"], "slice": flow["slice"],
                                 "device": ip, "requested_mbps": flow["mbps"],
                                 "delivered_mbps": round(delivered, 3),
                                 "loss_percent": round(loss, 3), "outcome": outcome,
                                 "error": flow.get("error"),
                                 "finished": time.strftime("%H:%M:%S")})
            del flow_results[:-500]


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

    if PROMETHEUS_URL:
        m = measured_mbps()
        add("slice_measured_mbps", "gauge",
            "Traffic each slice's UPF actually carried over the last 15 s, both directions.",
            ['slice_measured_mbps{sst="%d",sd="%s",slice="%s"} %.3f'
             % (s["sst"], s["sd"], s["name"], m.get(s["name"], 0.0)) for s in SLICES])
        add("slice_measurement_ok", "gauge",
            "1 when the last measured-load query to Prometheus succeeded.",
            ["slice_measurement_ok %d" % (1 if _measured["ok"] else 0)])

    adm, rej, done, failed = [], [], [], []
    for (event, sn, cls, reason), count in sorted(counters.items()):
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
                    "slices": [{**s, "allocated_mbps": round(allocated(snssai(s)), 2)}
                               for s in SLICES],
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
            elif path == "/":
                self._send(200, json.dumps({
                    "service": "slice-orchestrator",
                    "endpoints": ["/request (POST)", "/metrics", "/state", "/flows"],
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
    print("slice-orchestrator on :%d — core %s, %d slices, %d traffic classes, measured=%s, execute=%s"
          % (PORT, CORE, len(SLICES), len(TRAFFIC_CLASSES), bool(PROMETHEUS_URL), EXECUTE), flush=True)
    for s in SLICES:
        print("  %-6s %s  capacity %gMbps  latency<=%gms  5QI %d"
              % (s["name"], snssai(s), s["capacity_mbps"], s["max_latency_ms"], s["five_qi"]),
              flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
