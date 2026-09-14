#!/bin/sh
# upf-entrypoint.sh — prepare ONE slice's user plane, then run its UPF.
#
# There is one UPF container per slice (ADR-011). Each gets, from docker-compose.yaml:
#   UPF_CONFIG     its config file            e.g. /etc/open5gs/upf-embb.yaml
#   TUN_DEV        its TUN device             e.g. ogstun
#   TUN_GATEWAY    the pool gateway/prefix    e.g. 10.45.0.1/16
#   TUN_POOL       the UE address pool        e.g. 10.45.0.0/16
#   SLICE_DEVICES  what the traffic exporter reports, "dev:sst:sd:name"
#
# The Open5GS UPF forwards packets in userspace through the TUN device, which must exist and
# hold the pool gateway before the UPF starts; NAT lets UE traffic leave the container. None
# of this needs a kernel module, which is why it works on WSL2.
set -eu

: "${UPF_CONFIG:?}" "${TUN_DEV:?}" "${TUN_GATEWAY:?}" "${TUN_POOL:?}"

ip tuntap add name "$TUN_DEV" mode tun 2>/dev/null || true
ip addr replace "$TUN_GATEWAY" dev "$TUN_DEV"
ip link set "$TUN_DEV" up
# The kernel queues packets for the UPF on the TUN device; the default 500 overflowed under
# UDP bursts and dropped 8.5 % of an 80 Mbps downlink (tx_dropped on the TUN, every lost
# packet accounted for). 10000 took the same run to 0.00 % loss (ADR-012).
ip link set "$TUN_DEV" txqueuelen "${TUN_TXQUEUELEN:-10000}"
# Traffic to the lab data networks (o5gs-dn-*) is routed, not NATed, so a DN sees each UE's
# real address; everything else (e.g. the internet) is masqueraded.
if [ -n "${DN_SUBNET:-}" ]; then
  iptables -t nat -C POSTROUTING -s "$TUN_POOL" -d "$DN_SUBNET" -j RETURN 2>/dev/null \
    || iptables -t nat -I POSTROUTING 1 -s "$TUN_POOL" -d "$DN_SUBNET" -j RETURN
fi
iptables -t nat -C POSTROUTING -s "$TUN_POOL" ! -o "$TUN_DEV" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "$TUN_POOL" ! -o "$TUN_DEV" -j MASQUERADE
iptables -P FORWARD ACCEPT
echo "upf-entrypoint: $TUN_DEV up, gateway $TUN_GATEWAY, NAT for $TUN_POOL, config $UPF_CONFIG"

# Per-slice traffic exporter on :9120. It runs HERE, not as a sidecar: the TUN device exists
# only in this container's network namespace, and a sidecar that shares a namespace is
# stranded in a dead one when this container restarts — Docker then gave up restarting it.
( while true; do python3 -u /opt/o5gs-obs/upf_slice_exporter.py; sleep 2; done ) &

exec open5gs-upfd -c "$UPF_CONFIG"
