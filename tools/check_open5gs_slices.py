#!/usr/bin/env python3
"""
check_open5gs_slices.py — every UE's slice and DNN is served consistently by every plane.

ADR-010/011. A UE requesting an S-NSSAI that the AMF or NSSF does not support is rejected at
registration; a UE whose gNB does not advertise its slice cannot use it; a DNN the SMF has no
pool for, or routes to a UPF without that pool, fails at session establishment. Each of these
fails far from the file that is wrong. This checks them all from the committed configuration,
with no stack running.

Used by CI (.github/workflows/integrity.yml) and by tests/acceptance-open5gs.sh.
Usage: tools/check_open5gs_slices.py [deployments/open5gs]
"""

import glob
import os
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm(sst, sd):
    return int(sst), (int(str(sd), 16) if isinstance(sd, str) else int(sd))


def main(base):
    load = lambda f: yaml.safe_load(open(f, encoding="utf-8"))
    amf = load(f"{base}/config/amf.yaml")
    nssf = load(f"{base}/config/nssf.yaml")
    smf = load(f"{base}/config/smf.yaml")
    upfs = [load(f) for f in sorted(glob.glob(f"{base}/config/upf-*.yaml"))]
    gnbs = [load(f) for f in sorted(glob.glob(f"{base}/ran/gnb-*.yaml"))]

    amf_s = {norm(x["sst"], x["sd"]) for p in amf["amf"]["plmn_support"] for x in p["s_nssai"]}
    nssf_s = {norm(n["s_nssai"]["sst"], n["s_nssai"]["sd"]) for n in nssf["nssf"]["sbi"]["client"]["nsi"]}
    gnb_by_ip = {str(g["ngapIp"]): {norm(x["sst"], x["sd"]) for x in g["slices"]} for g in gnbs}
    upf_by_ip = {u["upf"]["pfcp"]["server"][0]["address"]: {x["dnn"] for x in u["upf"]["session"]}
                 for u in upfs}
    smf_dnns = {x.get("dnn") for x in smf["smf"]["session"]}
    route = {x["dnn"]: x["address"] for x in smf["smf"]["pfcp"]["client"]["upf"]}

    bad = checked = 0
    for f in sorted(glob.glob(f"{base}/ran/ue-*.yaml")):
        ue = load(f)
        for sess in ue["sessions"]:
            checked += 1
            sl, dnn = norm(sess["slice"]["sst"], sess["slice"]["sd"]), sess["apn"]
            errors = []
            if sl not in amf_s:
                errors.append("slice %s not supported by the AMF" % (sl,))
            if sl not in nssf_s:
                errors.append("slice %s not known to the NSSF" % (sl,))
            for ip in ue["gnbSearchList"]:
                if sl not in gnb_by_ip.get(str(ip), set()):
                    errors.append("gNB %s does not advertise slice %s" % (ip, sl))
            if dnn not in smf_dnns:
                errors.append("DNN %r has no address pool in the SMF" % dnn)
            target = route.get(dnn)
            if target is None or dnn not in upf_by_ip.get(target, set()):
                errors.append("SMF routes DNN %r to %s, which has no pool for it" % (dnn, target))
            for e in errors:
                print("::error file=%s::%s" % (f, e))
            bad += len(errors)
            if not errors:
                print("%s: slice %s, DNN %r -> gNB %s -> UPF %s: consistent"
                      % (os.path.relpath(f, REPO), sl, dnn, ue["gnbSearchList"], target))
    if checked == 0:
        print("::error::no UE session definitions found under %s/ran" % base)
        return 1
    print("%d UE session definition(s) checked, %d inconsistency(ies)" % (checked, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "deployments", "open5gs")))
