#!/bin/bash

# NAT for both network slices
iptables -t nat -C POSTROUTING -s 10.60.0.0/16 ! -o upfgtp -j MASQUERADE 2>/dev/null || \
iptables -t nat -A POSTROUTING -s 10.60.0.0/16 ! -o upfgtp -j MASQUERADE

iptables -t nat -C POSTROUTING -s 10.61.0.0/16 ! -o upfgtp -j MASQUERADE 2>/dev/null || \
iptables -t nat -A POSTROUTING -s 10.61.0.0/16 ! -o upfgtp -j MASQUERADE

# Forwarding
iptables -C FORWARD -i upfgtp -j ACCEPT 2>/dev/null || \
iptables -A FORWARD -i upfgtp -j ACCEPT

iptables -C FORWARD -o upfgtp -j ACCEPT 2>/dev/null || \
iptables -A FORWARD -o upfgtp -j ACCEPT

# Per-slice traffic accounting chains
iptables -N SLICE_A_TRAFFIC 2>/dev/null || true
iptables -N SLICE_B_TRAFFIC 2>/dev/null || true

# Slice A: UE -> external
iptables -C FORWARD -i upfgtp -s 10.60.0.0/16 -j SLICE_A_TRAFFIC 2>/dev/null || \
iptables -I FORWARD 1 -i upfgtp -s 10.60.0.0/16 -j SLICE_A_TRAFFIC

# Slice B: UE -> external
iptables -C FORWARD -i upfgtp -s 10.61.0.0/16 -j SLICE_B_TRAFFIC 2>/dev/null || \
iptables -I FORWARD 1 -i upfgtp -s 10.61.0.0/16 -j SLICE_B_TRAFFIC

# Slice A: external -> UE
iptables -C FORWARD -o upfgtp -d 10.60.0.0/16 -j SLICE_A_TRAFFIC 2>/dev/null || \
iptables -I FORWARD 1 -o upfgtp -d 10.60.0.0/16 -j SLICE_A_TRAFFIC

# Slice B: external -> UE
iptables -C FORWARD -o upfgtp -d 10.61.0.0/16 -j SLICE_B_TRAFFIC 2>/dev/null || \
iptables -I FORWARD 1 -o upfgtp -d 10.61.0.0/16 -j SLICE_B_TRAFFIC

# Accounting counters
iptables -C SLICE_A_TRAFFIC -j RETURN 2>/dev/null || \
iptables -A SLICE_A_TRAFFIC -j RETURN

iptables -C SLICE_B_TRAFFIC -j RETURN 2>/dev/null || \
iptables -A SLICE_B_TRAFFIC -j RETURN
