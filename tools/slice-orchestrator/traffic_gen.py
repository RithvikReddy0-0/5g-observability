#!/usr/bin/env python3
"""
traffic_gen.py — generate traffic demands from REAL registered UEs and send them to the
orchestrator for slice allocation.

This replaces the old project's ue_client.py, which sent HTTP to a Flask app that looked the
request type up in a dictionary and called the result a "slice". Nothing there touched 5G.

What is different here:
  * the SUPIs are real subscribers provisioned in the free5GC subscriber database
  * each SUPI's allowed slice comes from that database, so a UE cannot be placed on a slice
    it is not provisioned for
  * the decision is made by the orchestrator against real per-slice capacity, and can be
    REFUSED — which is the "divide into the resources that are actually available" part

Still honest about the limit: these are demands for capacity, not packets. No user traffic
crosses the 5G data path, because that needs a UPF and the gtp5g kernel module.

Usage:
  python3 traffic_gen.py                 # run until interrupted
  DURATION=60 RATE=5 python3 traffic_gen.py
"""

import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request

ORCH = os.environ.get("ORCH_URL", "http://localhost:9110")
DB_CONTAINER = os.environ.get("DB_CONTAINER", "mongodb")
RATE = float(os.environ.get("RATE", "4"))          # demands per second
DURATION = float(os.environ.get("DURATION", "0"))  # 0 = run forever
# Mix is weighted so bulk classes dominate, as they would in reality.
MIX = os.environ.get("MIX", "video:4 file:3 blog:2 control:1")


def real_supis():
    """Read the provisioned subscribers from the live core."""
    js = ('var a=[];db["subscriptionData.provisionedData.amData"]'
          '.find({},{_id:0,ueId:1}).forEach(function(d){a.push(d.ueId);});'
          'print(JSON.stringify(a));')
    try:
        out = subprocess.run(
            ["docker", "exec", "-i", DB_CONTAINER, "mongo", "free5gc", "--quiet", "--eval", js],
            capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if line.strip().startswith("["):
                return json.loads(line.strip())
    except Exception as exc:
        print("could not read subscribers: %s" % exc, file=sys.stderr)
    return []


def weighted_classes():
    pool = []
    for item in MIX.split():
        name, _, w = item.partition(":")
        pool.extend([name] * int(w or 1))
    return pool


def post(supi, cls):
    body = json.dumps({"supi": supi, "traffic_class": cls}).encode()
    req = urllib.request.Request(ORCH + "/request", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:            # 409 = refused, still a real answer
        try:
            return json.loads(e.read())
        except Exception:
            return {"admitted": False, "reason": "http %s" % e.code}
    except Exception as exc:
        return {"admitted": False, "reason": str(exc)}


def main():
    supis = real_supis()
    if not supis:
        print("No provisioned subscribers found — is the core up and provisioned?",
              file=sys.stderr)
        return 1
    pool = weighted_classes()
    print("driving %d real subscribers at ~%g demands/s against %s" % (len(supis), RATE, ORCH))
    print("traffic mix: %s\n" % MIX)

    started = time.time()
    admitted = refused = 0
    reasons = {}
    try:
        while True:
            if DURATION and time.time() - started >= DURATION:
                break
            supi = random.choice(supis)
            cls = random.choice(pool)
            res = post(supi, cls)
            if res.get("admitted"):
                admitted += 1
                s = res["slice"]
                print("  ADMIT  %-8s %s -> %-6s (sst %d/%s)  %.1f/%.0f Mbps"
                      % (cls, supi[-4:], s["name"], s["sst"], s["sd"],
                         res["slice_used_mbps"], res["slice_capacity_mbps"]))
            else:
                refused += 1
                why = res.get("reason", "?")
                key = why.split(":")[0][:38]
                reasons[key] = reasons.get(key, 0) + 1
                print("  REFUSE %-8s %s -> %s" % (cls, supi[-4:], why))
            time.sleep(1.0 / RATE if RATE > 0 else 0.25)
    except KeyboardInterrupt:
        pass

    total = admitted + refused
    print("\n--- summary ---")
    print("  demands : %d" % total)
    print("  admitted: %d (%.0f%%)" % (admitted, 100.0 * admitted / total if total else 0))
    print("  refused : %d (%.0f%%)" % (refused, 100.0 * refused / total if total else 0))
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("      %-40s %d" % (r, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
