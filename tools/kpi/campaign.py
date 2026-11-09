#!/usr/bin/env python3
"""
campaign.py — a same-day comparison of scheduler settings with standard KPI runs, interleaved.

Each ARM is a name and a POST :9114/config body. Every round runs every arm once; the order
alternates (forward, reverse, forward, ...), so drift during the day falls evenly on the arms (ABBA).
Before each run the arm's config is applied and confirmed in /state; after it the scheduler's mode
and /state are saved into the run directory, and every UE's data path is checked: a UE left without
one (a radio-link failure) is re-attached before the next run, and counted in the index.

    python3 tools/kpi/campaign.py <evidence dir> <rounds> <arm>=<json> [<arm>=<json> ...]
    python3 tools/kpi/campaign.py open5gs-cell-capacity 4 \\
        'cell250={"mode":"sliced","cell_dl_mbps":250,"cell_ul_mbps":100}' \\
        'cell600={"mode":"sliced","cell_dl_mbps":600,"cell_ul_mbps":100}'

Writes docs/evidence/<dir>/campaign.tsv (seq, round, arm, run_dir, started_utc, runner_exit,
broken_after, restored) and kpi/<run>/ (one tools/kpi/run_kpis.py standard run each); read it with
tools/kpi/compare_arms.py. At the end the scheduler is put back to the settings in slices.env.
FIRST_ROUND=<n> adds rounds n, n+1, ... to an existing campaign (the index is appended to).
Run from the repository root on the Docker host, with the stack up and nothing else running.
"""

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
API = os.environ.get("SCHED_API", "http://localhost:9114")
UE = os.environ.get("UE_CONTAINER", "o5gs-ue")
EXPECTED_UES = int(os.environ.get("EXPECTED_UES", "100"))


def order(arms, rounds, first=1):
    """[(round, arm), ...]: every arm once per round, odd rounds forward, even rounds reversed.
    `first` numbers the rounds from there, to add rounds to an existing campaign."""
    seq = []
    for r in range(first, first + rounds):
        seq += [(r, a) for a in (arms if r % 2 else arms[::-1])]
    return seq


def parse_arms(specs):
    arms = {}
    for spec in specs:
        name, _, body = spec.partition("=")
        if not name or not body:
            raise ValueError("arm must be <name>=<json>: %r" % spec)
        arms[name] = json.loads(body)
    return arms


def api(path, body=None):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def applied(cfg, st, errors_before):
    """True when /state shows the config (mode and, if given, the cell) and no new error since the
    POST (`errors` is a running count)."""
    if st.get("mode") != cfg.get("mode", st.get("mode")):
        return False
    cell = st.get("cell_mbps") or {}
    for key, d in (("cell_dl_mbps", "downlink"), ("cell_ul_mbps", "uplink")):
        if key in cfg and float(cell.get(d, -1)) != float(cfg[key]):
            return False
    return st.get("errors", 0) == errors_before


def apply(cfg):
    errors_before = api("/state").get("errors", 0)
    api("/config", cfg)
    for _ in range(30):
        st = api("/state")
        if applied(cfg, st, errors_before):
            return st
        time.sleep(2)
    raise RuntimeError("scheduler did not apply %s: %s" % (cfg, api("/state")))


def broken_ues():
    """UEs whose data path is down (ping each UE interface's UPF gateway), plus missing ones."""
    script = ("for l in $(ip -4 -o addr show | awk '/uesimtun/ {print $2\"@\"$4}'); do "
              "i=${l%%@*}; a=${l#*@}; gw=$(echo $a | cut -d. -f1,2).0.1; "
              "( ping -n -c 2 -W 1 -I $i $gw >/dev/null 2>&1 || echo $i ) & done; wait")
    out = subprocess.run(["docker", "exec", UE, "sh", "-c", script], capture_output=True, text=True, timeout=120).stdout
    homes = subprocess.run(["docker", "exec", UE, "sh", "-c", "ip -4 -o addr show | grep -c uesimtun"],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    return [x for x in out.split() if x.startswith("uesimtun")] + ["missing"] * max(0, EXPECTED_UES - int(homes or 0))


def log(fh, msg):
    line = "%s %s" % (datetime.datetime.utcnow().strftime("%H:%M:%SZ"), msg)
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def main(argv):
    if len(argv) < 3 or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    evidence, rounds, arms = argv[0], int(argv[1]), parse_arms(argv[2:])
    first = int(os.environ.get("FIRST_ROUND", "1"))
    base = os.path.join(REPO, "docs", "evidence", evidence)
    kpi_dir = os.path.join(base, "kpi")
    os.makedirs(kpi_dir, exist_ok=True)
    index = os.path.join(base, "campaign.tsv")
    new = not os.path.exists(index)
    with open(index, "a", encoding="utf-8") as tsv, open(os.path.join(base, "campaign.log"), "a", encoding="utf-8") as lg:
        if new:
            tsv.write("seq\tround\tarm\trun_dir\tstarted_utc\trunner_exit\tbroken_after\trestored\n")
            tsv.flush()              # the row count below reads the file
        commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        log(lg, "campaign %s: %d rounds, arms %s, commit %s" % (evidence, rounds, json.dumps(arms), commit))
        done = sum(1 for _ in open(index, encoding="utf-8")) - 1
        for seq, (rnd, arm) in enumerate(order(list(arms), rounds, first), done + 1):
            st = apply(arms[arm])
            log(lg, "#%d round %d %s: applied %s cell %s" % (seq, rnd, arm, st["mode"], st.get("cell_mbps")))
            before = set(os.listdir(kpi_dir))
            started = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            env = dict(os.environ, KPI_EVIDENCE=os.path.join(evidence, "kpi"))
            rc = subprocess.run([sys.executable, os.path.join(REPO, "tools", "kpi", "run_kpis.py"), "standard"],
                                cwd=REPO, env=env).returncode
            made = sorted(set(os.listdir(kpi_dir)) - before)
            run_dir = made[-1] if made else ""
            if run_dir:
                st = api("/state")
                with open(os.path.join(kpi_dir, run_dir, "scheduler-mode.txt"), "w", encoding="utf-8") as f:
                    f.write("%s %s cell %s\n" % (arm, st["mode"], json.dumps(st.get("cell_mbps"))))
                with open(os.path.join(kpi_dir, run_dir, "scheduler-state.json"), "w", encoding="utf-8") as f:
                    json.dump(st, f, indent=1)
            broken = broken_ues()
            restored = 0
            if broken:
                log(lg, "#%d: %d UEs without a data path (%s): re-attaching" % (seq, len(broken), " ".join(broken[:8])))
                subprocess.run(["bash", os.path.join(REPO, "scripts", "open5gs", "start_ues.sh")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
                restored = 1
            log(lg, "#%d round %d %s: run %s exit %d, broken after %d" % (seq, rnd, arm, run_dir or "-", rc, len(broken)))
            tsv.write("%d\t%d\t%s\t%s\t%s\t%d\t%d\t%d\n" % (seq, rnd, arm, run_dir, started, rc, len(broken), restored))
            tsv.flush()
        # back to the deployment's own settings (slices.env)
        env = {}
        for line in open(os.path.join(REPO, "deployments", "slices.env"), encoding="utf-8"):
            line = line.split("#")[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
        default = {"mode": env.get("SCHED_MODE", "sliced"), "cell_dl_mbps": float(env["SCHED_CELL_DL_MBPS"]),
                   "cell_ul_mbps": float(env["SCHED_CELL_UL_MBPS"])}
        st = apply(default)
        log(lg, "campaign done; scheduler back to slices.env: %s cell %s" % (st["mode"], st.get("cell_mbps")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
