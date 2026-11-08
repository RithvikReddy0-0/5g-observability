#!/usr/bin/env python3
"""
experiment.py — does the packet scheduler (ADR-019) do its job? Measured on the live stack.

  A  isolation   eMBB offers 400 Mbps into a 200 Mbps cell. URLLC (both ARP tiers), mMTC and an
                 unloaded eMBB device are probed every 20/200/100 ms. Modes: off (no shaping, the
                 stack as it was), fifo (the same cell, one drop-tail queue: a shared cell with no
                 scheduler) and sliced. Two rounds, the second in reverse order. Plus an idle run.
  B  ARP tiers   six standard URLLC devices are flooded at their 20 Mbps AMBR into a 100 Mbps
                 cell. A critical device and the seventh standard device are probed. fifo vs sliced.
  C  shares      eMBB (160 Mbps offered) and 40 mMTC devices (40 Mbps offered) share a 60 Mbps
                 cell; mMTC's guaranteed share is swept 5..25 %. Delivered rates against the shares.
  D  cell-bound  A, at a load the CPU can carry: eMBB offers 150 Mbps into a 100 Mbps cell. In A the
                 host's CPU saturates at 400 Mbps (load average ~10 on 12 threads), so URLLC's tail
                 there is CPU contention, which no queue discipline can fix; D isolates what the
                 scheduler itself does. Two rounds of off / fifo / sliced.
Parts can be chosen: PARTS=D (default ABCD).

Load is real UDP through each slice's DN, UPF and gNB (flowgen.py, paced, AMBR-limited at the gNB).
Every run also counts the radio-link failures it caused (UE logs) and checks every UE's data path
afterwards. A UE that suffered a radio-link failure keeps its interface but loses its data path —
UERANSIM's gNB drops the session context and never restores it — so the stack is restored
(scripts/open5gs/start_ues.sh) before the next run whenever any UE is left without one: no run
may inherit the damage of the one before.
Latency is ping RTT from the device's own PDU-session interface to its UPF: the request goes up
and the reply comes back down through the cell's downlink queue. Per-class bytes and drops come
from the scheduler's own counters. The scheduler is restored from slices.env at the end.

Writes docs/evidence/open5gs-scheduler/<UTC timestamp>/: results.json, summary.md, results.xlsx.
Run: scripts/open5gs/scheduler_experiment.sh   (QUICK=1 for short runs while developing)
"""

import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "kpi"))
import xlsx  # noqa: E402

API = os.environ.get("SCHED_API", "http://localhost:9114")
UE = "o5gs-ue"
FLOWGEN = os.path.join(REPO, "tools", "slice-orchestrator", "flowgen.py")
QUICK = os.environ.get("QUICK") == "1"
T = 20 if QUICK else int(os.environ.get("T", "60"))
PARTS = os.environ.get("PARTS", "ABCD").upper()
GW = {"eMBB": "10.45.0.1", "URLLC": "10.46.0.1", "mMTC": "10.47.0.1"}
DN = {"eMBB": "o5gs-dn-embb", "URLLC": "o5gs-dn-urllc", "mMTC": "o5gs-dn-mmtc"}
PORT0 = 5600


def sh(args, timeout=60):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout


def api(path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def class_counters():
    """{(direction, slice, tier): {"bytes":…, "drops":…, "packets":…}} from the scheduler's /metrics."""
    out = {}
    with urllib.request.urlopen(API + "/metrics", timeout=10) as r:
        text = r.read().decode()
    for m in re.finditer(r'packet_scheduler_class_(bytes|drops|packets)_total\{direction="(\w+)",classid="[^"]*",'
                         r'slice="([^"]*)",tier="([^"]*)"\} (\d+)', text):
        key = (m.group(2), m.group(3), m.group(4))
        out.setdefault(key, {})[m.group(1)] = int(m.group(5))
    return out


def devices():
    """{slice: [(address, interface)]}: each device's HOME session (PSI 1, ADR-020), in address order."""
    out = {s: [] for s in GW}
    for line in sh(["docker", "exec", UE, "sh", "/etc/ueransim/ue-slice.sh", "sessions"]).splitlines():
        f = line.split()
        if len(f) != 5 or f[4] != "home":
            continue
        for s, gw in GW.items():
            if f[3].startswith(gw.rsplit(".", 2)[0] + "."):
                out[s].append((f[3], f[2]))
    for s in out:
        out[s].sort(key=lambda d: tuple(int(x) for x in d[0].split(".")))
    return out


def rlf_events():
    """Radio-link failures logged by all UEs so far (logs are truncated when a UE starts)."""
    out = sh(["docker", "exec", UE, "sh", "-c", "cat /tmp/ue-[0-9]*.log 2>/dev/null | grep -c 'Radio link failure'"])
    return int(out.strip() or 0)


def broken_ues():
    """UEs whose data path is down: pings from each UE's interface to its UPF, all in parallel."""
    script = ("for l in $(ip -4 -o addr show | awk '/uesimtun/ {print $2\"@\"$4}'); do "
              "i=${l%%@*}; a=${l#*@}; gw=$(echo $a | cut -d. -f1,2).0.1; "
              "( ping -n -c 2 -W 1 -I $i $gw >/dev/null 2>&1 || echo $i ) & done; wait")
    out = sh(["docker", "exec", UE, "sh", "-c", script], timeout=60)
    up = int(sh(["docker", "exec", UE, "sh", "-c", "ip -4 -o addr show | grep -c uesimtun"]).strip() or 0)
    return [x for x in out.split() if x.startswith("uesimtun")] + (["missing"] * max(0, 100 - up))


def restore():
    """Re-attach all UEs and wait until the scheduler has re-applied to the new UE container."""
    applies = api("/state")["applies"]
    subprocess.run(["bash", os.path.join(REPO, "scripts", "open5gs", "start_ues.sh")],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    for _ in range(60):
        st = api("/state")
        if st["applies"] > applies and len(st["tracked_addresses"].get("URLLC/0", [])) >= 3:
            break
        time.sleep(2)
    subprocess.run(["docker", "exec", "-i", UE, "sh", "-c", "cat > /tmp/fg.py"], stdin=open(FLOWGEN, "rb"), check=True)
    time.sleep(10)


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def summarise(rtts, sent):
    return {"sent": sent, "received": len(rtts),
            "loss_percent": round(100.0 * (1 - len(rtts) / sent), 2) if sent else None,
            "p50": pct(rtts, 50), "p95": pct(rtts, 95), "p99": pct(rtts, 99),
            "max": round(max(rtts), 3) if rtts else None,
            "mean": round(statistics.mean(rtts), 3) if rtts else None}


class Run:
    """One measurement: loads (flows) and probes (pings) started together, then collected."""

    def __init__(self):
        self.flows, self.pings, self.port = [], [], PORT0

    def flow(self, slice_name, addr, mbps, seconds):
        port = self.port
        self.port += 1
        rx = subprocess.Popen(["docker", "exec", UE, "timeout", "-s", "KILL", str(seconds + 25), "python3",
                               "/tmp/fg.py", "recv", addr, str(port), str(seconds)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.flows.append({"slice": slice_name, "addr": addr, "mbps": mbps, "port": port, "rx": rx,
                           "seconds": seconds})

    def start_senders(self):
        for f in self.flows:
            f["tx"] = subprocess.Popen(
                ["docker", "exec", "-i", DN[f["slice"]], "timeout", "-s", "KILL", str(f["seconds"] + 20),
                 "python3", "-", "send", f["addr"], str(f["port"]), str(f["mbps"]), str(f["seconds"])],
                stdin=open(FLOWGEN, "rb"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def probe(self, name, iface, target, interval, seconds):
        count = max(1, int(seconds / interval))
        p = subprocess.Popen(["docker", "exec", UE, "ping", "-n", "-I", iface, "-i", str(interval), "-c", str(count),
                              "-W", "1", target], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.pings.append({"name": name, "count": count, "p": p})

    def collect(self):
        probes = {}
        for x in self.pings:
            out, _ = x["p"].communicate(timeout=T + 120)
            rtts = [float(m) for m in re.findall(r"time=([0-9.]+) ms", out)]
            probes[x["name"]] = summarise(rtts, x["count"])
        loads = {}
        for f in self.flows:
            out, _ = f["rx"].communicate(timeout=T + 120)
            f["tx"].wait(timeout=60)
            res = {}
            for line in out.splitlines():
                if line.startswith("{"):
                    res = json.loads(line)
            agg = loads.setdefault(f["slice"], {"flows": 0, "offered_mbps": 0.0, "delivered_mbps": 0.0, "loss": []})
            agg["flows"] += 1
            agg["offered_mbps"] += f["mbps"]
            agg["delivered_mbps"] += res.get("mbps", 0.0)
            agg["loss"].append(res.get("loss_percent", 100.0))
        for agg in loads.values():
            agg["offered_mbps"] = round(agg["offered_mbps"], 2)
            agg["delivered_mbps"] = round(agg["delivered_mbps"], 2)
            agg["loss_percent_mean"] = round(statistics.mean(agg.pop("loss")), 2)
        return probes, loads


def configure(**body):
    st = api("/config", body)
    time.sleep(3)
    return st


def roles():
    """Device roles, taken fresh: addresses change whenever the UEs are re-attached."""
    dev = devices()
    crit = set(api("/state")["tracked_addresses"].get("URLLC/0", []))
    return dev, [d for d in dev["URLLC"] if d[0] in crit], [d for d in dev["URLLC"] if d[0] not in crit]


def measure(label, cfg, plan, seconds):
    """cfg: scheduler settings; plan(dev, critical, standard) -> (load [(slice, addr, mbps)],
    probes [(name, iface, slice, interval)])."""
    broken = broken_ues()
    restored = False
    if broken:
        print("    %d UEs without a data path before %s: re-attaching all" % (len(broken), label), flush=True)
        restore()
        restored = True
    dev, critical, standard = roles()
    load, probes = plan(dev, critical, standard)
    st = configure(**cfg)
    rlf0 = rlf_events()
    c0 = class_counters()
    t0 = time.time()
    r = Run()
    for s, addr, mbps in load:
        r.flow(s, addr, mbps, seconds)
    time.sleep(1.5)                                     # receivers bound
    r.start_senders()
    time.sleep(3 if load else 0)                        # let the queues build
    for name, iface, s, interval in probes:
        r.probe(name, iface, GW[s], interval, seconds - 6 if load else seconds)
    probes_out, loads_out = r.collect()
    elapsed = time.time() - t0
    c1 = class_counters()
    classes = {}
    for k, v in c1.items():
        d, s, tier = k
        if s == "cell" or tier == "all":
            continue
        b0 = c0.get(k, {})
        classes["%s %s%s" % (d, s, " " + tier if tier else "")] = {
            "mbps": round((v.get("bytes", 0) - b0.get("bytes", 0)) * 8 / elapsed / 1e6, 3),
            "drops": v.get("drops", 0) - b0.get("drops", 0)}
    # A UE restarted by its supervisor during the run starts a fresh log, so the total can fall
    # (seen once: -1). Clamp at 0; such a restart can hide failures, never invent them.
    rlf = max(0, rlf_events() - rlf0)
    left_broken = len(broken_ues())
    res = {"label": label, "mode": st["mode"], "cell_mbps": st["cell_mbps"], "shares": st["shares"],
           "seconds": seconds, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
           "restored_before": restored, "radio_link_failures": rlf, "ues_without_data_path_after": left_broken,
           "probes": probes_out, "load": loads_out, "classes": classes}
    print("  %-26s RLF %-3d broken after %-3d %s" % (label, rlf, left_broken, "  ".join(
        "%s p99 %s loss %s%%" % (n, p["p99"], p["loss_percent"]) for n, p in probes_out.items())), flush=True)
    return res


def main():
    ts = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
    out_dir = os.path.join(REPO, "docs", "evidence", "open5gs-scheduler", ts)
    os.makedirs(out_dir, exist_ok=True)
    subprocess.run(["docker", "exec", "-i", UE, "sh", "-c", "cat > /tmp/fg.py"], stdin=open(FLOWGEN, "rb"), check=True)
    base = api("/state")
    if broken_ues():
        restore()
    dev, critical, standard = roles()
    if not critical or len(standard) < 7 or len(dev["eMBB"]) < 11 or len(dev["mMTC"]) < 41:
        sys.exit("not enough devices up: %s critical, %d standard, %d eMBB, %d mMTC"
                 % (len(critical), len(standard), len(dev["eMBB"]), len(dev["mMTC"])))
    print("devices: critical URLLC %s, standard URLLC %d, eMBB %d, mMTC %d" % (
        [c[0] for c in critical], len(standard), len(dev["eMBB"]), len(dev["mMTC"])), flush=True)
    defaults = {"mode": base["mode"], "cell_dl_mbps": base["cell_mbps"]["downlink"],
                "cell_ul_mbps": base["cell_mbps"]["uplink"], "shares": base["shares"]}
    results = {"timestamp": ts, "seconds": T, "parts": PARTS, "scheduler_defaults": defaults,
               "A": [], "B": [], "C": [], "D": []}

    # A — isolation under eMBB overload
    def probes_a(dev, crit, std):
        return [("URLLC critical", crit[0][1], "URLLC", 0.02), ("URLLC standard", std[0][1], "URLLC", 0.02),
                ("mMTC", dev["mMTC"][0][1], "mMTC", 0.2), ("eMBB unloaded", dev["eMBB"][10][1], "eMBB", 0.1)]

    def plan_a(dev, crit, std):
        return [("eMBB", a, 40) for a, _ in dev["eMBB"][:10]], probes_a(dev, crit, std)

    if "A" in PARTS:
        print("A: eMBB offers 400 Mbps into a 200 Mbps cell", flush=True)
        results["A"].append(measure("idle (sliced, no load)", {"mode": "sliced", "cell_dl_mbps": 200},
                                    lambda d, c, s: ([], probes_a(d, c, s)), T))
        for rnd, order in ((1, ["off", "fifo", "sliced"]), (2, ["sliced", "fifo", "off"])):
            for mode in order:
                results["A"].append(measure("round %d %s" % (rnd, mode), {"mode": mode, "cell_dl_mbps": 200}, plan_a, T))

    # B — ARP tiers inside URLLC
    def plan_b(dev, crit, std):
        return ([("URLLC", a, 20) for a, _ in std[1:7]],
                [("URLLC critical", crit[0][1], "URLLC", 0.02), ("URLLC standard", std[0][1], "URLLC", 0.02)])

    if "B" in PARTS:
        print("B: six standard URLLC devices flooded at their 20 Mbps AMBR into a 100 Mbps cell", flush=True)
        for rnd, order in ((1, ["fifo", "sliced"]), (2, ["sliced", "fifo"])):
            for mode in order:
                results["B"].append(measure("round %d %s" % (rnd, mode), {"mode": mode, "cell_dl_mbps": 100}, plan_b, T))

    if "D" in PARTS:
        print("D: eMBB offers 150 Mbps into a 100 Mbps cell (the cell, not the CPU, is the bottleneck)", flush=True)

        def plan_d(dev, crit, std):
            return [("eMBB", a, 30) for a, _ in dev["eMBB"][:5]], probes_a(dev, crit, std)

        for rnd, order in ((1, ["off", "fifo", "sliced"]), (2, ["sliced", "fifo", "off"])):
            for mode in order:
                results["D"].append(measure("round %d %s" % (rnd, mode), {"mode": mode, "cell_dl_mbps": 100}, plan_d, T))

    # C — guaranteed shares are a tuning knob
    def plan_c(dev, crit, std):
        return ([("eMBB", a, 40) for a, _ in dev["eMBB"][:4]] + [("mMTC", a, 1) for a, _ in dev["mMTC"][1:41]],
                [("URLLC critical", crit[0][1], "URLLC", 0.02)])

    fixed = 100 - base["shares"]["URLLC"] - base["signalling_share"] - base["default_share"]
    for share in ((5, 10, 15, 20, 25) if "C" in PARTS else ()):
        if share == 5:
            print("C: eMBB 160 Mbps + mMTC 40 Mbps offered into a 60 Mbps cell; mMTC share swept", flush=True)
        shares = dict(base["shares"], mMTC=share, eMBB=fixed - share)
        r = measure("mMTC share %d%%" % share, {"mode": "sliced", "cell_dl_mbps": 60, "shares": shares}, plan_c, T)
        r["mmtc_share"] = share
        r["mmtc_guaranteed_mbps"] = round(60 * share / 100.0, 2)
        results["C"].append(r)

    configure(**defaults)
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=2)
    write_summary(results, out_dir)
    print("evidence: %s" % os.path.relpath(out_dir, REPO), flush=True)


def write_summary(res, out_dir):
    L = ["# Packet scheduler — measured on the live stack (ADR-019)", "",
         "Run %s, %d s per measurement. Cell, load and probes as described in "
         "`tools/packet-scheduler/experiment.py`. RTT is ping from the device's own PDU-session interface "
         "to its UPF (ms); loss is lost pings. Delivered is what the devices received." % (res["timestamp"], res["seconds"]), ""]
    rows_x = []
    for part, title in (("A", "## A — isolation: eMBB offers 400 Mbps into a 200 Mbps cell (the CPU saturates)"),
                        ("D", "## D — isolation where the cell is the bottleneck: eMBB offers 150 Mbps into a 100 Mbps cell")):
        if not res.get(part):
            continue
        L += [title, "",
              "| Run | URLLC critical p50 / p99 / max | URLLC standard p50 / p99 / max | loss crit / std | mMTC p99 | eMBB unloaded p99 | eMBB delivered | radio-link failures | UEs left without data path |",
              "|---|---|---|---|---|---|---|---|---|"]
        rows_x += table_a(res[part], part, L)
        L.append("")
    write_rest(res, out_dir, L, rows_x)


def table_a(runs, part, L):
    rows = []
    for r in runs:
        pc, ps = r["probes"]["URLLC critical"], r["probes"]["URLLC standard"]
        emb = r["load"].get("eMBB", {})
        L.append("| %s | %s / %s / %s | %s / %s / %s | %s %% / %s %% | %s | %s | %s Mbps | %s | %s |" % (
            r["label"], pc["p50"], pc["p99"], pc["max"], ps["p50"], ps["p99"], ps["max"], pc["loss_percent"],
            ps["loss_percent"], r["probes"]["mMTC"]["p99"], r["probes"]["eMBB unloaded"]["p99"],
            emb.get("delivered_mbps", "-"), r["radio_link_failures"], r["ues_without_data_path_after"]))
        rows.append([part, r["label"], r["mode"], r["cell_mbps"]["downlink"], pc["p50"], pc["p99"], pc["max"],
                     pc["loss_percent"], ps["p50"], ps["p99"], ps["max"], ps["loss_percent"],
                     r["probes"]["mMTC"]["p99"], r["probes"]["eMBB unloaded"]["p99"], emb.get("delivered_mbps"),
                     r["radio_link_failures"], r["ues_without_data_path_after"]])
    return rows


def write_rest(res, out_dir, L, rows_x):
    if res.get("B"):
        L += ["## B — ARP tiers: six standard URLLC devices flood a 100 Mbps cell", "",
          "| Run | critical p50 / p99 / max | standard p50 / p99 / max | loss crit / std | URLLC delivered | radio-link failures |",
          "|---|---|---|---|---|---|", ""]
    for r in res.get("B", []):
        pc, ps = r["probes"]["URLLC critical"], r["probes"]["URLLC standard"]
        L.insert(len(L) - 1, "| %s | %s / %s / %s | %s / %s / %s | %s %% / %s %% | %s Mbps | %s |" % (
            r["label"], pc["p50"], pc["p99"], pc["max"], ps["p50"], ps["p99"], ps["max"], pc["loss_percent"],
            ps["loss_percent"], r["load"].get("URLLC", {}).get("delivered_mbps", "-"), r["radio_link_failures"]))
        rows_x.append(["B", r["label"], r["mode"], r["cell_mbps"]["downlink"], pc["p50"], pc["p99"], pc["max"],
                       pc["loss_percent"], ps["p50"], ps["p99"], ps["max"], ps["loss_percent"], None, None,
                       r["load"].get("URLLC", {}).get("delivered_mbps"), r["radio_link_failures"],
                       r["ues_without_data_path_after"]])
    if res.get("C"):
        L += ["## C — shares: eMBB 160 + mMTC 40 Mbps offered into a 60 Mbps cell", "",
              "| mMTC share | mMTC guaranteed | mMTC delivered | eMBB delivered | URLLC critical p99 |", "|---|---|---|---|---|"]
    rows_c = [["mMTC share %", "mMTC guaranteed Mbps", "mMTC delivered Mbps", "eMBB delivered Mbps", "URLLC critical p99 ms"]]
    for r in res.get("C", []):
        m, e = r["load"].get("mMTC", {}), r["load"].get("eMBB", {})
        L.append("| %d %% | %s Mbps | %s Mbps | %s Mbps | %s |" % (
            r["mmtc_share"], r["mmtc_guaranteed_mbps"], m.get("delivered_mbps"), e.get("delivered_mbps"),
            r["probes"]["URLLC critical"]["p99"]))
        rows_c.append([r["mmtc_share"], r["mmtc_guaranteed_mbps"], m.get("delivered_mbps"), e.get("delivered_mbps"),
                       r["probes"]["URLLC critical"]["p99"]])
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    head = ["Part", "Run", "Mode", "Cell DL Mbps", "Crit p50", "Crit p99", "Crit max", "Crit loss %",
            "Std p50", "Std p99", "Std max", "Std loss %", "mMTC p99", "eMBB unloaded p99", "Delivered Mbps",
            "Radio-link failures", "UEs without data path after"]
    xlsx.write(os.path.join(out_dir, "results.xlsx"), [
        xlsx.Sheet("Isolation and ARP", rows=[[(h, "header") for h in head]] + rows_x, widths=[6, 24] + [12] * 15),
        xlsx.Sheet("Share sweep", rows=[[(h, "header") for h in rows_c[0]]] + rows_c[1:], widths=[14, 20, 20, 20, 20]),
    ])


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--merge":
        # --merge <run dir> <other run dir>...: one summary over several runs (parts from each)
        base = json.load(open(os.path.join(sys.argv[2], "results.json")))
        for d in sys.argv[3:]:
            other = json.load(open(os.path.join(d, "results.json")))
            for part in "ABCD":
                if other.get(part):
                    base[part] = other[part]
                    base.setdefault("merged", []).append({"part": part, "from": other["timestamp"]})
        json.dump(base, open(os.path.join(sys.argv[2], "results-merged.json"), "w"), indent=2)
        write_summary(base, sys.argv[2])
    else:
        main()
