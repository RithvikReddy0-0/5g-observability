# Multi-session UEs — evidence (ADR-020)

Produced by `scripts/open5gs/multi_session_demo.sh` on the live 100-UE stack, with the packet
scheduler running. Run [`20261108-092615Z`](20261108-092615Z/):

| File | What it shows |
|---|---|
| [`1-sessions.txt`](20261108-092615Z/1-sessions.txt) | every live session per pool and kind: eMBB 20 home + 10 second, URLLC 10 home + 20 second, mMTC 70 home; the SMF counts 30 / 30 / 70 |
| [`2-placement.txt`](20261108-092615Z/2-placement.txt) | 6 of 6 demands placed on the other slice ran on the requester's own session; an IoT device asking for URLLC-grade service was refused |
| [`3-lifecycle.txt`](20261108-092615Z/3-lifecycle.txt) | URLLC off in 10.8 s: the 20 eMBB phones released only their URLLC session and stayed registered; URLLC on in 34.5 s: all 20 sessions back, sampled data path checked, the core back to 30 / 30 / 70 |

Every one of the 130 sessions was also checked separately with a ping to its own UPF: 130 of 130.

## What went wrong on the way, and stays recorded

1. **Interface names do not say which session.** The first version took `uesimtun<NNNN>0` to be the
   home session. UERANSIM numbers interfaces in setup order, so a URLLC device's eMBB session sat on
   `…0`; the UE supervisor pinged the wrong gateway and restarted healthy UEs, and 18/12 "home"
   sessions were counted instead of 20/10. Every tool now reads the PSI from the UE's own log
   (`ue-slice.sh sessions`): PSI 1 is the home session.
2. **A configured session cannot be released.** In the first lifecycle run
   ([`superseded-configured-second-session/`](superseded-configured-second-session/)) the eMBB
   phones' URLLC session came back 0.8 s after `ps-release`: UERANSIM re-opens every session in its
   config as soon as it is gone (`establishRequiredSessions`). The URLLC UPF then stopped under 20
   fresh sessions, which the core kept. The second session is now opened on demand
   (`extraSessions:` + `nr-cli ps-establish`), and a released slice's DNN stays released until it is
   activated again. In the run above the core drops to 0 URLLC sessions.
3. **A bare `wait` waited for the UEs.** The first on-demand version waited for every child of the
   shell, including the `nr-ue` processes it had just launched: an activation timed out after 180 s
   and was rolled back cleanly, and the UE supervisor never started. Both now wait for their own
   jobs only.
4. **30 session requests at once cost 4 home sessions at start.** The supervisor restarted them. The
   second sessions are now opened 10 at a time: 0 restarts.
