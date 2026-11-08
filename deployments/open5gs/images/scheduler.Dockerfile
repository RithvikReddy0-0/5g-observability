# scheduler.Dockerfile — the packet scheduler's runtime (ADR-019): Python plus tc and nsenter.
#
# The scheduler itself (tools/packet-scheduler/scheduler.py) is mounted read-only from the
# repository, so a change to it needs no rebuild. The image only supplies the tools: neither the
# Open5GS nor the UERANSIM image carries tc, and the host's tc needs root, which a user shell on
# this host does not have without a password.
#
# Base pinned by digest, as in the other images.
#   docker build -f deployments/open5gs/images/scheduler.Dockerfile -t o5gs/scheduler:1 .

FROM python@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a

# iproute2: tc with JSON output (-j) and the u32/htb/fq_codel support the scheduler installs.
# util-linux-misc: nsenter, to reach the UE container's network namespace for the uplink queue.
RUN apk add --no-cache iproute2 util-linux-misc \
 && tc -V && nsenter --version

ENTRYPOINT ["python3", "-u", "/repo/tools/packet-scheduler/scheduler.py"]
