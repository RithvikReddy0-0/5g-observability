#!/usr/bin/env python3
"""
netorch.py — the network orchestrator: lifecycle of the network slices on the Open5GS stack (ADR-017).

WHAT IT IS, AND HOW IT DIFFERS FROM THE SLICE ORCHESTRATOR
The slice orchestrator (tools/slice-orchestrator, ADR-016) works on TRAFFIC: which existing slice
serves a demand, at what priority, within what admission limit. This one works on the NETWORK:
whether a slice exists in a working state at all, and with how much compute. In the standards'
terms it plays the part of slice management (3GPP NSMF/NSSMF, TS 28.531) and of an NFV
orchestrator: it instantiates and terminates the network functions that make up a slice.

A slice here is: a gNB, a UPF and a data-network container of its own (ADR-011), its subscribers'
UEs in the UE container, and its admission switch in the slice orchestrator.

  activate    start the UPF and wait until the SMF has associated with it (PFCP), start the data
              network, start the gNB and wait for NG Setup with the AMF, bring the UEs back and wait
              for their PDU sessions, check the data path from the UEs, then let the slice
              orchestrator admit demands. Every step is checked; if one fails, or the whole
              activation misses its deadline, everything started is stopped again (rollback).
  deactivate  stop admitting, let admitted flows drain, deregister the UEs (which releases their
              sessions in the core), stop gNB, data network and UPF, and check that the other
              slices kept every UE.
  scale       set the CPU quota of the slice's gNB and UPF: compute is what the slices on this
              host actually compete for (ADR-016's diagnosis).
  auto        a closed loop on the slice orchestrator's /plan: an on-demand slice is activated when
              the demand it alone can serve appears, and deactivated after IDLE_SECONDS without it.

STATELESS BY DESIGN
The state of a slice is always DISCOVERED from the infrastructure — containers running, UEs parked
or up — never remembered. A restarted orchestrator, an engine restart or a manual `docker stop`
cannot leave it believing something false. Operations are serialised: one at a time.

Run: scripts/open5gs/start_netorch.sh      CLI: tools/network-orchestrator/netorch.py status|activate|...
"""

import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SLICES_ENV = os.environ.get("SLICES_ENV", os.path.join(REPO, "deployments", "slices.env"))
COMPOSE = os.environ.get("COMPOSE", os.path.join(REPO, "deployments", "open5gs", "docker-compose.yaml"))
PORT = int(os.environ.get("PORT", "9113"))
SLICE_ORCH = os.environ.get("SLICE_ORCH", "http://localhost:9111")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9091")
UE_CONTAINER = os.environ.get("UE_CONTAINER", "o5gs-ue")
SMF = os.environ.get("SMF_CONTAINER", "o5gs-smf")
OPS_LOG = os.environ.get("NETORCH_LOG", "/tmp/o5gs_netorch_ops.jsonl")

ACTIVATE_DEADLINE = float(os.environ.get("ACTIVATE_DEADLINE", "150"))  # whole activation, seconds
DRAIN_TIMEOUT = float(os.environ.get("DRAIN_TIMEOUT", "30"))           # admitted flows may finish
UE_UP_SHARE = float(os.environ.get("UE_UP_SHARE", "0.95"))             # UEs that must attach

AUTO = os.environ.get("NETORCH_AUTO", "0") == "1"
ON_DEMAND = os.environ.get("ON_DEMAND", "mMTC").split()      # slices the closed loop may switch
AUTO_INTERVAL = float(os.environ.get("AUTO_INTERVAL", "10"))
PLAN_WINDOW = float(os.environ.get("PLAN_WINDOW", "60"))     # demand window asked of /plan
IDLE_SECONDS = float(os.environ.get("IDLE_SECONDS", "120"))  # no demand this long -> deactivate
MIN_UP = float(os.environ.get("MIN_UP", "60"))               # never deactivate sooner after a change
# reactive: switch on when demand appears (the original). predictive: also learn the period between
# demand onsets — IoT devices report periodically — and switch on ACTIVATE_LEAD seconds before the
# next one is due, and do not switch off when the next one is closer than that (ADR-018). Predictive
# is the default: in simulation it refused 3.6 % of periodic IoT demand against reactive's 42 %, for
# about 9 points more time switched on; until it has seen three onsets it behaves reactively.
AUTO_POLICY = os.environ.get("AUTO_POLICY", "predictive")
ACTIVATE_LEAD = float(os.environ.get("ACTIVATE_LEAD", "30"))  # > activation time (12-25 s measured) + a tick


class Failure(Exception):
    pass


# ─────────────────────────── inventory ───────────────────────────

def load_inventory():
    """Slices from slices.env (the single slice definition) and the UE plan in the compose file."""
    env = {}
    for line in open(SLICES_ENV, encoding="utf-8"):
        m = re.match(r'\s*([A-Z0-9_]+)=("?)(.*?)\2\s*(#.*)?$', line)
        if m:
            env[m.group(1)] = m.group(3)
    counts = dict(x.split("=") for x in env.get("O5GS_UES", "").split())
    plan = re.search(r'UE_PLAN:\s*"([^"]+)"', open(COMPOSE, encoding="utf-8").read())
    gateways = {}
    for item in (plan.group(1).split() if plan else []):
        _, _, gw, name = item.split(":")
        gateways[name] = gw
    slices = []
    for letter in "ABCD":
        p = "SLICE_%s_" % letter
        if p + "SST" not in env or letter not in counts:
            continue
        name = env[p + "NAME"]
        low = name.lower()
        slices.append({
            "name": name, "sst": int(env[p + "SST"]), "sd": env[p + "SD"],
            "snssai_label": "%s-%s" % (env[p + "SST"], env[p + "SD"]),
            "ues": int(counts[letter]), "gateway": gateways.get(name, ""),
            "pool": ".".join(gateways.get(name, "").split(".")[:2]) + ".",
            "upf": "o5gs-upf-%s" % low, "dn": "o5gs-dn-%s" % low, "gnb": "o5gs-gnb-%s" % low,
        })
    return slices


SLICES = load_inventory()
BY_NAME = {s["name"]: s for s in SLICES}


# ─────────────────────────── infrastructure access ───────────────────────────

def sh(args, timeout=30, stdin=None):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def inspect(container, fmt):
    rc, out = sh(["docker", "inspect", "-f", fmt, container], timeout=10)
    return out.strip() if rc == 0 else ""


def running(container):
    return inspect(container, "{{.State.Running}}") == "true"


def cpus_of(container):
    v = inspect(container, "{{.HostConfig.NanoCpus}}")
    return int(v) / 1e9 if v.isdigit() else 0.0


def ue_slice(op, name=""):
    """Run ue-slice.sh in the UE container. -> list of output lines."""
    rc, out = sh(["docker", "exec", UE_CONTAINER, "sh", "/etc/ueransim/ue-slice.sh", op] + ([name] if name else []),
                 timeout=180)
    if rc != 0:
        raise Failure("ue-slice.sh %s %s failed: %s" % (op, name, out.strip()[-200:]))
    return [l for l in out.splitlines() if l.strip()]


def ue_status():
    """{slice: {"parked": bool, "total": n, "up": n}} from the UE container itself."""
    out = {}
    try:
        for line in ue_slice("status"):
            parts = line.split()
            if len(parts) == 4:
                out[parts[0]] = {"parked": parts[1] == "parked", "total": int(parts[2]), "up": int(parts[3])}
    except Failure:
        pass
    return out


def prom(query):
    try:
        url = PROMETHEUS_URL.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())["data"]["result"]
    except Exception:
        return None


def core_sessions():
    """{snssai label: sessions the SMF holds} — exact per slice (docs/open5gs.md)."""
    res = prom("fivegs_smffunction_sm_sessionnbr")
    if res is None:
        return {}
    return {r["metric"].get("snssai", ""): int(float(r["value"][1])) for r in res}


def orch(method, path, body=None):
    """The slice orchestrator's API; None when it is not running (it is optional for lifecycle)."""
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(SLICE_ORCH.rstrip("/") + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return None
    except Exception:
        return None


def since_logs(container, since, pattern):
    rc, out = sh(["docker", "logs", "--since", since, container], timeout=20)
    return pattern in out


def wait_for(check, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return
        time.sleep(1)
    raise Failure("%s not reached within %.0fs" % (what, timeout))


# ─────────────────────────── observed state ───────────────────────────

_cache = {"t": 0.0, "slices": {}}
_busy = {}          # slice -> "ACTIVATING" | "DEACTIVATING" | "SCALING" while an operation runs


def observe(force=False):
    """{slice: observed state}, cached for 15 s — observing runs ~120 small processes, and a
    measurement tool must not load the host it measures (ADR-016). Always discovered, never
    remembered."""
    if not force and time.time() - _cache["t"] < 15:
        return _cache["slices"]
    ues = ue_status()
    core = core_sessions()
    out = {}
    for s in SLICES:
        nf = {k: running(s[k]) for k in ("upf", "dn", "gnb")}
        u = ues.get(s["name"], {"parked": False, "total": s["ues"], "up": 0})
        if all(nf.values()) and not u["parked"] and u["up"] >= math.ceil(UE_UP_SHARE * s["ues"]):
            state = "ACTIVE"
        elif not any(nf.values()) and u["parked"] and u["up"] == 0:
            state = "DORMANT"
        else:
            state = "DEGRADED"
        out[s["name"]] = {
            "state": _busy.get(s["name"], state), "settled_state": state, "nfs_running": nf,
            "parked": u["parked"], "ues_up": u["up"], "ues_total": s["ues"],
            "core_sessions": core.get(s["snssai_label"]),
            # CPU quota of the gNB and UPF; 0 = unlimited (no quota, or one of every host core).
            "cpus": (lambda c: 0.0 if c == 0 or c >= HOST_CPUS else c)(max(cpus_of(s["gnb"]), cpus_of(s["upf"])))
                    if nf["gnb"] or nf["upf"] else None,
        }
    _cache.update(t=time.time(), slices=out)
    return out


# ─────────────────────────── operations ───────────────────────────

_op_lock = threading.Lock()
ops = []                 # recent operations, newest last
op_counts = {}           # (slice, op, result) -> n
last_change = {}         # slice -> time of its last completed lifecycle change


class Op:
    def __init__(self, kind, name, reason, **args):
        self.id = "%s-%s-%x" % (kind, name, random.getrandbits(20))
        self.kind, self.name, self.reason, self.args = kind, name, reason, args
        self.steps, self.result, self.error, self.warnings = [], "running", None, []
        self.t0 = time.time()
        self.t_end = None

    def step(self, label, fn):
        t = time.time()
        detail = fn()
        self.steps.append({"step": label, "seconds": round(time.time() - t, 2), "detail": detail})
        return detail

    def remaining(self):
        return ACTIVATE_DEADLINE - (time.time() - self.t0)

    def as_dict(self):
        return {"id": self.id, "op": self.kind, "slice": self.name, "reason": self.reason, "args": self.args,
                "result": self.result, "error": self.error, "warnings": self.warnings, "seconds": round((self.t_end or time.time()) - self.t0, 2),
                "started": time.strftime("%H:%M:%S", time.localtime(self.t0)), "steps": self.steps}


def _finish(op, result, error=None):
    op.result, op.error, op.t_end = result, error, time.time()
    k = (op.name, op.kind, result)
    op_counts[k] = op_counts.get(k, 0) + 1
    if result in ("done", "rolled_back"):
        last_change[op.name] = time.time()
    try:
        with open(OPS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(op.as_dict()) + "\n")
    except OSError:
        pass
    print("NETORCH %-10s %-6s %-11s %6.1fs  %s" % (op.kind, op.name, result, op.t_end - op.t0, error or op.reason),
          flush=True)


def activate(op):
    s = BY_NAME[op.name]
    before = {k: running(s[k]) for k in ("upf", "dn", "gnb")}
    started = []
    unparked = False

    def start(key):
        c = s[key]
        if not before[key]:
            rc, out = sh(["docker", "start", c], timeout=30)
            if rc != 0:
                raise Failure("docker start %s: %s" % (c, out.strip()[-120:]))
            started.append(c)
        return inspect(c, "{{.State.StartedAt}}")

    try:
        def upf():
            since = start("upf")
            ip = inspect(s["upf"], "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")
            # The association must be NEWER than this UPF process: an older one looks the same
            # and loses every session ("Cannot find PFCP-Node", ADR-014).
            wait_for(lambda: since_logs(SMF, since, "PFCP associated [%s]" % ip),
                     min(60, op.remaining()), "PFCP association of %s with the SMF" % s["upf"])
            return "associated with the SMF (%s)" % ip
        op.step("upf", upf)

        op.step("data network", lambda: (start("dn"), "running")[1])

        def gnb():
            since = start("gnb")
            wait_for(lambda: since_logs(s["gnb"], since, "NG Setup procedure is successful"),
                     min(60, op.remaining()), "NG Setup of %s with the AMF" % s["gnb"])
            return "NG Setup done"
        op.step("gnb", gnb)

        def ues():
            nonlocal unparked
            unparked = True
            line = ue_slice("unpark", s["name"])[-1].split()          # unpark <slice> <UEs> <up> <s>
            total, up = int(line[2]), int(line[3])
            if up < math.ceil(UE_UP_SHARE * total):
                raise Failure("only %d of %d UEs attached" % (up, total))
            if op.remaining() < 0:
                raise Failure("activation deadline of %.0fs passed while UEs attached" % ACTIVATE_DEADLINE)
            return "%d/%d UEs with a PDU session" % (up, total)
        op.step("ues", ues)

        op.step("data path", lambda: datapath(s))

        def admit():
            r = orch("POST", "/slices", {"name": s["name"], "active": True})
            return "slice orchestrator admits" if r else "slice orchestrator not running (nothing to admit)"
        op.step("admit", admit)
        if op.remaining() < 0:
            raise Failure("activation took longer than its %.0fs deadline" % ACTIVATE_DEADLINE)
        _finish(op, "done")
    except Failure as e:
        # Roll back to where the slice was: everything this operation started is stopped again.
        rb = []
        orch("POST", "/slices", {"name": s["name"], "active": False})
        if unparked:
            try:
                ue_slice("park", s["name"])
                rb.append("UEs parked")
            except Failure as e2:
                rb.append("park failed: %s" % e2)
        for c in reversed(started):
            sh(["docker", "stop", "-t", "5", c], timeout=30)
            rb.append("stopped %s" % c)
        op.steps.append({"step": "rollback", "seconds": 0, "detail": "; ".join(rb) or "nothing to undo"})
        _finish(op, "rolled_back", str(e))


def datapath(s):
    """Ping the slice's UPF gateway from up to three of its UEs, through their own interfaces."""
    rc, out = sh(["docker", "exec", UE_CONTAINER, "ip", "-4", "-o", "addr", "show"], timeout=10)
    tuns = [m.group(1) for m in re.finditer(r"^\d+:\s+(uesimtun\d+)\s+inet\s+([\d.]+)/", out, re.M)
            if m.group(2).startswith(s["pool"])]
    if not tuns:
        raise Failure("no UE interface on %s*" % s["pool"])
    sample = random.sample(tuns, min(3, len(tuns)))
    ok = sum(sh(["docker", "exec", UE_CONTAINER, "ping", "-I", t, "-c", "2", "-W", "2", s["gateway"]],
                timeout=15)[0] == 0 for t in sample)
    if ok < max(1, len(sample) - 1):
        raise Failure("data path: %d of %d UEs reached %s through the UPF" % (ok, len(sample), s["gateway"]))
    return "%d/%d sampled UEs reach %s through the UPF" % (ok, len(sample), s["gateway"])


def deactivate(op):
    s = BY_NAME[op.name]
    others_before = {k: v["up"] for k, v in ue_status().items() if k != s["name"]}
    try:
        def stop_admitting():
            r = orch("POST", "/slices", {"name": s["name"], "active": False})
            return ("slice orchestrator admits nothing more; %d flows active" % r.get("active_flows", 0)
                    if r else "slice orchestrator not running")
        op.step("stop admitting", stop_admitting)

        def drain():
            end = time.time() + DRAIN_TIMEOUT
            while time.time() < end:
                st = orch("GET", "/state")
                if not st:
                    return "no slice orchestrator: nothing to drain"
                a = next((x.get("allocated_mbps", 0) for x in st.get("slices", []) if x["name"] == s["name"]), 0)
                if a <= 0:
                    return "no admitted flows left"
                time.sleep(1)
            return "drain timeout after %.0fs: remaining flows are cut" % DRAIN_TIMEOUT
        op.step("drain", drain)

        def park():
            line = ue_slice("park", s["name"])[-1].split()
            return "%s UEs deregistered and stopped, %s still up" % (line[2], line[3])
        op.step("ues", park)

        def stop_nfs():
            for key in ("gnb", "dn", "upf"):
                rc, out = sh(["docker", "stop", "-t", "5", s[key]], timeout=30)
                if rc != 0:
                    raise Failure("docker stop %s: %s" % (s[key], out.strip()[-120:]))
            return "gNB, data network and UPF stopped"
        op.step("network functions", stop_nfs)

        def verify():
            after = ue_status()
            mine = after.get(s["name"], {}).get("up", 0)
            if mine:
                raise Failure("%d UEs of %s still up" % (mine, s["name"]))
            lost = {k: v - after.get(k, {}).get("up", 0) for k, v in others_before.items()
                    if after.get(k, {}).get("up", 0) < v}
            # The core's own count, once Prometheus has scraped the SMF again.
            core = None
            for _ in range(20):
                core = core_sessions().get(s["snssai_label"])
                if core is not None and core <= max(1, s["ues"] // 50):
                    break
                time.sleep(1)
            detail = "other slices kept every UE" if not lost else "other slices lost UEs: %s" % lost
            if lost:
                op.warnings.append("other slices lost UEs: %s" % lost)
            # Under heavy load some deregistrations do not reach the core before the UEs stop
            # (11 of 70 in one run): those sessions stay counted until the UEs re-attach. Not a
            # failure — the slice IS down — but it must be visible, not buried in a detail line.
            if core is not None and core > max(1, s["ues"] // 50):
                op.warnings.append("core still holds %d stale session(s) on %s until its UEs re-attach"
                                   % (core, s["name"]))
            return "%s; core still holds %s session(s) on %s" % (detail, core, s["name"])
        op.step("verify", verify)
        _finish(op, "done")
    except Failure as e:
        _finish(op, "failed", str(e))


HOST_CPUS = os.cpu_count() or 1


def scale(op):
    s = BY_NAME[op.name]
    cpus = float(op.args["cpus"])
    # `docker update --cpus 0` is accepted and changes nothing — found when "unlimited" left a
    # 1.5-CPU quota in place while the operation reported success. Unlimited is therefore a quota
    # of every host core, and the quota read back must be the one asked for.
    target = HOST_CPUS if cpus <= 0 else min(cpus, HOST_CPUS)

    def update():
        before = {k: cpus_of(s[k]) for k in ("gnb", "upf")}
        for k in ("gnb", "upf"):
            rc, out = sh(["docker", "update", "--cpus", "%g" % target, s[k]], timeout=20)
            if rc != 0:
                raise Failure("docker update %s: %s" % (s[k], out.strip()[-120:]))
        after = {k: cpus_of(s[k]) for k in ("gnb", "upf")}
        if any(abs(v - target) > 0.01 for v in after.values()):
            raise Failure("asked for %g CPUs, the containers have %s" % (target, after))
        label = "unlimited (all %d cores)" % HOST_CPUS if target >= HOST_CPUS else "%g CPUs" % target
        return "CPU quota %s -> %s: %s" % (before, after, label)
    try:
        op.step("cpu quota", update)
        _finish(op, "done")
    except Failure as e:
        _finish(op, "failed", str(e))


RUNNERS = {"activate": activate, "deactivate": deactivate, "scale": scale}
BUSY_LABEL = {"activate": "ACTIVATING", "deactivate": "DEACTIVATING", "scale": "SCALING"}


def submit(kind, name, reason="manual", wait=False, **args):
    """Start an operation in the background. -> (Op, None) or (None, why it cannot start)."""
    if name not in BY_NAME:
        return None, "unknown slice %r" % name
    if not _op_lock.acquire(blocking=False):
        return None, "another operation is running"
    op = Op(kind, name, reason, **args)
    ops.append(op)
    del ops[:-100]
    _busy[name] = BUSY_LABEL[kind]

    def run():
        try:
            RUNNERS[kind](op)
        except Exception as e:                    # never leave the lock held
            _finish(op, "failed", "unexpected: %s" % e)
        finally:
            _busy.pop(name, None)
            _cache["t"] = 0
            _op_lock.release()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    if wait:
        t.join()
    return op, None


# ─────────────────────────── closed loop on /plan ───────────────────────────

loop = {"enabled": AUTO, "last_plan": None, "last_need": {}, "ticks": 0, "decisions": []}


def next_onset(name):
    """When the next demand onset is due: the last onset plus the median gap between recent onsets.
    None until three onsets (two gaps) have been seen."""
    on = loop.get("onsets", {}).get(name, [])
    if len(on) < 3:
        return None
    gaps = sorted(b - a for a, b in zip(on, on[1:]))
    period = gaps[len(gaps) // 2]
    loop.setdefault("period", {})[name] = period
    return on[-1] + period


def auto_tick(now=None, plan=None, states=None):
    """One closed-loop step. Pure apart from submit(), so tests can drive it.
    -> list of (op, slice, reason) it decided."""
    now = time.time() if now is None else now
    plan = plan if plan is not None else orch("GET", "/plan?window=%d" % PLAN_WINDOW)
    if not plan or "slices" not in plan:
        return []
    states = states if states is not None else observe(force=True)
    loop["last_plan"] = {x["slice"]: x["demand_mbps"] for x in plan["slices"]}
    needed = {x["slice"]: x["needed"] and x["demand_mbps"] > 0 for x in plan["slices"]}
    decided = []
    for name in ON_DEMAND:
        st = states.get(name, {}).get("state")
        prev = loop.setdefault("prev_need", {}).get(name, False)
        if needed.get(name) and not prev:
            loop.setdefault("onsets", {}).setdefault(name, []).append(now)   # demand just began
            del loop["onsets"][name][:-8]
        loop["prev_need"][name] = bool(needed.get(name))
        if needed.get(name):
            loop["last_need"][name] = now
        idle_for = now - loop["last_need"].setdefault(name, now)
        settled_for = now - last_change.get(name, 0)
        nxt = next_onset(name) if AUTO_POLICY == "predictive" else None
        due_soon = nxt is not None and now >= nxt - ACTIVATE_LEAD and now < nxt + IDLE_SECONDS
        if st == "DORMANT" and needed.get(name):
            decided.append(("activate", name, "demand: %.2f Mbps needs %s" % (loop["last_plan"][name], name)))
        elif st == "DORMANT" and due_soon:
            decided.append(("activate", name, "predicted demand in %.0fs (period %.0fs)"
                            % (nxt - now, loop["period"][name])))
        elif (st == "ACTIVE" and not needed.get(name) and idle_for >= IDLE_SECONDS
              and settled_for >= MIN_UP and not due_soon):
            decided.append(("deactivate", name, "idle: no demand for %.0fs" % idle_for))
    # Keep the slice orchestrator's switches in line with what is really deployed (it may have
    # restarted and forgotten them).
    for name, st in states.items():
        if st.get("settled_state") in ("ACTIVE", "DORMANT") and name not in _busy:
            orch("POST", "/slices", {"name": name, "active": st["settled_state"] == "ACTIVE"})
    for kind, name, why in decided:
        op, err = submit(kind, name, reason="auto " + why)
        loop["decisions"].append({"t": time.strftime("%H:%M:%S", time.localtime(now)), "op": kind,
                                  "slice": name, "reason": why, "started": bool(op), "error": err})
        del loop["decisions"][:-100]
    loop["ticks"] += 1
    return decided


def auto_loop():
    while True:
        time.sleep(AUTO_INTERVAL)
        if loop["enabled"]:
            try:
                auto_tick()
            except Exception as e:
                print("auto tick failed: %s" % e, flush=True)


# ─────────────────────────── metrics and HTTP ───────────────────────────

STATES = ("ACTIVE", "DORMANT", "DEGRADED", "ACTIVATING", "DEACTIVATING", "SCALING")


def metrics():
    obs = observe()
    out = []

    def add(name, typ, help_text, rows):
        out.append("# HELP %s %s" % (name, help_text))
        out.append("# TYPE %s %s" % (name, typ))
        out.extend(rows)

    add("netorch_slice_state", "gauge", "1 for the slice's current lifecycle state.",
        ['netorch_slice_state{slice="%s",state="%s"} %d' % (n, st, 1 if o["state"] == st else 0)
         for n, o in obs.items() for st in STATES])
    add("netorch_slice_active", "gauge", "1 when the slice is deployed and serving (ACTIVE).",
        ['netorch_slice_active{slice="%s"} %d' % (n, 1 if o["settled_state"] == "ACTIVE" else 0) for n, o in obs.items()])
    add("netorch_slice_ues_up", "gauge", "The slice's UEs holding a PDU session.",
        ['netorch_slice_ues_up{slice="%s"} %d' % (n, o["ues_up"]) for n, o in obs.items()])
    add("netorch_slice_nf_running", "gauge", "1 when the slice's network function container runs.",
        ['netorch_slice_nf_running{slice="%s",nf="%s"} %d' % (n, nf, 1 if r else 0)
         for n, o in obs.items() for nf, r in o["nfs_running"].items()])
    add("netorch_slice_cpus", "gauge", "CPU quota of the slice's gNB and UPF (0 = unlimited).",
        ['netorch_slice_cpus{slice="%s"} %g' % (n, o["cpus"] or 0) for n, o in obs.items()])
    add("netorch_operations_total", "counter", "Lifecycle operations by slice, kind and result.",
        ['netorch_operations_total{slice="%s",op="%s",result="%s"} %d' % (k[0], k[1], k[2], v)
         for k, v in sorted(op_counts.items())])
    last = {}
    for o in ops:
        if o.result != "running":
            last[(o.name, o.kind)] = o
    add("netorch_operation_seconds", "gauge", "Duration of the last completed operation per slice and kind.",
        ['netorch_operation_seconds{slice="%s",op="%s",result="%s"} %.2f' % (k[0], k[1], o.result, o.t_end - o.t0)
         for k, o in sorted(last.items())])
    add("netorch_auto_enabled", "gauge", "1 when the closed loop on /plan is on.",
        ["netorch_auto_enabled %d" % (1 if loop["enabled"] else 0)])
    out.append("netorch_up 1")
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/metrics":
            self._send(200, metrics(), "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/slices":
            self._send(200, {"slices": observe(force=True), "busy": dict(_busy)})
        elif path == "/ops":
            self._send(200, [o.as_dict() for o in ops])
        elif path.startswith("/ops/"):
            o = next((o for o in ops if o.id == path[5:]), None)
            self._send(200 if o else 404, o.as_dict() if o else {"error": "unknown op"})
        elif path == "/auto":
            self._send(200, {"enabled": loop["enabled"], "on_demand": ON_DEMAND, "idle_seconds": IDLE_SECONDS,
                             "plan_window": PLAN_WINDOW, "last_plan": loop["last_plan"],
                             "decisions": loop["decisions"][-20:]})
        elif path == "/":
            self._send(200, {"service": "network-orchestrator", "slices": [s["name"] for s in SLICES],
                             "endpoints": ["GET /slices", "POST /slices/<name>/activate|deactivate",
                                           "POST /slices/<name>/scale {\"cpus\": x}", "GET|POST /auto",
                                           "GET /ops", "GET /metrics"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, {"error": "invalid JSON"})
            return
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == ["auto"]:
            loop["enabled"] = bool(body.get("enabled"))
            self._send(200, {"enabled": loop["enabled"]})
            return
        if len(parts) == 3 and parts[0] == "slices" and parts[2] in RUNNERS:
            args = {}
            if parts[2] == "scale":
                if not isinstance(body.get("cpus"), (int, float)) or body["cpus"] < 0:
                    self._send(400, {"error": "scale needs {\"cpus\": number >= 0}"})
                    return
                args["cpus"] = body["cpus"]
            op, err = submit(parts[2], parts[1], reason=body.get("reason", "manual"), **args)
            self._send(202 if op else 409, op.as_dict() if op else {"error": err})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


# ─────────────────────────── CLI ───────────────────────────

def cli(argv):
    """netorch.py status | activate <slice> | deactivate <slice> | scale <slice> <cpus> | auto on|off.
    Talks to the running service, and waits for an operation to finish."""
    base = "http://localhost:%d" % PORT

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode()
                                     if method == "POST" else None, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        if argv[0] == "status":
            _, r = call("GET", "/slices")
            for n, o in r["slices"].items():
                print("%-6s %-12s UEs %3d/%-3d  core sessions %-4s  NFs %s  cpus %s"
                      % (n, o["state"], o["ues_up"], o["ues_total"], o["core_sessions"],
                         " ".join(k for k, v in o["nfs_running"].items() if v) or "-", o["cpus"]))
            return 0
        if argv[0] == "auto":
            print(call("POST", "/auto", {"enabled": argv[1] == "on"})[1])
            return 0
        body = {"cpus": float(argv[2])} if argv[0] == "scale" else {}
        code, r = call("POST", "/slices/%s/%s" % (argv[1], argv[0]), body)
        if code != 202:
            print("refused: %s" % r.get("error"))
            return 1
        while r["result"] == "running":
            time.sleep(1)
            r = call("GET", "/ops/" + r["id"])[1]
        for st in r["steps"]:
            print("  %-18s %6.1fs  %s" % (st["step"], st["seconds"], st["detail"]))
        for w in r.get("warnings", []):
            print("  WARNING: %s" % w)
        print("%s %s: %s in %.1fs%s" % (r["op"], r["slice"], r["result"], r["seconds"],
                                       " — " + r["error"] if r["error"] else ""))
        return 0 if r["result"] == "done" else 1
    except (IndexError, ValueError):
        print(cli.__doc__)
        return 2
    except urllib.error.URLError:
        print("network orchestrator not running: scripts/open5gs/start_netorch.sh")
        return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--serve":
        sys.exit(cli(sys.argv[1:]))
    print("network-orchestrator on :%d — slices %s, auto=%s (on-demand %s, idle %gs)"
          % (PORT, ", ".join("%s (%d UEs)" % (s["name"], s["ues"]) for s in SLICES), loop["enabled"],
             " ".join(ON_DEMAND), IDLE_SECONDS), flush=True)
    threading.Thread(target=auto_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
