# Wall-clock steps faked radio link failures (Open5GS, 2026-09-25)

Found while starting Track 1 (mMTC slice, 100 UEs). The running 20-UE stack would not recover
after an engine restart: 6 of 20 UEs reachable after 2 minutes, while the supervisor kept
restarting UEs.

## What was measured

**The simulator was not starved.** VM load average 0.47 on 12 threads; no container above 10 % CPU.

**Radio link failures came in bursts about 31 s apart,** 1 to 6 UEs each, across both slices and
both gNBs:

```
6  04:21:16     3  04:21:48     3  04:22:19     1  04:22:51     1  04:23:22
```

**The VM's wall clock was being stepped.** A loop ticking every 100 ms compared monotonic and
wall time. Monotonic time never stalled (worst gap 0.207 s). The wall clock jumped forward
~1.1 s every ~31 s, and the failures fired at exactly those seconds:

```
04:24:25  monotonic gap 0.10s  wall gap 1.19s      radio link failures at 04:24:25: 2
04:24:56  monotonic gap 0.10s  wall gap 1.20s      radio link failures at 04:24:56: 3
```

The VM's TSC clocksource ran about 3.5 % slow on this boot. `systemd-timesyncd` (active, 32 s
minimum poll) stepped the clock back into line on each poll.

## Why that broke UEs

UERANSIM v3.3.0 times its radio-link heartbeats with `utils::CurrentTimeMillis()`, which reads
`std::chrono::system_clock` — the wall clock. A heartbeat is lost after 2 s
(`HEARTBEAT_THRESHOLD`, `src/ue/rls/udp_task.cpp`). Heartbeats arrive every ~1 s, so a UE whose
last one was more than ~0.9 s old when the clock stepped +1.1 s saw a 2 s silence. It declared
radio link failure, recovered its registration with a Service Request, and lost its user plane
(`Uplink data failure, PDU session not found` at the gNB).

All 38 callers of `CurrentTimeMillis()` measure intervals: heartbeats, NAS timers, the NTS timer
queue, and the gNB's AMBR token bucket. So the same step also fired timers early and refilled
the rate limiter with 1.1 s of extra tokens.

## Fix

[`ueransim-monotonic-clock.patch`](../../../deployments/open5gs/images/patches/ueransim-monotonic-clock.patch):
`CurrentTimeMillis()` now reads `std::chrono::steady_clock`, which cannot step. The only caller
that needs real time is `CurrentTimeStamp()`, the NTP-format timestamp in NGAP messages, and it
now reads the wall clock itself. Image tag `o5gs/ueransim:v3.3.0-udpbuf-mono`.

## After, same host, clock still stepping

Both gNBs and the UE container were recreated with the patched image. Nothing else changed.

```
UE interfaces: 20 after ~12s
wall-clock steps during 130 s: 6  04:35:26 (+1.11s) 04:35:58 (+1.10s) 04:36:28 (+0.56s)
                                  04:36:29 (+1.07s) 04:37:00 (+0.52s) 04:37:01 (+1.10s)
radio link failures since the patched UEs started: 0
supervisor restarts since start: 0
gNB 'PDU session not found': 0
reachable now: eMBB 10, URLLC 10
```

The host's clock was not changed. Fixing a VM's time source is a system setting; making the
simulator immune to clock steps fixes this laptop, and any other one — NTP corrections, resume
from sleep and VM time sync all step the wall clock.
