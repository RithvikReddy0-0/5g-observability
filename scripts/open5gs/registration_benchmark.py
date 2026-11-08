#!/usr/bin/env python3
"""
registration_benchmark.py — UE registration time for a burst of N UEs, measured
the way Mukute et al. (IEEE Access 12, 2024, doi 10.1109/ACCESS.2024.3441725) measure it, so the
numbers can be put next to theirs.

Their macro-benchmark: N UEs register "in a burst"; reported are (a) the total time for all N UEs
and (b) the average time per UE, for N up to 300, for registration and de-registration together.
Here only registration is timed: UERANSIM's `deregister normal` makes the UE register again at once,
its log does not keep the de-registration, and the AMF logs no accept time, so de-registration has
no clean end point on this stack (checked on one UE, 2026-11-08). For each N:

  1. Subscribers are re-provisioned (same starting state every run: network SQN reset).
  2. The UE container restarts with a plan of exactly N UEs and START_BATCH=N: every nr-ue process
     is launched at once, i.e. all N registrations are in flight together.
  3. Registration, per UE, from its own log: "MM-REGISTER-INITIATED" -> "Initial Registration is
     successful". Total = first initiation -> last success (log clock), and the same span timed
     with this script's monotonic clock by polling the logs (POLL s resolution) — the VM's wall
     clock has been seen to step (docs/evidence/open5gs-clock).
  4. Peak CPU of the control-plane NFs during the registration burst (cgroups, cgroup_stats.py).

N <= 20: eMBB devices only; N <= 30: eMBB 20 + URLLC; above: eMBB 20 + URLLC 10 + mMTC N-30
(extra mMTC subscribers provisioned for N > 100, removed at the end). The standard 100 UEs are
restored at the end (start_ues.sh), even after a failure.

Usage:  python3 scripts/open5gs/registration_benchmark.py            NS="1 10 50 100 200" REPS=3
Writes: docs/evidence/open5gs-registration-benchmark/<UTC ts>/ meta.json, results.jsonl, results.json, results.csv
"""

import csv
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
UE = "o5gs-ue"
NS = [int(x) for x in os.environ.get("NS", "1 10 50 100 200").split()]
REPS = int(os.environ.get("REPS", "3"))
POLL = 0.1
TIMEOUT = int(os.environ.get("TIMEOUT", "300"))
CP = ["o5gs-amf", "o5gs-smf", "o5gs-ausf", "o5gs-udm", "o5gs-udr", "o5gs-nrf", "o5gs-scp", "o5gs-pcf", "o5gs-mongodb"]
FIRST_IMSI = 208930000000001
TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d) (\d\d):(\d\d):(\d\d\.\d+)\]")


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def plan_for(n):
    a = min(n, 20)
    b = min(max(n - 20, 0), 10)
    c = max(n - 30, 0)
    parts = [("ue-slice-a.yaml", a, "10.45.0.1", "eMBB"), ("ue-slice-b.yaml", b, "10.46.0.1", "URLLC"),
             ("ue-slice-c.yaml", c, "10.47.0.1", "mMTC")]
    return " ".join("%s:%d:%s:%s" % p for p in parts if p[1] > 0), (a, b, c)


def provision(a, b, c):
    # provisioning is by slice in IMSI order; the plan above uses the same order, so the first N
    # subscribers of a standard-or-larger provisioning are exactly the N UEs of the plan
    env = dict(os.environ, O5GS_UES="A=20 B=10 C=%d" % max(70, c))
    r = sh([sys.executable, os.path.join(HERE, "provision_subscribers.py")], env=env)
    if r.returncode != 0:
        raise RuntimeError("provisioning failed: " + r.stderr[-300:])


def counts(pattern_a, pattern_b):
    r = sh(["docker", "exec", UE, "sh", "-c",
            'grep -l "%s" /tmp/ue-2089*.log 2>/dev/null | wc -l; grep -l "%s" /tmp/ue-2089*.log 2>/dev/null | wc -l'
            % (pattern_a, pattern_b)])
    try:
        x, y = r.stdout.split()
        return int(x), int(y)
    except ValueError:
        return 0, 0


def log_times(pattern_a, pattern_b):
    """{imsi: (t_a, t_b)} in seconds (log clock) for the first line matching each pattern."""
    r = sh(["docker", "exec", UE, "sh", "-c",
            'for f in /tmp/ue-2089*.log; do i=${f#/tmp/ue-}; i=${i%%.log}; '
            'a=$(grep -m1 "%s" "$f" | cut -c1-25); b=$(grep -m1 "%s" "$f" | cut -c1-25); '
            'echo "$i|$a|$b"; done' % (pattern_a, pattern_b)])
    out = {}
    for line in r.stdout.splitlines():
        p = line.split("|")
        if len(p) != 3:
            continue
        ts = []
        for s in p[1:]:
            m = TS_RE.match(s)
            ts.append(int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4)) if m else None)
        out[p[0]] = tuple(ts)
    return out


def span(times):
    starts = [a for a, b in times.values() if a is not None]
    ends = [b for a, b in times.values() if b is not None]
    per = [b - a for a, b in times.values() if a is not None and b is not None]
    if not per:
        return {"ues": 0}
    return {"ues": len(per), "total_s": round(max(ends) - min(starts), 3),
            "launch_spread_s": round(max(starts) - min(starts), 3),
            "per_ue_mean_s": round(statistics.mean(per), 3), "per_ue_p50_s": round(statistics.median(per), 3),
            "per_ue_max_s": round(max(per), 3), "per_ue_min_s": round(min(per), 3)}


def wait_count(pattern_a, pattern_b, n, timeout):
    """Monotonic time of the first pattern_a anywhere, and of n UEs with pattern_b."""
    t_first = t_all = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        ca, cb = counts(pattern_a, pattern_b)
        now = time.monotonic()
        if t_first is None and ca > 0:
            t_first = now
        if cb >= n:
            t_all = now
            break
        time.sleep(POLL)
    return t_first, t_all


def one(n, rep, out):
    plan, (a, b, c) = plan_for(n)
    provision(a, b, c)
    override = 'UE_PLAN="%s"\nSTART_BATCH=%d\n' % (plan, n)
    sh(["docker", "exec", "-i", UE, "sh", "-c", "cat > /tmp/ue-plan.override"], input=override, check=True)
    stop = "/tmp/o5gs-regbench-stop"
    if os.path.exists(stop):
        os.remove(stop)
    cpu = subprocess.Popen([sys.executable, os.path.join(HERE, "cgroup_stats.py"), "--json", "--until-file", stop] + CP,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # the entrypoint clears old logs, but only once it runs: the first poll after `docker restart`
    # could still count the previous run's registrations, so they go first
    sh(["docker", "exec", UE, "sh", "-c", "rm -f /tmp/ue-2089*.log /tmp/ues/startup"])
    sh(["docker", "restart", UE], check=True)
    t_first, t_all = wait_count("MM-REGISTER-INITIATED", "Initial Registration is successful", n, TIMEOUT)
    open(stop, "w").close()
    try:
        cpu_out = json.loads(cpu.communicate(timeout=30)[0] or "{}")
    except Exception:
        cpu_out = {}
    times = log_times("MM-REGISTER-INITIATED", "Initial Registration is successful")
    reg = span(times)
    # outliers: keep the log of a UE that took > 5 s or started > 10 s after the first one
    t0 = min((a for a, b in times.values() if a is not None), default=None)
    for imsi, (a, b) in times.items():
        if a is not None and b is not None and (b - a > 5 or a - t0 > 10):
            log = sh(["docker", "exec", UE, "cat", "/tmp/ue-%s.log" % imsi]).stdout
            with open(os.path.join(out, "outlier-n%d-rep%d-%s.log" % (n, rep, imsi)), "w") as f:
                f.write(log)
    reg["total_monotonic_s"] = round(t_all - t_first, 3) if t_first and t_all else None
    failed = sh(["docker", "exec", UE, "sh", "-c", 'cat /tmp/ue-2089*.log | grep -c "Initial Registration failed"']).stdout.strip()
    reg["failures_logged"] = int(failed or 0)
    rec = {"n": n, "rep": rep, "plan": plan, "registration": reg,
           "per_ue": {k: [round(a, 3) if a is not None else None, round(b, 3) if b is not None else None]
                      for k, (a, b) in sorted(times.items())},
           "cpu_peak": {k.replace("o5gs-", ""): v.get("cpu_peak") for k, v in cpu_out.items()},
           "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print("N=%-4d rep %d  registration total %s s (monotonic %s) mean/UE %s s p50 %s max %s, %d/%d ok, failures %s, "
          "launch spread %s s" % (
              n, rep, reg.get("total_s"), reg.get("total_monotonic_s"), reg.get("per_ue_mean_s"), reg.get("per_ue_p50_s"),
              reg.get("per_ue_max_s"), reg.get("ues", 0), n, reg["failures_logged"], reg.get("launch_spread_s")), flush=True)
    with open(os.path.join(out, "results.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def restore():
    print("restore: the standard 100 UEs", flush=True)
    sh(["docker", "exec", "o5gs-mongodb", "mongo", "open5gs", "--quiet", "--eval",
        'db.subscribers.deleteMany({imsi: {$gt: "208930000000100"}})'])
    sh(["docker", "exec", UE, "rm", "-f", "/tmp/ue-plan.override"])
    r = sh(["bash", os.path.join(HERE, "start_ues.sh")], timeout=900)
    print("  " + " · ".join(l.strip() for l in r.stdout.splitlines() if "attached in" in l or "interfaces" in l), flush=True)


def write(out, recs):
    rows = []
    for r in recs:
        g = r["registration"]
        rows.append([r["n"], r["rep"], g.get("ues"), g.get("total_s"), g.get("total_monotonic_s"), g.get("per_ue_mean_s"),
                     g.get("per_ue_p50_s"), g.get("per_ue_max_s"), g.get("launch_spread_s"), g.get("failures_logged"),
                     r["cpu_peak"].get("amf"), r["cpu_peak"].get("scp"), r["cpu_peak"].get("mongodb"), r["cpu_peak"].get("udm")])
    head = ["n", "rep", "registered", "reg_total_s", "reg_total_monotonic_s", "reg_mean_per_ue_s", "reg_p50_per_ue_s",
            "reg_max_per_ue_s", "launch_spread_s", "reg_failures", "amf_cpu_peak", "scp_cpu_peak", "mongodb_cpu_peak", "udm_cpu_peak"]
    with open(os.path.join(out, "results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)
    json.dump(recs, open(os.path.join(out, "results.json"), "w"), indent=2)


def main():
    ts = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
    out = os.path.join(REPO, "docs", "evidence", "open5gs-registration-benchmark", ts)
    os.makedirs(out, exist_ok=True)
    commit = sh(["git", "rev-parse", "HEAD"], cwd=REPO).stdout.strip()
    meta = {"started": ts, "commit": commit, "ns": NS, "reps": REPS, "poll_s": POLL,
            "host": "%d CPUs, %d MiB" % (os.cpu_count(), os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20),
            "open5gs": "v2.8.0", "ueransim": "v3.3.0"}
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=2)
    recs = []
    try:
        for rep in range(1, REPS + 1):
            order = NS if rep % 2 else NS[::-1]
            for n in order:
                recs.append(one(n, rep, out))
                write(out, recs)
    finally:
        restore()
        write(out, recs)
    print("evidence: %s" % os.path.relpath(out, REPO), flush=True)


if __name__ == "__main__":
    main()
