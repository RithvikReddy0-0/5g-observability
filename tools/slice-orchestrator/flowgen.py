#!/usr/bin/env python3
"""
flowgen.py — a paced UDP flow at a fixed bitrate, for the orchestrator's executed demands (ADR-016).

WHY NOT IPERF3
The orchestrator used to run each admitted demand as an iperf3 UDP test. iperf3 3.16's UDP sender
paces by busy-waiting: measured on this stack, the sending side of every flow took 45-90 % of a CPU
core whatever its rate — 90 % for a 0.01 Mbps sensor report. Twenty concurrent flows asked for more
cores than the VM has, and the CPU contention inflated URLLC's latency tail: the controller was
reacting to the load generator, not to the network. This sender sleeps between bursts instead.

It is shipped to the containers on stdin (`docker exec -i <c> python3 - ...`), so it needs no
volume mount: the eMBB data network has none.

  receiver (device side, in the UE container), bound to the device's PDU-session address:
      python3 - recv <device ip> <port> <seconds>
      prints READY once bound, then one JSON line when the flow ends
  sender (data-network side), downlink through the slice's UPF and gNB:
      python3 - send <device ip> <port> <mbps> <seconds>

Each packet carries (sequence, total), so loss is exact even when the last packets are lost.
Delivered rate is payload bits received over the flow's nominal duration.
"""

import json
import socket
import struct
import sys
import time

HDR = struct.Struct("!II")          # sequence, total packets in the flow
SIZE = 1200                          # payload bytes per packet
TICK = 0.002                         # pacing granularity: send what is due, then sleep 2 ms


def send(dst, port, mbps, seconds):
    total = max(1, int(round(mbps * 1e6 * seconds / (SIZE * 8))))
    pad = b"\0" * (SIZE - HDR.size)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    interval = seconds / total
    t0 = time.monotonic()
    sent = 0
    while sent < total:
        due = min(total, int((time.monotonic() - t0) / interval) + 1)
        while sent < due:
            try:
                s.sendto(HDR.pack(sent, total) + pad, (dst, port))
            except OSError:
                pass                 # a full queue drops the packet; the receiver counts it lost
            sent += 1
        time.sleep(TICK)
    print(json.dumps({"sent": sent, "seconds": round(time.monotonic() - t0, 3)}), flush=True)


def recv(bind, port, seconds, idle=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    s.bind((bind, port))
    s.settimeout(0.5)
    print("READY", flush=True)
    start = time.monotonic()
    deadline = start + seconds + 10          # the sender may start a little later
    got = nbytes = 0
    total, max_seq, last = None, -1, None
    while time.monotonic() < deadline:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            if last is not None and time.monotonic() - last > idle:
                break                        # the flow ended, or was stopped
            if last is None and time.monotonic() - start > idle + 5:
                break                        # nothing ever arrived
            continue
        if len(data) < HDR.size:
            continue
        seq, total = HDR.unpack_from(data)
        got += 1
        nbytes += len(data)
        max_seq = max(max_seq, seq)
        last = time.monotonic()
    expected = total if total else max_seq + 1
    print(json.dumps({
        "received": got, "expected": expected,
        "loss_percent": round(100.0 * (1 - got / expected), 3) if expected > 0 else 100.0,
        "mbps": round(nbytes * 8 / seconds / 1e6, 4),
    }), flush=True)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "send":
        send(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]))
    elif mode == "recv":
        recv(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
    else:
        sys.exit("usage: flowgen.py send|recv ...")
