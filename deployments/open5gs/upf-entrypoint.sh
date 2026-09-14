#!/bin/sh
# upf-entrypoint.sh — prepare the user plane, then run the UPF.
#
# The Open5GS UPF forwards packets in userspace through TUN devices. It needs one device per
# address pool, each holding its pool's gateway address, plus NAT so UE traffic can leave
# the container. Nothing here needs a kernel module, which is why it works on WSL2.
set -eu

setup_tun() {   # <device> <gateway/prefix> <pool>
  ip tuntap add name "$1" mode tun 2>/dev/null || true
  ip addr replace "$2" dev "$1"
  ip link set "$1" up
  iptables -t nat -C POSTROUTING -s "$3" ! -o "$1" -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -s "$3" ! -o "$1" -j MASQUERADE
  echo "upf-entrypoint: $1 up, gateway $2, NAT for $3"
}

setup_tun ogstun  10.45.0.1/16 10.45.0.0/16   # slice A — eMBB
setup_tun ogstun2 10.46.0.1/16 10.46.0.0/16   # slice B — URLLC

# Allow forwarding between the pools and eth0 (Docker's default policy may be DROP).
iptables -P FORWARD ACCEPT

# Per-slice traffic exporter on :9120. It runs HERE, not as a sidecar: the slice TUN devices
# exist only in this container's network namespace, and a sidecar that shares a namespace is
# stranded in a dead one when this container restarts — Docker then gave up restarting it.
# The respawn loop keeps it alive; it lives and dies with this container.
( while true; do python3 -u /opt/o5gs-obs/upf_slice_exporter.py; sleep 2; done ) &

exec open5gs-upfd -c /etc/open5gs/upf.yaml
