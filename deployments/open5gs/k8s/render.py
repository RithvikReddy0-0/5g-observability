#!/usr/bin/env python3
"""
render.py — generate the Kubernetes deployment of the Open5GS stack FROM the compose deployment.

The compose configuration (deployments/open5gs/{config,ran,observability}, docker-compose.yaml)
stays the single source of truth. This script derives the Kubernetes manifests from it, so the
two cannot drift: change a slice, a QoS value or an NF setting once, re-render, and both
deployments carry it.

Only what must differ is transformed:
  * servers bind by device instead of a fixed IP     address: 10.53.0.5   ->  dev: eth0
    (pods get dynamic addresses; Open5GS servers accept `dev`)
  * clients use Service DNS names instead of IPs     10.53.0.200          ->  scp.o5gs.svc.cluster.local
  * UERANSIM gNB addresses use the pod IP            linkIp/ngapIp/gtpIp  ->  $POD_IP at start
    (Downward API; UERANSIM's interface-name option is broken in v3.3.0, see to_k8s_gnb)
  * UE gnbSearchList uses the gNB Service name       (resolved with getaddrinfo by nr-ue)
  * Prometheus scrapes Service names instead of IPs

Every NF gets a headless Service (DNS resolves straight to the pod IP), because PFCP and NGAP
peers check the address a message comes from; a ClusterIP's NAT would hide it.

Output: deployments/open5gs/k8s/generated/o5gs.yaml (committed, reviewable)
Usage:  python3 deployments/open5gs/k8s/render.py
"""

import copy
import glob
import json
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)                         # deployments/open5gs
NS = "o5gs"
SUFFIX = "%s.svc.cluster.local" % NS
OUT = os.path.join(HERE, "generated", "o5gs.yaml")

compose = yaml.safe_load(open(os.path.join(BASE, "docker-compose.yaml"), encoding="utf-8"))
SERVICES = compose["services"]
IP_TO_NAME = {v["networks"]["o5gs"]["ipv4_address"]: k
              for k, v in SERVICES.items() if isinstance(v.get("networks"), dict)}
fqdn = lambda name: "%s.%s" % (name, SUFFIX)


def load(path):
    return yaml.safe_load(open(path, encoding="utf-8"))


def dump(doc):
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120)


# ------------------------------------------------------------------ config transforms

def to_k8s_nf(doc):
    """Open5GS NF config: servers bind to eth0, every IP reference becomes a Service name."""
    def walk(node, parent_key=None):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "server" and isinstance(v, list):
                    out[k] = []
                    for entry in v:
                        e = {kk: vv for kk, vv in entry.items() if kk != "address"}
                        e = {"dev": "eth0", **e} if "address" in entry else e
                        out[k].append(walk(e, k))
                else:
                    out[k] = walk(v, k)
            return out
        if isinstance(node, list):
            return [walk(x, parent_key) for x in node]
        if isinstance(node, str):
            return re.sub(r"\b10\.53\.0\.(\d+)\b",
                          lambda m: fqdn(IP_TO_NAME["10.53.0.%s" % m.group(1)]), node)
        return node
    return walk(doc)


def to_k8s_gnb(doc):
    # Not "eth0": UERANSIM v3.3.0 documents an interface name as valid, but GetIpAddress()
    # (src/utils/yaml_utils.cpp) first runs TryResolveHost(), which returns "" for anything that
    # is not a resolvable host and OVERWRITES the name, so the interface lookup that follows
    # receives an empty string. Found when every gNB pod crash-looped with "must be a valid IP
    # address, or FQDN, or a valid network interface". The pod IP comes from the Downward API
    # instead and is substituted at container start (see the gNB Deployment below).
    d = copy.deepcopy(doc)
    for k in ("linkIp", "ngapIp", "gtpIp"):
        d[k] = "POD_IP"
    for amf in d["amfConfigs"]:
        amf["address"] = fqdn(IP_TO_NAME[str(amf["address"])])
    return d


def to_k8s_ue(doc):
    d = copy.deepcopy(doc)
    d["gnbSearchList"] = [fqdn(IP_TO_NAME[str(ip)]) for ip in d["gnbSearchList"]]
    return d


def to_k8s_prometheus(doc):
    d = copy.deepcopy(doc)
    for job in d["scrape_configs"]:
        if job["job_name"] == "slice-orchestrator":
            continue
        for sc in job.get("static_configs", []):
            sc["targets"] = [re.sub(r"^10\.53\.0\.(\d+)", lambda m: fqdn(IP_TO_NAME["10.53.0.%s" % m.group(1)]), t)
                             for t in sc["targets"]]
    # The orchestrator is a host process in the compose deployment; it is not part of this one.
    d["scrape_configs"] = [j for j in d["scrape_configs"] if j["job_name"] != "slice-orchestrator"]
    return d


# ------------------------------------------------------------------ manifests

docs = []
add = docs.append

add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}})


def configmap(name, files):
    add({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NS},
         "data": files})


nf_configs = {os.path.basename(p): dump(to_k8s_nf(load(p)))
              for p in sorted(glob.glob(os.path.join(BASE, "config", "*.yaml")))}
configmap("o5gs-config", nf_configs)

ran = {}
for p in sorted(glob.glob(os.path.join(BASE, "ran", "gnb-*.yaml"))):
    ran[os.path.basename(p)] = dump(to_k8s_gnb(load(p)))
for p in sorted(glob.glob(os.path.join(BASE, "ran", "ue-slice-*.yaml"))):
    ran[os.path.basename(p)] = dump(to_k8s_ue(load(p)))
ran["ue-entrypoint.sh"] = open(os.path.join(BASE, "ran", "ue-entrypoint.sh"), encoding="utf-8").read()
configmap("o5gs-ran", ran)

obs = {os.path.basename(p): open(p, encoding="utf-8").read()
       for p in sorted(glob.glob(os.path.join(BASE, "observability", "*.py")))}
obs["upf-entrypoint.sh"] = open(os.path.join(BASE, "upf-entrypoint.sh"), encoding="utf-8").read()
configmap("o5gs-obs", obs)
configmap("o5gs-prometheus", {"prometheus.yml": dump(to_k8s_prometheus(load(os.path.join(BASE, "observability", "prometheus.yml"))))})


def headless(name, ports):
    add({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS},
         "spec": {"clusterIP": "None", "selector": {"app": name},
                  "ports": [{"name": "%s-%d" % (proto.lower(), port), "port": port, "protocol": proto}
                            for proto, port in ports]}})


def deployment(name, container, volumes=(), wait_for=()):
    """wait_for: Service names that must resolve before the main container starts. Open5GS and
    UERANSIM resolve their peers once at startup, and a headless Service has no DNS record until
    its pod is running — without this, pods crash-loop until their dependencies happen to be up."""
    pod = {"containers": [container], "volumes": list(volumes)}
    if wait_for:
        names = " ".join(fqdn(n) for n in wait_for)
        pod["initContainers"] = [{
            "name": "wait-for-peers", "image": container["image"], "imagePullPolicy": container.get("imagePullPolicy", "IfNotPresent"),
            "command": ["/bin/sh", "-c",
                        "for n in %s; do until getent hosts $n >/dev/null; do echo waiting for $n; sleep 2; done; done" % names]}]
    add({"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": name, "namespace": NS, "labels": {"app": name}},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": {"app": name}},
                  "template": {"metadata": {"labels": {"app": name}}, "spec": pod}}})


def cm_volume(vol, cm, items=None, mode=None):
    v = {"name": vol, "configMap": {"name": cm}}
    if items:
        v["configMap"]["items"] = [{"key": k, "path": k} for k in items]
    if mode:
        v["configMap"]["defaultMode"] = mode
    return v


OPEN5GS = SERVICES["amf"]["image"] if "image" in SERVICES["amf"] else "o5gs/open5gs:v2.8.0"
UERANSIM = SERVICES["ue"]["image"]
MONGO = SERVICES["mongodb"]["image"]
PROM = SERVICES["prometheus"]["image"]
CONFIG_MOUNT = {"name": "config", "mountPath": "/etc/open5gs"}
SBI = [("TCP", 7777)]
METRICS = [("TCP", 9090)]

# ---- database
add({"apiVersion": "v1", "kind": "PersistentVolumeClaim",
     "metadata": {"name": "mongodb-data", "namespace": NS},
     "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}})
headless("mongodb", [("TCP", 27017)])
deployment("mongodb", {
    "name": "mongodb", "image": MONGO, "imagePullPolicy": "IfNotPresent",
    "args": ["mongod", "--bind_ip", "0.0.0.0"],
    "volumeMounts": [{"name": "data", "mountPath": "/data/db"}],
    "readinessProbe": {"exec": {"command": ["mongo", "--quiet", "--eval", "db.runCommand({ping:1}).ok"]},
                       "periodSeconds": 5}},
    volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "mongodb-data"}}])

# ---- control-plane NFs: one image, one binary each
NFS = {"nrf": SBI, "scp": SBI, "ausf": SBI, "udm": SBI, "udr": SBI, "pcf": SBI + METRICS,
       "bsf": SBI, "nssf": SBI, "amf": SBI + METRICS + [("SCTP", 38412)],
       "smf": SBI + METRICS + [("UDP", 8805), ("UDP", 2152)]}
WAIT = {"nrf": ["mongodb"], "scp": ["nrf"], "ausf": ["scp"], "udm": ["scp"], "udr": ["scp", "mongodb"],
        "pcf": ["scp", "mongodb"], "bsf": ["scp"], "nssf": ["scp"], "amf": ["scp"],
        "smf": ["scp", "upf-embb", "upf-urllc"]}
for nf, ports in NFS.items():
    headless(nf, ports)
    deployment(nf, {
        "name": nf, "image": OPEN5GS, "imagePullPolicy": "Never",
        "command": ["open5gs-%sd" % nf, "-c", "/etc/open5gs/%s.yaml" % nf],
        "volumeMounts": [CONFIG_MOUNT]},
        volumes=[cm_volume("config", "o5gs-config")], wait_for=WAIT[nf])

# ---- one UPF per slice (ADR-011), userspace over TUN
for upf, svc in (("upf-embb", SERVICES["upf-embb"]), ("upf-urllc", SERVICES["upf-urllc"])):
    env = {k: str(v) for k, v in svc["environment"].items() if k != "DN_SUBNET"}
    headless(upf, METRICS + [("UDP", 8805), ("UDP", 2152), ("TCP", 9120)])
    deployment(upf, {
        "name": upf, "image": OPEN5GS, "imagePullPolicy": "Never",
        "command": ["/bin/sh", "/opt/o5gs-entry/upf-entrypoint.sh"],
        "env": [{"name": k, "value": v} for k, v in env.items()],
        # TUN devices, iptables NAT and ip_forward need a privileged pod on this cluster.
        "securityContext": {"privileged": True},
        "volumeMounts": [CONFIG_MOUNT, {"name": "obs", "mountPath": "/opt/o5gs-obs"},
                         {"name": "entry", "mountPath": "/opt/o5gs-entry"}]},
        volumes=[cm_volume("config", "o5gs-config"), cm_volume("obs", "o5gs-obs"),
                 cm_volume("entry", "o5gs-obs", items=["upf-entrypoint.sh"])])

# ---- per-slice data networks (ADR-012); on Kubernetes UE traffic reaches them through NAT
for dn in ("dn-embb", "dn-urllc"):
    headless(dn, [("TCP", 5201)])
    deployment(dn, {"name": dn, "image": OPEN5GS, "imagePullPolicy": "Never",
                    "command": ["sleep", "infinity"]})

# ---- one gNB per slice (ADR-011)
for gnb in ("gnb-embb", "gnb-urllc"):
    headless(gnb, [("UDP", 4997), ("UDP", 2152)])
    deployment(gnb, {
        "name": gnb, "image": UERANSIM, "imagePullPolicy": "Never",
        "command": ["/bin/sh", "-c",
                    "sed \"s/POD_IP/$POD_IP/g\" /etc/ueransim/%s.yaml > /tmp/gnb.yaml && exec nr-gnb -c /tmp/gnb.yaml" % gnb],
        "env": [{"name": "UERANSIM_UDP_BUFFER_BYTES", "value": "8388608"},
                {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}],
        "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
        "volumeMounts": [{"name": "ran", "mountPath": "/etc/ueransim"}]},
        volumes=[cm_volume("ran", "o5gs-ran")], wait_for=["amf"])

# ---- the 20 supervised UEs
ue_env = {k: str(v) for k, v in SERVICES["ue"]["environment"].items()}
headless("ue", [("TCP", 9121)])
deployment("ue", {
    "name": "ue", "image": UERANSIM, "imagePullPolicy": "Never",
    "command": ["/bin/sh", "/etc/ueransim/ue-entrypoint.sh"],
    "env": [{"name": k, "value": v} for k, v in ue_env.items()],
    "securityContext": {"privileged": True},
    "volumeMounts": [{"name": "ran", "mountPath": "/etc/ueransim"},
                     {"name": "obs", "mountPath": "/opt/o5gs-obs"}]},
    volumes=[cm_volume("ran", "o5gs-ran"), cm_volume("obs", "o5gs-obs")], wait_for=["gnb-embb", "gnb-urllc"])

# ---- Prometheus (same scrape jobs and labels as compose, so the same KPI gate applies)
add({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "prometheus", "namespace": NS},
     "spec": {"selector": {"app": "prometheus"}, "ports": [{"name": "http", "port": 9090}]}})
deployment("prometheus", {
    "name": "prometheus", "image": PROM, "imagePullPolicy": "IfNotPresent",
    "args": ["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.retention.time=2d"],
    "volumeMounts": [{"name": "cfg", "mountPath": "/etc/prometheus"}]},
    volumes=[cm_volume("cfg", "o5gs-prometheus")])

os.makedirs(os.path.dirname(OUT), exist_ok=True)
header = ("# GENERATED by deployments/open5gs/k8s/render.py from the compose deployment — do not edit.\n"
          "# Re-render after changing deployments/open5gs/{config,ran,observability} or docker-compose.yaml.\n")
with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write(header + "---\n".join(yaml.safe_dump(d, sort_keys=False, width=120) for d in docs))
kinds = {}
for d in docs:
    kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
print("rendered %s: %s" % (os.path.relpath(OUT, os.path.dirname(BASE)), json.dumps(kinds)))
