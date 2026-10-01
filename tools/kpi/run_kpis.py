#!/usr/bin/env python3
"""
run_kpis.py — measure the Phase 2b per-slice KPIs on the running Open5GS stack, score them, and
write the Excel report (Tracks 2, 3 and 4; docs/adr/ADR-015-traffic-profiles-and-kpi-scorecard.md).

An INSTANCE is one measurement window in which all three slices run their service at once:
  URLLC  every device sends 64-byte packets every 20 ms (traffic_agent.py, always on)
  mMTC   every device sends a 10-byte report every 10 s (traffic_agent.py, always on)
  eMBB   N devices download as fast as they can (TCP, iperf3), started for the window
and the KPIs defined in deployments/open5gs/kpi-scorecard.json are measured and scored.
A SWEEP repeats the instance while ONE parameter changes — the guide's "KPI report at all
instances by varying one parameter".

    tools/kpi/run_kpis.py standard                         fresh attach, then one nominal instance
    tools/kpi/run_kpis.py sweep embb_ues 0 5 10 20         eMBB devices downloading at once
    tools/kpi/run_kpis.py sweep mmtc_interval 1 5 10 30    seconds between IoT reports
    tools/kpi/run_kpis.py sweep mmtc_devices 70 140 210    IoT devices attached ("increase the UEs")
    tools/kpi/run_kpis.py all                              all of the above, then the report
    tools/kpi/run_kpis.py report <run dir>                 rebuild the Excel workbook

Output: docs/evidence/open5gs-kpi/<timestamp>/ — instances.jsonl (every measurement, raw),
kpi-report.xlsx, scorecard.md, one CSV per sweep. Run from the repo root on the Docker host.
DURATION (default 60 s) and STANDARD_DURATION (150 s: >= 1000 IoT reports) set window lengths.
RUN_DIR=<existing run> adds to that run instead; a re-run instance replaces its earlier record
in the report (both stay in instances.jsonl).
"""

import csv
import datetime
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import scorecard  # noqa: E402
import xlsx  # noqa: E402

UE = "o5gs-ue"
COLLECTORS = {"URLLC": "http://10.53.0.52:9130", "mMTC": "http://10.53.0.53:9130"}
DN_EMBB, DN_EMBB_IP = "o5gs-dn-embb", "10.53.0.51"
DURATION = int(os.environ.get("DURATION", "60"))
STANDARD_DURATION = int(os.environ.get("STANDARD_DURATION", "150"))
STANDARD_EMBB_UES = int(os.environ.get("STANDARD_EMBB_UES", "20"))
CPU_CONTAINERS = ["o5gs-ue", "o5gs-gnb-embb", "o5gs-gnb-urllc", "o5gs-gnb-mmtc", "o5gs-upf-embb",
                  "o5gs-upf-urllc", "o5gs-upf-mmtc", "o5gs-amf", "o5gs-smf", "o5gs-dn-urllc", "o5gs-dn-mmtc"]
PARAMS = {
    "embb_ues": "eMBB devices downloading at once",
    "mmtc_interval": "seconds between each IoT device's reports",
    "mmtc_devices": "IoT (mMTC) devices attached",
}

OUT = None


def log(msg):
    line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if OUT:
        with open(os.path.join(OUT, "run.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")


def sh(args, check=False, timeout=None, **kw):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(args[:4]), r.stderr.strip()[:300]))
    return r


def dexec(container, *cmd, **kw):
    return sh(["docker", "exec", container] + list(cmd), **kw)


def http_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ------------------------------------------------------------------ the stack

def ue_plan():
    """{slice: (first IMSI, last IMSI)} for the plan the UE container is running NOW — the one-off
    override file if a run wrote one, else its environment. (Reading only the environment once
    counted 70 of 210 IoT devices during the device sweep.)"""
    env = sh(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", UE], check=True).stdout
    plan = next((l[len("UE_PLAN="):] for l in env.splitlines() if l.startswith("UE_PLAN=")), "")
    override = dexec(UE, "sh", "-c", "cat /tmp/ue-plan.override 2>/dev/null").stdout
    m = re.search(r'^UE_PLAN="([^"]*)"', override, re.M)
    if m:
        plan = m.group(1)
    first = int(next((l[len("FIRST_IMSI="):] for l in env.splitlines() if l.startswith("FIRST_IMSI=")), "208930000000001"))
    ranges, imsi = {}, first
    for item in plan.split():
        cfg, n, gw, name = item.split(":")
        ranges[name] = (imsi, imsi + int(n) - 1)
        imsi += int(n)
    return ranges


def ue_addresses():
    out = dexec(UE, "ip", "-4", "-o", "addr", "show").stdout
    return [(int(t), ip) for _, t, ip in re.findall(r"^\d+:\s+(uesimtun(\d{4})\d)\s+inet\s+([\d.]+)/", out, re.M)]


def wait_ues(want, timeout=300):
    t0 = time.time()
    n = 0
    while time.time() - t0 < timeout:
        try:
            n = len(ue_addresses())
        except Exception:
            n = 0
        if n >= want:
            return n
        time.sleep(3)
    return n


def registration_stats():
    """Per slice, from each UE's own log at its latest attach: successful and failed initial
    registrations. Logs are reset whenever a UE (re)starts, so this describes the latest attach."""
    out = dexec(UE, "sh", "-c",
                'for f in /tmp/ue-2089*.log; do i=${f#/tmp/ue-}; i=${i%.log}; '
                's=$(grep -c "Initial Registration is successful" "$f"); '
                'x=$(grep -c "Initial Registration failed" "$f"); echo "$i $s $x"; done').stdout
    ranges = ue_plan()
    res = {n: {"ues": hi - lo + 1, "registered": 0, "succeeded": 0, "failed": 0} for n, (lo, hi) in ranges.items()}
    for line in out.split("\n"):
        p = line.split()
        if len(p) != 3:
            continue
        imsi, s, x = int(p[0]), int(p[1]), int(p[2])
        for name, (lo, hi) in ranges.items():
            if lo <= imsi <= hi:
                r = res[name]
                r["succeeded"] += s
                r["failed"] += x
                r["registered"] += 1 if s > 0 else 0
    for r in res.values():
        att = r["succeeded"] + r["failed"]
        r["success_rate"] = round(r["succeeded"] / att, 6) if att else None
    return res


def set_profile(override):
    if override:
        sh(["docker", "exec", "-i", UE, "sh", "-c", "cat > /tmp/traffic-profile.json"], input=json.dumps(override), check=True)
    else:
        dexec(UE, "rm", "-f", "/tmp/traffic-profile.json")


def nearest_rank(vals, q):
    v = sorted(vals)
    return v[max(0, math.ceil(q * len(v)) - 1)] if v else None


def embb_load(n, duration):
    """n eMBB devices each download (TCP, DN -> UE) for `duration` s at once -> per-UE Mbps."""
    if n <= 0:
        time.sleep(duration)
        return []
    ues = sorted((dev, ip) for dev, ip in ue_addresses() if ip.startswith("10.45."))[:n]
    dexec(DN_EMBB, "sh", "-c", "pkill -x iperf3; true")
    flows = []
    for i, (dev, ip) in enumerate(ues):
        port = 5600 + i
        dexec(DN_EMBB, "iperf3", "-s", "-B", DN_EMBB_IP, "-p", str(port), "-D", "--one-off")
        flows.append((dev, ip, port))
    for _ in range(50):
        listening = dexec(DN_EMBB, "sh", "-c", "ss -ltn | grep -c ':56[0-9][0-9] '").stdout.strip()
        if listening.isdigit() and int(listening) >= len(flows):
            break
        time.sleep(0.2)
    procs = [(dev, ip, subprocess.Popen(
        ["docker", "exec", UE, "iperf3", "-c", DN_EMBB_IP, "-p", str(port), "-B", ip, "-R",
         "-t", str(duration), "-J"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
        for dev, ip, port in flows]
    res = []
    for dev, ip, p in procs:
        out, _ = p.communicate(timeout=duration + 60)
        try:
            mbps = json.loads(out)["end"]["sum_received"]["bits_per_second"] / 1e6
        except Exception:
            mbps = None
        res.append({"device": dev, "ip": ip, "mbps": None if mbps is None else round(mbps, 2)})
    return res


def cpu_sampler(duration):
    return subprocess.Popen([sys.executable, os.path.join(REPO, "scripts", "open5gs", "cgroup_stats.py"),
                             "--json", "--duration", str(duration)] + CPU_CONTAINERS,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


# ------------------------------------------------------------------ one instance

def instance(label, param=None, value=None, embb_ues=None, duration=None):
    defs = scorecard.load()
    embb_ues = STANDARD_EMBB_UES if embb_ues is None else embb_ues
    duration = duration or DURATION
    log("instance %s: %s = %s · eMBB devices downloading %d · %d s"
        % (label, param or "standard", value if value is not None else "-", embb_ues, duration))
    for url in COLLECTORS.values():
        http_json(url + "/run/start")
    cpu = cpu_sampler(duration)
    load0 = os.getloadavg()
    t0 = time.time()
    flows = embb_load(embb_ues, duration)
    stats = {name: http_json(url + "/run/stats") for name, url in COLLECTORS.items()}
    try:
        cpu_out = json.loads(cpu.communicate(timeout=30)[0] or "{}")
    except Exception:
        cpu_out = {}
    reg = registration_stats()
    rates = [f["mbps"] for f in flows if f["mbps"] is not None]
    u, m = stats["URLLC"], stats["mMTC"]
    measured = {
        "urllc.owd_mean_ms": u["owd_ms"].get("mean"), "urllc.owd_p50_ms": u["owd_ms"].get("p50"),
        "urllc.owd_p95_ms": u["owd_ms"].get("p95"), "urllc.owd_p99_ms": u["owd_ms"].get("p99"),
        "urllc.owd_max_ms": u["owd_ms"].get("max"), "urllc.loss_ratio": u["loss_ratio"],
        "urllc.jitter_ms": u["jitter_ms_mean"], "urllc.packets": u["received"], "urllc.devices": u["devices"],
        "embb.ues_loaded": embb_ues, "embb.flows_completed": len(rates),
        "embb.dl_p5_mbps": round(nearest_rank(rates, 0.05), 2) if rates else None,
        "embb.dl_mean_mbps": round(sum(rates) / len(rates), 2) if rates else None,
        "embb.dl_min_mbps": min(rates) if rates else None, "embb.dl_max_mbps": max(rates) if rates else None,
        "embb.dl_aggregate_mbps": round(sum(rates), 1) if rates else None,
        "mmtc.registration_success": reg.get("mMTC", {}).get("success_rate"),
        "mmtc.registered": reg.get("mMTC", {}).get("registered"), "mmtc.provisioned": reg.get("mMTC", {}).get("ues"),
        "mmtc.loss_ratio": m["loss_ratio"], "mmtc.reports": m["received"], "mmtc.expected": m["expected"],
        "mmtc.loss_resolution": round(1 / m["expected"], 6) if m["expected"] else None,
        "mmtc.owd_mean_ms": m["owd_ms"].get("mean"), "mmtc.owd_p99_ms": m["owd_ms"].get("p99"),
        "mmtc.owd_max_ms": m["owd_ms"].get("max"), "mmtc.devices_reporting": m["devices"],
        "registration_success.eMBB": reg.get("eMBB", {}).get("success_rate"),
        "registration_success.URLLC": reg.get("URLLC", {}).get("success_rate"),
        "host.load1_start": round(load0[0], 2), "host.load1_end": round(os.getloadavg()[0], 2),
    }
    for c, v in cpu_out.items():
        if v.get("cpu_avg") is not None:
            measured["cpu.%s" % c.replace("o5gs-", "")] = v["cpu_avg"]
    card = scorecard.score(defs, measured)
    rec = {"label": label, "param": param, "value": value, "started": datetime.datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
           "duration_s": duration, "measured": measured, "card": card, "registration": reg,
           "embb_flows": flows,
           "urllc_per_device": u.get("per_device"), "mmtc_per_device": m.get("per_device")}
    with open(os.path.join(OUT, "instances.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    sl = {s["slice"]: s["score"] for s in card["slices"]}
    log("  URLLC mean %s p99 %s ms loss %s · eMBB p5 %s agg %s Mbps · mMTC reg %s loss %s (res %s) · scores %s · overall %s"
        % (measured["urllc.owd_mean_ms"], measured["urllc.owd_p99_ms"], measured["urllc.loss_ratio"],
           measured["embb.dl_p5_mbps"], measured["embb.dl_aggregate_mbps"], measured["mmtc.registration_success"],
           measured["mmtc.loss_ratio"], measured["mmtc.loss_resolution"], sl, card["overall"]))
    return rec


# ------------------------------------------------------------------ sweeps

def standard():
    log("standard: fresh attach of all UEs (registration KPIs describe this attach)")
    r = sh(["bash", os.path.join(REPO, "scripts", "open5gs", "start_ues.sh")], timeout=900)
    log("  " + " · ".join(l.strip() for l in r.stdout.splitlines() if "interfaces" in l or "attached in" in l))
    time.sleep(30)
    return instance("standard", duration=STANDARD_DURATION)


def sweep(param, values):
    log("sweep %s (%s): %s" % (param, PARAMS[param], " ".join(str(v) for v in values)))
    recs = []
    if param == "embb_ues":
        for v in values:
            recs.append(instance("embb_ues=%s" % v, param, v, embb_ues=int(v)))
    elif param == "mmtc_interval":
        prev = 10.0
        try:
            for v in values:
                set_profile({"mMTC": {"interval": float(v)}})
                time.sleep(prev + 5)                 # every device has switched to the new interval
                recs.append(instance("mmtc_interval=%ss" % v, param, v))
                prev = float(v)
        finally:
            set_profile(None)
            time.sleep(prev + 5)
    elif param == "mmtc_devices":
        try:
            for v in values:
                attach_mmtc(int(v))
                recs.append(instance("mmtc_devices=%s" % v, param, v))
        finally:
            restore_standard()
    else:
        raise SystemExit("unknown parameter %s (one of %s)" % (param, ", ".join(PARAMS)))
    return recs


def attach_mmtc(n):
    """Restart the UE container with n IoT devices (eMBB 20, URLLC 10 unchanged), provisioned.
    The plan goes into an override file the UE entrypoint reads at start: no `docker compose`."""
    log("  attaching %d IoT devices (eMBB 20 + URLLC 10 + mMTC %d = %d UEs)" % (n, n, n + 30))
    env = dict(os.environ, O5GS_UES="A=20 B=10 C=%d" % n)
    sh([sys.executable, os.path.join(REPO, "scripts", "open5gs", "provision_subscribers.py")], env=env, check=True)
    override = ('UE_PLAN="ue-slice-a.yaml:20:10.45.0.1:eMBB ue-slice-b.yaml:10:10.46.0.1:URLLC '
                'ue-slice-c.yaml:%d:10.47.0.1:mMTC"\nSTART_BATCH=25\n' % n)
    sh(["docker", "exec", "-i", UE, "sh", "-c", "cat > /tmp/ue-plan.override"], input=override, check=True)
    sh(["docker", "restart", UE], check=True)
    got = wait_ues(n + 30, timeout=600)
    log("  %d/%d UEs with a session; settling 30 s" % (got, n + 30))
    time.sleep(30)


def restore_standard():
    log("restore: the standard 100 UEs")
    dexec("o5gs-mongodb", "mongo", "open5gs", "--quiet", "--eval",
          'db.subscribers.deleteMany({imsi: {$gt: "208930000000100"}})')
    dexec(UE, "rm", "-f", "/tmp/ue-plan.override")
    r = sh(["bash", os.path.join(REPO, "scripts", "open5gs", "start_ues.sh")], timeout=900)
    log("  " + " · ".join(l.strip() for l in r.stdout.splitlines() if "attached in" in l))


# ------------------------------------------------------------------ the report

COLUMNS = [  # (header, measured key)
    ("URLLC latency mean (ms)", "urllc.owd_mean_ms"), ("URLLC p95 (ms)", "urllc.owd_p95_ms"),
    ("URLLC p99 (ms)", "urllc.owd_p99_ms"), ("URLLC max (ms)", "urllc.owd_max_ms"),
    ("URLLC loss", "urllc.loss_ratio"), ("URLLC jitter (ms)", "urllc.jitter_ms"), ("URLLC packets", "urllc.packets"),
    ("eMBB devices loaded", "embb.ues_loaded"), ("eMBB p5 DL (Mbps)", "embb.dl_p5_mbps"),
    ("eMBB mean DL (Mbps)", "embb.dl_mean_mbps"), ("eMBB aggregate DL (Mbps)", "embb.dl_aggregate_mbps"),
    ("mMTC registration success", "mmtc.registration_success"), ("mMTC registered", "mmtc.registered"),
    ("mMTC provisioned", "mmtc.provisioned"), ("mMTC report loss", "mmtc.loss_ratio"),
    ("mMTC loss resolution", "mmtc.loss_resolution"), ("mMTC reports", "mmtc.reports"),
    ("mMTC latency p99 (ms)", "mmtc.owd_p99_ms"), ("mMTC devices reporting", "mmtc.devices_reporting"),
    ("UE container CPU (% core)", "cpu.ue"), ("eMBB UPF CPU (% core)", "cpu.upf-embb"),
    ("host load start", "host.load1_start"), ("host load end", "host.load1_end"),
]


def load_instances(run_dir):
    """Every instance, in order; when an instance was re-run (same label), the latest replaces it.
    The superseded record stays in instances.jsonl as the history of what was measured."""
    with open(os.path.join(run_dir, "instances.jsonl"), encoding="utf-8") as f:
        recs = [json.loads(l) for l in f if l.strip()]
    latest = {}
    for i, r in enumerate(recs):
        latest[r["label"]] = i
    return [r for i, r in enumerate(recs) if latest[r["label"]] == i]


def mark(met):
    return {True: ("MET", "met"), False: ("MISS", "miss"), None: ("", "default")}[met]


def report(run_dir):
    defs = scorecard.load()
    recs = load_instances(run_dir)
    meta = {}
    if os.path.exists(os.path.join(run_dir, "meta.json")):
        meta = json.load(open(os.path.join(run_dir, "meta.json"), encoding="utf-8"))
    sheets = []

    readme = [[("Phase 2b — per-slice KPI report (Open5GS + UERANSIM, 100 UEs, three slices)", "bold")], [],
              ["Run", meta.get("started", "")], ["Commit", meta.get("commit", "")], ["Host", meta.get("host", "")],
              ["Instances", len(recs)], [],
              [("Assumptions (team KPI notes)", "bold")]]
    readme += [["", a] for a in defs["assumptions"]]
    readme += [[], [("How to read it", "bold")],
               ["", "Each row of a Sweep sheet is one instance: all three slices served at once for a fixed window, one parameter changed."],
               ["", "URLLC and mMTC latency is one-way, device to its slice's data network, on one shared monotonic clock."],
               ["", "eMBB throughput: each loaded device downloads over TCP for the window; p5 is the 5th percentile across devices."],
               ["", "Score per KPI: 1 if the target is met, otherwise target/measured (lower is better) or measured/target (higher is better)."],
               ["", "Slice score: weighted sum of its KPI scores (weights in kpi-scorecard.json, provisional). Overall: mean of the slices."],
               ["", "vs benchmark: the measurement as a fraction of the industry benchmark (1.0 = equal to the industry value)."],
               ["", "mMTC loss resolution = 1 / reports expected: a loss smaller than this cannot be seen in that instance."],
               [], [("Provisional choices (not fixed by the notes)", "bold")],
               ["", "URLLC traffic: 64-byte packets every 20 ms per device. mMTC: 10-byte reports every 10 s per device."],
               ["", "KPI weights; sweep parameters and values; standard instance length 150 s (>= 1000 IoT reports)."],
               [], ["Definitions", "deployments/open5gs/kpi-scorecard.json"],
               ["Method", "docs/adr/ADR-015-traffic-profiles-and-kpi-scorecard.md"]]
    sheets.append(xlsx.Sheet("README", readme, widths=[34, 120], freeze_header=False))

    std = next((r for r in recs if r["label"] == "standard"), recs[0] if recs else None)
    if std:
        rows = [[(h, "header") for h in ("Slice", "KPI", "Measured", "Unit", "Project target", "Research target",
                                         "Industry benchmark", "Benchmark source", "vs benchmark", "Result", "Score",
                                         "Weight", "Origin")]]
        for r in std["card"]["rows"]:
            txt, style = mark(r["met"])
            rows.append([r["slice"], r["name"], r["measured"], r["unit"], r["project_target"], r["research_target"],
                         r["benchmark"], r["benchmark_source"], r["vs_benchmark"], (txt, style), r["score"], r["weight"], r["origin"]])
        rows.append([])
        rows.append([("Slice scores", "bold")])
        for s in std["card"]["slices"]:
            rows.append([s["slice"], "slice score", s["score"]])
        rows.append(["Overall", "mean of slice scores", std["card"]["overall"]])
        rows.append([])
        rows.append(["Instance", "%s, %s s, started %s" % (std["label"], std["duration_s"], std["started"])])
        sheets.append(xlsx.Sheet("Scorecard (standard)", rows, widths=[8, 58, 12, 8, 14, 15, 16, 26, 12, 8, 8, 8, 8]))

    rows = [[(h, "header") for h in ("Instance", "Parameter", "Value", "URLLC score", "eMBB score", "mMTC score", "Overall")]]
    for r in recs:
        sl = {s["slice"]: s["score"] for s in r["card"]["slices"]}
        rows.append([r["label"], r["param"] or "standard", r["value"], sl.get("URLLC"), sl.get("eMBB"), sl.get("mMTC"), r["card"]["overall"]])
    sheets.append(xlsx.Sheet("Slice scores", rows, widths=[24, 16, 8, 12, 12, 12, 10]))

    for param, title in PARAMS.items():
        sub = [r for r in recs if r["param"] == param]
        if not sub:
            continue
        head = [(param, "header"), ("Instance", "header")] + [(h, "header") for h, _ in COLUMNS] + \
               [(s["name"] + " score", "header") for s in defs["slices"]] + [("Overall", "header")]
        rows = [head]
        for r in sub:
            sl = {s["slice"]: s["score"] for s in r["card"]["slices"]}
            met = {row["id"]: row["met"] for row in r["card"]["rows"]}
            line = [r["value"], r["label"]]
            for _, key in COLUMNS:
                v = r["measured"].get(key)
                kid = next((k["id"] for s in defs["slices"] for k in s["kpis"] if k["measure"] == key and float(k["weight"]) > 0), None)
                line.append((v, mark(met.get(kid))[1]) if kid else v)
            line += [sl.get(s["name"]) for s in defs["slices"]] + [r["card"]["overall"]]
            rows.append(line)
        rows.append([])
        rows.append([("Parameter: " + title, "bold")])
        rows.append(["Green = KPI target met, red = missed (scored KPIs only)."])
        sheets.append(xlsx.Sheet("Sweep - " + param, rows, widths=[10, 22] + [14] * (len(COLUMNS) + len(defs["slices"]) + 1)))
        with open(os.path.join(run_dir, "sweep-%s.csv" % param), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([param, "instance"] + [h for h, _ in COLUMNS] + [s["name"] + " score" for s in defs["slices"]] + ["overall"])
            for r in sub:
                sl = {s["slice"]: s["score"] for s in r["card"]["slices"]}
                w.writerow([r["value"], r["label"]] + [r["measured"].get(k) for _, k in COLUMNS]
                           + [sl.get(s["name"]) for s in defs["slices"]] + [r["card"]["overall"]])

    keys = sorted({k for r in recs for k in r["measured"]})
    rows = [[("Instance", "header"), ("Parameter", "header"), ("Value", "header"), ("Started", "header"), ("Seconds", "header")]
            + [(k, "header") for k in keys]]
    for r in recs:
        rows.append([r["label"], r["param"] or "standard", r["value"], r["started"], r["duration_s"]] + [r["measured"].get(k) for k in keys])
    sheets.append(xlsx.Sheet("All instances", rows, widths=[22, 14, 8, 20, 8] + [16] * len(keys)))

    rows = [[(h, "header") for h in ("Instance", "Device (IMSI …)", "UE address", "DL Mbps")]]
    for r in recs:
        for fl in r["embb_flows"]:
            rows.append([r["label"], fl["device"], fl["ip"], fl["mbps"]])
    sheets.append(xlsx.Sheet("eMBB per device", rows, widths=[22, 16, 14, 10]))

    rows = [[(h, "header") for h in ("Instance", "Slice", "Device (IMSI …)", "Received", "Expected", "Mean latency (ms)")]]
    for r in recs:
        for name, key in (("URLLC", "urllc_per_device"), ("mMTC", "mmtc_per_device")):
            for dev, d in sorted((r.get(key) or {}).items(), key=lambda x: int(x[0])):
                rows.append([r["label"], name, int(dev), d["received"], d["expected"], d.get("owd_mean_ms")])
    sheets.append(xlsx.Sheet("URLLC+mMTC per device", rows, widths=[22, 8, 16, 10, 10, 16]))

    rows = [[(h, "header") for h in ("Slice", "Id", "KPI", "Measure", "Unit", "Better", "Project target", "Research target",
                                      "Benchmark", "Benchmark source", "Benchmark note", "Weight", "Origin", "Why")]]
    for s in defs["slices"]:
        for k in s["kpis"]:
            b = k.get("benchmark") or {}
            rows.append([s["name"], k["id"], k["name"], k["measure"], k["unit"], k["better"], k.get("project_target"),
                         k.get("research_target"), b.get("value"), b.get("source"), (b.get("note"), "wrap"), k["weight"],
                         k["origin"], (k["why"], "wrap")])
    rows.append([])
    rows.append([("Sources", "bold")])
    for src, what in defs["sources"].items():
        rows.append(["", src, (what, "wrap")])
    sheets.append(xlsx.Sheet("KPI definitions", rows, widths=[8, 26, 40, 26, 8, 8, 10, 10, 10, 22, 40, 7, 8, 60]))

    path = os.path.join(run_dir, "kpi-report.xlsx")
    xlsx.write(path, sheets)
    write_markdown(run_dir, recs, defs)
    log("report: %s (%d sheets)" % (os.path.relpath(path, REPO), len(sheets)))
    return path


def write_markdown(run_dir, recs, defs):
    std = next((r for r in recs if r["label"] == "standard"), None)
    lines = ["# KPI run — %s" % os.path.basename(run_dir), ""]
    if std:
        lines += ["## Scorecard — standard instance (%s s, all three slices at once)" % std["duration_s"], "",
                  "| Slice | KPI | Measured | Target | Industry benchmark | Result | Score |", "|---|---|---|---|---|---|---|"]
        for r in std["card"]["rows"]:
            unit = r["unit"]
            fmtv = lambda v: "—" if v is None else ("%.4g %%" % (100 * v) if unit == "ratio" else "%s %s" % (v, unit))
            res = {True: "✅ met", False: "❌ missed", None: "reported"}[r["met"]]
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                r["slice"], r["name"], fmtv(r["measured"]), fmtv(r["target_used"]),
                "—" if r["benchmark"] is None else "%s (%s)" % (fmtv(r["benchmark"]), r["benchmark_source"]), res,
                "—" if r["score"] is None else "%.2f" % r["score"]))
        lines += ["", "Slice scores: " + ", ".join("%s %s" % (s["slice"], "—" if s["score"] is None else "%.2f" % s["score"])
                                                   for s in std["card"]["slices"])
                  + "; overall %s." % ("—" if std["card"]["overall"] is None else "%.2f" % std["card"]["overall"]), ""]
    for param, title in PARAMS.items():
        sub = [r for r in recs if r["param"] == param]
        if not sub:
            continue
        lines += ["## Sweep — %s (%s)" % (param, title), "",
                  "| %s | URLLC mean / p99 (ms) | URLLC loss | eMBB p5 / aggregate (Mbps) | mMTC reg. success | mMTC loss | URLLC · eMBB · mMTC score |" % param,
                  "|---|---|---|---|---|---|---|"]
        for r in sub:
            m = r["measured"]
            sl = {s["slice"]: s["score"] for s in r["card"]["slices"]}
            pct = lambda v: "—" if v is None else "%.3g %%" % (100 * v)
            val = lambda v: "—" if v is None else v
            lines.append("| %s | %s / %s | %s | %s / %s | %s | %s | %s |" % (
                r["value"], val(m.get("urllc.owd_mean_ms")), val(m.get("urllc.owd_p99_ms")), pct(m.get("urllc.loss_ratio")),
                val(m.get("embb.dl_p5_mbps")), val(m.get("embb.dl_aggregate_mbps")), pct(m.get("mmtc.registration_success")),
                pct(m.get("mmtc.loss_ratio")), " · ".join("—" if sl.get(s["name"]) is None else "%.2f" % sl[s["name"]] for s in defs["slices"])))
        lines.append("")
    with open(os.path.join(run_dir, "scorecard.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ------------------------------------------------------------------ main

def new_run_dir():
    """A new run directory — or, with RUN_DIR set, an existing one to add (re-run) instances to."""
    global OUT
    if os.environ.get("RUN_DIR"):
        OUT = os.path.abspath(os.environ["RUN_DIR"])
        if not os.path.exists(os.path.join(OUT, "instances.jsonl")):
            raise SystemExit("RUN_DIR %s has no instances.jsonl" % OUT)
        return OUT
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    OUT = os.path.join(REPO, "docs", "evidence", "open5gs-kpi", ts)
    os.makedirs(OUT, exist_ok=True)
    commit = sh(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()
    dirty = sh(["git", "diff", "--quiet"], cwd=REPO).returncode != 0
    meta = {"started": ts, "commit": commit + (" + uncommitted changes" if dirty else ""),
            "host": "%d CPUs, %d MiB" % (os.cpu_count(), os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20),
            "duration_s": DURATION, "standard_duration_s": STANDARD_DURATION}
    json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=2)
    return OUT


def main(argv):
    global OUT
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "report":
        OUT = argv[1]
        report(argv[1])
        return 0
    if wait_ues(100, timeout=180) < 100:
        print("the stack does not have 100 UEs with sessions — start it first (make o5gs-up)")
        return 1
    new_run_dir()
    log("run directory %s" % os.path.relpath(OUT, REPO))
    if cmd == "standard":
        standard()
    elif cmd == "sweep":
        sweep(argv[1], [float(v) if "." in v else int(v) for v in argv[2:]])
    elif cmd == "all":
        standard()
        sweep("embb_ues", [0, 5, 10, 20])
        sweep("mmtc_interval", [1, 5, 10, 30])
        sweep("mmtc_devices", [70, 140, 210])
    else:
        print(__doc__)
        return 1
    report(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
