#!/usr/bin/env python3
"""
cgroup_stats.py — CPU and memory per container, read straight from the kernel's cgroups.

`docker stats` takes about two seconds per sample and costs CPU of its own, which is the very
thing being measured. This reads each container's cgroup v2 files instead: cpu.stat
(usage_usec, cumulative) and memory.current, every INTERVAL seconds, until DURATION passes or
UNTIL_FILE appears. CPU is reported as % of one core, like `docker stats`.

    scripts/open5gs/cgroup_stats.py --duration 20 o5gs-amf o5gs-smf
    scripts/open5gs/cgroup_stats.py --until-file /tmp/stop --json o5gs-amf ...

Output: one line per container — avg and peak CPU %, peak memory MiB — or, with --json, the
same as a JSON object keyed by container name. Used by scripts/open5gs/scale_test.sh.
"""

import argparse
import json
import os
import subprocess
import sys
import time


def cgroup_dir(container):
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", container],
                         capture_output=True, text=True).stdout.strip()
    for d in ("/sys/fs/cgroup/system.slice/docker-%s.scope" % cid, "/sys/fs/cgroup/docker/%s" % cid):
        if os.path.exists(os.path.join(d, "cpu.stat")):
            return d
    return None


def read_usage(d):
    with open(os.path.join(d, "cpu.stat")) as f:
        for line in f:
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    return 0


def read_mem(d):
    try:
        with open(os.path.join(d, "memory.current")) as f:
            return int(f.read())
    except OSError:
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("containers", nargs="+")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = until --until-file")
    ap.add_argument("--until-file")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not a.duration and not a.until_file:
        ap.error("give --duration or --until-file")

    dirs = {c: cgroup_dir(c) for c in a.containers}
    live = {c: d for c, d in dirs.items() if d}
    stats = {c: {"samples": [], "mem_peak": 0} for c in live}
    last = {c: (time.monotonic(), read_usage(d)) for c, d in live.items()}
    started = time.monotonic()
    while True:
        time.sleep(a.interval)
        for c, d in live.items():
            try:
                t, u = time.monotonic(), read_usage(d)
            except OSError:                      # container restarted: new cgroup
                continue
            t0, u0 = last[c]
            if t > t0 and u >= u0:
                stats[c]["samples"].append(100.0 * (u - u0) / 1e6 / (t - t0))
            last[c] = (t, u)
            stats[c]["mem_peak"] = max(stats[c]["mem_peak"], read_mem(d))
        if a.duration and time.monotonic() - started >= a.duration:
            break
        if a.until_file and os.path.exists(a.until_file):
            break

    out = {}
    for c in a.containers:
        s = stats.get(c)
        if not s or not s["samples"]:
            out[c] = {"cpu_avg": None, "cpu_peak": None, "mem_peak_mib": None}
            continue
        out[c] = {"cpu_avg": round(sum(s["samples"]) / len(s["samples"]), 1),
                  "cpu_peak": round(max(s["samples"]), 1),
                  "mem_peak_mib": round(s["mem_peak"] / 1048576.0, 1)}
    if a.json:
        print(json.dumps(out))
    else:
        for c, v in out.items():
            print("%-18s cpu avg %6s %%  peak %6s %%  mem peak %7s MiB"
                  % (c, v["cpu_avg"], v["cpu_peak"], v["mem_peak_mib"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
