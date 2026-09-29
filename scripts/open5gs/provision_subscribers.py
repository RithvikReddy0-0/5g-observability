#!/usr/bin/env python3
"""
provision_subscribers.py — write the 100 subscribers into the Open5GS database.

Every QoS value comes from deployments/slices.env, the same file the free5GC provisioning and
the slice orchestrator read, so the two cores are provisioned with identical slice
definitions and a comparison between them is fair.

How many subscribers each slice gets is O5GS_UES in that file (Phase 2b: eMBB 20, URLLC 10,
mMTC 70). IMSIs are assigned contiguously in slice order, A then B then C — the same rule the
UE container follows (deployments/open5gs/ran/ue-entrypoint.sh), checked in CI.

A subscriber's home slice is its default S-NSSAI and decides which PDU session the UE opens.
SLICE_<X>_ALSO_PERMITTED lists the other slices it may use: eMBB and URLLC devices may use
each other's slice, which gives the orchestrator a real choice; IoT devices are mMTC only.

    scripts/open5gs/provision_subscribers.py            # upsert all 100
    scripts/open5gs/provision_subscribers.py --dry-run  # print the mongo script instead

Idempotent: records are replaced by IMSI, so re-running never creates duplicates. The
authentication SQN is reset on every run — a stale SQN fails 5G-AKA with a symptom that
looks exactly like a wrong key (see docs/RESULTS.md §4.3).
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SLICES_ENV = os.path.join(REPO, "deployments", "slices.env")

MCC, MNC = "208", "93"
KEY = "8baf473f2f8fd09487cccbd7097c6862"
OPC = "8e27b6af0e692e750f32667a3b14605d"

# Open5GS stores bitrates as {value, unit}; unit 0=bps 1=Kbps 2=Mbps 3=Gbps 4=Tbps.
UNITS = {"bps": 0, "kbps": 1, "mbps": 2, "gbps": 3, "tbps": 4}

# lib/proto/types.h: OGS_5GC_PRE_EMPTION_DISABLED 1, OGS_5GC_PRE_EMPTION_ENABLED 2
PREEMPT_DISABLED, PREEMPT_ENABLED = 1, 2

# Each slice's DNN selects its UE address pool (deployments/open5gs/config/smf.yaml).
DNN = {"A": "internet", "B": "urllc", "C": "iot"}


def load_env(path):
    env = {}
    for line in open(path, encoding="utf-8"):
        m = re.match(r'\s*([A-Z0-9_]+)=("?)(.*?)\2\s*(#.*)?$', line)
        if m:
            env[m.group(1)] = m.group(3)
    return env


def bitrate(text):
    value, unit = text.split()
    return {"value": int(value), "unit": UNITS[unit.lower()]}


def slice_def(env, s):
    p = "SLICE_%s_" % s
    return {
        "key": s,
        "name": env[p + "NAME"],
        "sst": int(env[p + "SST"]),
        "sd": env[p + "SD"],
        "fiveqi": int(env[p + "5QI"]),
        "arp": int(env[p + "ARP_PRIORITY"]),
        "cap": PREEMPT_ENABLED if env.get(p + "ARP_PREEMPT_CAP") == "MAY_PREEMPT"
               else PREEMPT_DISABLED,
        "vuln": PREEMPT_ENABLED if env.get(p + "ARP_PREEMPT_VULN") == "PRE_EMPTABLE"
                else PREEMPT_DISABLED,
        "session_dl": bitrate(env[p + "SESSION_AMBR_DL"]),
        "session_ul": bitrate(env[p + "SESSION_AMBR_UL"]),
        "ue_dl": bitrate(env[p + "UE_AMBR_DL"]),
        "ue_ul": bitrate(env[p + "UE_AMBR_UL"]),
        "also": list(env.get(p + "ALSO_PERMITTED", "")),
    }


def slice_entry(sl, default):
    return {
        "sst": sl["sst"],
        "sd": sl["sd"],
        "default_indicator": default,
        "session": [{
            "name": DNN[sl["key"]],
            "type": 3,                                    # IPv4v6 allowed; UE requests IPv4
            "qos": {
                "index": sl["fiveqi"],
                "arp": {
                    "priority_level": sl["arp"],
                    "pre_emption_capability": sl["cap"],
                    "pre_emption_vulnerability": sl["vuln"],
                },
            },
            "ambr": {"downlink": sl["session_dl"], "uplink": sl["session_ul"]},
            "pcc_rule": [],
        }],
    }


def subscriber(imsi, home, others):
    return {
        "schema_version": 1,
        "imsi": imsi,
        "msisdn": [], "imeisv": [], "mme_host": [], "mm_realm": [], "purge_flag": [],
        "slice": [slice_entry(home, True)] + [slice_entry(o, False) for o in others],
        "security": {"k": KEY, "op": None, "opc": OPC, "amf": "8000", "sqn": 0},
        "ambr": {"downlink": home["ue_dl"], "uplink": home["ue_ul"]},
        "access_restriction_data": 32,
        "network_access_mode": 0,
        "subscriber_status": 0,
        "operator_determined_barring": 0,
        "subscribed_rau_tau_timer": 12,
        "__v": 0,
    }


def plan(env):
    """[(slice letter, count)] in IMSI order, from O5GS_UES ("A=20 B=10 C=70").

    The environment variable O5GS_UES overrides slices.env for one run — used only by
    scripts/open5gs/scale_test.sh to provision extra mMTC devices while it measures scale.
    """
    out = []
    for item in os.environ.get("O5GS_UES", env["O5GS_UES"]).split():
        letter, count = item.split("=")
        if "SLICE_%s_SST" % letter not in env:
            raise SystemExit("O5GS_UES names slice %s, which slices.env does not define" % letter)
        out.append((letter, int(count)))
    return out


def build_script(env):
    slices = {letter: slice_def(env, letter) for letter, _ in plan(env)}
    docs, n = [], 1
    for letter, count in plan(env):
        home = slices[letter]
        others = [slices[o] if o in slices else slice_def(env, o) for o in home["also"]]
        for _ in range(count):
            docs.append(subscriber("%s%s%010d" % (MCC, MNC, n), home, others))
            n += 1

    # NumberInt/NumberLong matter: Open5GS reads these with bson_iter_int32/int64 and a
    # plain JS number would be stored as a double.
    js = ["var docs = %s;" % json.dumps(docs),
          """
function ints(o) {
  for (var k in o) {
    var v = o[k];
    if (v === null) continue;
    if (typeof v === 'number') o[k] = (k === 'sqn') ? NumberLong(v) : NumberInt(v);
    else if (typeof v === 'object') ints(v);
  }
  return o;
}
var n = 0;
docs.forEach(function (d) {
  ints(d);
  d.slice.forEach(function (s) { s._id = new ObjectId();
                                 s.session.forEach(function (x) { x._id = new ObjectId(); }); });
  db.subscribers.replaceOne({imsi: d.imsi}, d, {upsert: true});
  n++;
});
print('upserted ' + n + ' subscribers');
print('total in db: ' + db.subscribers.count());
db.subscribers.aggregate([
  {$unwind: '$slice'}, {$match: {'slice.default_indicator': true}},
  {$group: {_id: {sst: '$slice.sst', sd: '$slice.sd', dnn: {$arrayElemAt: ['$slice.session.name', 0]},
                  fiveqi: {$arrayElemAt: ['$slice.session.qos.index', 0]}}, subscribers: {$sum: 1}}},
  {$sort: {'_id.sst': 1}}
]).forEach(function (r) { print('  home slice sst ' + r._id.sst + '/' + r._id.sd + '  dnn=' + r._id.dnn +
                                '  5qi=' + r._id.fiveqi + '  -> ' + r.subscribers + ' subscribers'); });
"""]
    return "\n".join(js), len(docs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--container", default=os.environ.get("O5GS_DB", "o5gs-mongodb"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--kubernetes", metavar="NAMESPACE",
                    help="provision the Kubernetes deployment instead: kubectl exec into deploy/mongodb")
    args = ap.parse_args()

    script, count = build_script(load_env(SLICES_ENV))
    if args.dry_run:
        print(script)
        return 0

    if args.kubernetes:
        target = "deploy/mongodb in namespace %s" % args.kubernetes
        exec_prefix = ["kubectl", "-n", args.kubernetes, "exec", "-i", "deploy/mongodb", "--"]
    else:
        target = args.container
        exec_prefix = ["docker", "exec", "-i", args.container]
    print("provisioning %d subscribers into %s (database open5gs)" % (count, target))
    # Run as a script FILE, not piped stdin: the legacy mongo shell reads stdin line by line
    # with a ~4 KB line limit, which silently truncates the subscriber array and then fails
    # every following line with an unrelated-looking SyntaxError.
    r = subprocess.run(exec_prefix + ["sh", "-c",
                                      "cat > /tmp/provision.js && mongo open5gs --quiet /tmp/provision.js"],
                       input=script, text=True, capture_output=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0 or "Error" in r.stdout or "exception" in r.stdout:
        sys.stderr.write(r.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
