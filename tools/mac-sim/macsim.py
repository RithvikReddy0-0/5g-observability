#!/usr/bin/env python3
"""
macsim.py -- SIMULATION ONLY: a slot-based downlink MAC scheduler for one 5G NR cell that carries
the project's three slices (ADR-022). Nothing here touches the testbed.

The testbed's radio is UERANSIM, which has no MAC layer, no PRBs and no channel; its packet
scheduler (ADR-019) works on IP packets. This simulator answers the guide's question "what would a
real MAC scheduler do?" with the same slices and shares, on a modelled radio:

  carrier     numerology 1 (30 kHz SCS, 0.5 ms slots), 40 MHz = 106 PRBs (TS 38.101-1 Table 5.3.2-1),
              resource allocation type 0 with RBGs of 8 PRBs (TS 38.214 Table 5.1.2.2.1-1, BWP
              73-144 PRBs, configuration 1): 13 RBGs of 8 PRBs and 1 of 2.
  channel     per UE: distance-based mean SNR + log-normal shadowing, and Rayleigh fading from a
              4-tap channel whose taps evolve as a Gauss-Markov process with the Jakes correlation
              J0(2 pi fd T); frequency-selective across RBGs. CQI per RBG from TS 38.214 Table
              5.2.2.1-2 (4-bit CQI table 1) with an assumed 3 dB SNR gap to Shannon. CQI is reported
              every 5 slots and the channel is held constant in between (no CSI error), so every
              transmission at the reported CQI is assumed to succeed: HARQ is NOT modelled.
  traffic     eMBB 20 UEs (Poisson 1500-byte packets, or full buffer), URLLC 10 UEs (3 critical +
              7 standard, 64 B every 20 ms, 10 ms budget), mMTC 70 UEs (10 B every 10 s), as in the
              testbed's traffic profiles (ADR-015).
  intra-slice Round Robin, Max-C/I, Proportional Fair, M-LWDF, EXP/PF.
  inter-slice static split, NVS, priority (HTB-like, the ADR-019 analogue), quota-seq (RadioSaber's
              quotas without channel awareness: an ablation), RadioSaber, and none (one scheduler
              over all UEs: no slicing).

Usage:
  python3 tools/mac-sim/macsim.py                         # full study -> docs/evidence/mac-sim/
  python3 tools/mac-sim/macsim.py --seeds 3 --duration 2  # quicker
  python3 tools/mac-sim/macsim.py --one overload radiosaber default 1   # one run, metrics as JSON

Deterministic: every random stream is seeded from (scenario, seed, UE), so all schedulers see the same
UE positions, the same channel realisation and the same packets for a given seed.
"""

import argparse
import bisect
import cmath
import csv
import json
import math
import os
import random
import sys
import time
from collections import deque

# ------------------------------------------------------------------------------------ carrier
SCS_KHZ = 30                    # numerology mu = 1
SLOT_MS = 0.5                   # 14 OFDM symbols at 30 kHz
N_PRB = 106                     # 40 MHz at 30 kHz SCS (TS 38.101-1 Table 5.3.2-1)
RBG_SIZE = 8                    # TS 38.214 Table 5.1.2.2.1-1, BWP of 73-144 PRBs, configuration 1
RBG_PRBS = [RBG_SIZE] * (N_PRB // RBG_SIZE) + ([N_PRB % RBG_SIZE] if N_PRB % RBG_SIZE else [])
N_RBG = len(RBG_PRBS)           # 14
RE_PER_PRB = 12 * 12 - 12       # 12 subcarriers x 12 PDSCH symbols (2 of 14 for PDCCH) - 12 DMRS REs
BANDWIDTH_HZ = N_PRB * 12 * SCS_KHZ * 1000.0
CSI_PERIOD = 5                  # slots between CQI reports (2.5 ms)
CARRIER_HZ = 3.5e9              # n78
UE_SPEED_KMH = 3.0

# TS 38.214 Table 5.2.2.1-2, "4-bit CQI Table" (CQI table 1): index, modulation, code rate x 1024,
# spectral efficiency (bit per resource element).
CQI_TABLE = [
    (0, "out of range", 0, 0.0),
    (1, "QPSK", 78, 0.1523), (2, "QPSK", 120, 0.2344), (3, "QPSK", 193, 0.3770),
    (4, "QPSK", 308, 0.6016), (5, "QPSK", 449, 0.8770), (6, "QPSK", 602, 1.1758),
    (7, "16QAM", 378, 1.4766), (8, "16QAM", 490, 1.9141), (9, "16QAM", 616, 2.4063),
    (10, "64QAM", 466, 2.7305), (11, "64QAM", 567, 3.3223), (12, "64QAM", 666, 3.9023),
    (13, "64QAM", 772, 4.5234), (14, "64QAM", 873, 5.1152), (15, "64QAM", 948, 5.5547),
]
SE = [row[3] for row in CQI_TABLE]
SNR_GAP_DB = 3.0                # ASSUMPTION: the CQI whose efficiency fits log2(1 + SNR / gap)
CQI_THRESH = [(2.0 ** se - 1.0) * 10 ** (SNR_GAP_DB / 10.0) for se in SE[1:]]
# bits one RBG carries in one slot at each CQI (single layer, no MIMO)
BITS = [[int(se * RE_PER_PRB * p) for p in RBG_PRBS] for se in SE]
INF = float("inf")


def cqi_from_snr(snr_lin):
    """Highest CQI index of Table 5.2.2.1-2 whose SNR threshold is met (0 = out of range)."""
    return bisect.bisect_right(CQI_THRESH, snr_lin)


def bessel_j0(x):
    s, term, k = 0.0, 1.0, 0
    while abs(term) > 1e-15 and k < 60:
        s += term
        k += 1
        term *= -(x * x / 4.0) / (k * k)
    return s


# ------------------------------------------------------------------------------------ channel
class Channel:
    """Frequency-selective Rayleigh fading around a mean SNR. L equal-power taps, each a complex
    Gauss-Markov process with one-step correlation rho = J0(2 pi fd T) per CQI report period; the
    response of RBG b is sum_l h_l exp(-j 2 pi f_b tau_l). Its own random stream, advanced once per
    report period whatever the scheduler does, so every scheduler sees the same realisation."""

    __slots__ = ("rng", "snr", "h", "rho", "sig", "phase", "steps", "cqi", "tap_sd")

    def __init__(self, seed, mean_snr_db, phase, rho):
        self.rng = random.Random(seed)
        self.snr = 10 ** (mean_snr_db / 10.0)
        self.phase = phase
        self.rho = rho
        self.sig = math.sqrt(max(0.0, 1.0 - rho * rho))
        self.tap_sd = math.sqrt(1.0 / (2.0 * len(phase[0])))
        self.h = [self._cn() for _ in phase[0]]
        self.steps = 0
        self.cqi = self._cqi()

    def _cn(self):
        g = self.rng.gauss
        return complex(g(0.0, self.tap_sd), g(0.0, self.tap_sd))

    def _cqi(self):
        out = []
        for ph in self.phase:
            hb = 0j
            for hl, p in zip(self.h, ph):
                hb += hl * p
            out.append(cqi_from_snr(self.snr * (hb.real * hb.real + hb.imag * hb.imag)))
        return out

    def at(self, period):
        if period > self.steps:
            rho, sig = self.rho, self.sig
            while self.steps < period:
                self.h = [rho * x + sig * self._cn() for x in self.h]
                self.steps += 1
            self.cqi = self._cqi()
        return self.cqi


class FixedChannel:
    """A channel that never changes (unit tests): a CQI per RBG, or a function of the period."""

    def __init__(self, cqi):
        self.f = cqi if callable(cqi) else None
        self.cqi = None if callable(cqi) else list(cqi)

    def at(self, period):
        return self.f(period) if self.f else self.cqi


# ------------------------------------------------------------------------------------ UEs
class UE:
    __slots__ = ("id", "slice", "tier", "ch", "q", "qbits", "full", "avg", "slot_bits", "budget",
                 "cq", "a", "rt", "budget_ms", "delivered", "rbgs", "idx", "discard")

    def __init__(self, uid, slice_name, ch, full=False, tier=0, budget_ms=None, delta=0.01, discard=False):
        self.id = uid
        self.slice = slice_name
        self.tier = tier                # 1 = critical (ARP 1) URLLC device
        self.ch = ch
        self.q = deque()                # [arrival ms, remaining bits]
        self.qbits = 0
        self.full = full                # full buffer: always backlogged
        self.avg = 1.0                  # PF average throughput, bits per slot
        self.slot_bits = 0
        self.budget = 0
        self.cq = None
        self.rt = budget_ms is not None
        self.budget_ms = budget_ms
        self.a = (-math.log(delta) / budget_ms) if self.rt else 0.0   # M-LWDF a_i = -log(delta_i) / T_i
        self.delivered = 0
        self.rbgs = 0
        self.idx = 0
        self.discard = discard          # drop a packet once it is older than its budget (PDCP discardTimer)

    def backlogged(self):
        return self.full or self.qbits > 0


# ------------------------------------------------------------------------------------ intra-slice
INTRA_KINDS = ("rr", "maxci", "pf", "mlwdf", "exppf")


class Intra:
    """A greedy intra-slice ("enterprise") scheduler. prepare() at the start of a slot; pick(b)
    answers "which of my users would get RBG b?" for the current state; commit() after a grant.
    Metrics are computed once per slot (PF averages and head-of-line delays are per-slot values,
    as in an LTE/NR MAC); within the slot a user drops out once its backlog is covered."""

    def __init__(self, users, kind, tiered=False):
        if kind not in INTRA_KINDS:
            raise ValueError("unknown intra-slice scheduler %r" % kind)
        self.users = users
        for i, u in enumerate(users):
            u.idx = i
        self.kind = kind
        self.tiered = tiered            # priority policy: critical URLLC devices first
        self.ptr = 0                    # round robin pointer (persists across slots)
        self.cands = []
        self.lists = None
        self.cur = None

    def prepare(self, slot, now_ms):
        cands = [u for u in self.users if u.budget > 0]
        self.cands = cands
        if not cands or self.kind == "rr":
            self.lists = None
            return
        n = len(self.users)
        kind = self.kind
        hol = {}
        aw_mean = 0.0
        if kind in ("mlwdf", "exppf"):
            rts = []
            for u in cands:
                if u.rt and u.q:
                    w = now_ms - u.q[0][0] + SLOT_MS       # head-of-line delay at the end of this slot
                    hol[u.id] = w
                    rts.append(u.a * w)
            if rts:
                aw_mean = sum(rts) / len(rts)
        lists = []
        for b in range(N_RBG):
            row = []
            for u in cands:
                c = u.cq[b]
                if c == 0:
                    continue
                r = BITS[c][b]
                if kind == "maxci":
                    m = r
                elif kind == "pf":
                    m = r / u.avg
                elif kind == "mlwdf":
                    m = (u.a * hol[u.id] * r / u.avg) if u.id in hol else r / u.avg
                else:   # exppf: exp((a W - mean aW) / (1 + sqrt(mean aW))) r / R for real-time flows
                    if u.id in hol:
                        e = (u.a * hol[u.id] - aw_mean) / (1.0 + math.sqrt(aw_mean))
                        m = math.exp(min(e, 700.0)) * r / u.avg
                    else:
                        m = r / u.avg
                rot = (u.idx - slot) % n                    # ties broken by a rotating order
                row.append(((u.tier if self.tiered else 0), m, -rot, u))
            row.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
            lists.append([t[3] for t in row])
        self.lists = lists
        self.cur = [0] * N_RBG

    def active(self):
        for u in self.cands:
            if u.budget > 0:
                return True
        return False

    def pick(self, b):
        if self.kind == "rr":
            cands = self.cands
            if not cands:
                return None
            n = len(cands)
            start = 0
            while start < n and cands[start].idx < self.ptr:
                start += 1
            best = None
            for k in range(n):
                u = cands[(start + k) % n]
                if u.budget > 0 and u.cq[b] > 0:
                    if not self.tiered or u.tier == 1:
                        return u
                    if best is None:
                        best = u
            return best
        if self.lists is None:
            return None
        lst = self.lists[b]
        i = self.cur[b]
        while i < len(lst) and lst[i].budget <= 0:
            i += 1
        self.cur[b] = i
        return lst[i] if i < len(lst) else None

    def commit(self, u, b):
        if self.kind == "rr":
            self.ptr = (u.idx + 1) % len(self.users)


# ------------------------------------------------------------------------------------ slices
class Slice:
    def __init__(self, name, users, intra, weight, share, prio):
        self.name = name
        self.users = users
        self.intra = intra
        self.weight = weight            # inter-slice weight (normalised over the slices)
        self.share = share              # guaranteed share of the cell (priority policy, ADR-019)
        self.prio = prio                # lower is served first (priority policy)
        self.off = 0.0                  # RadioSaber's rolling offset, in RBGs
        self.ewma = 0.0                 # NVS: moving average of slots given to this slice
        self.rbgs = 0


INTER_KINDS = ("static", "nvs", "priority", "quota-seq", "radiosaber", "none")
NVS_BETA = 0.01                 # as in the RadioSaber authors' NVS baseline (downlink-nvs-scheduler.h)


class Cell:
    """One cell, one slot at a time. `inter` divides the RBGs of a slot between the slices; each
    slice's Intra divides its RBGs between its users."""

    def __init__(self, slices, inter, seed=0, pf_tc_slots=100, none_kind="mlwdf"):
        if inter not in INTER_KINDS:
            raise ValueError("unknown inter-slice scheduler %r" % inter)
        self.slices = slices
        self.inter = inter
        self.rng = random.Random(seed * 7919 + 17)
        self.alpha = 1.0 / pf_tc_slots
        self.users = [u for s in slices for u in s.users]
        wsum = sum(s.weight for s in slices)
        for s in slices:
            s.weight = s.weight / wsum
        # no slicing: one scheduler over every UE (the slices' own schedulers are then unused)
        self.none = Intra(self.users, none_kind) if inter == "none" else None
        self.assigned = [None] * N_RBG
        self.slot = 0
        self.prb_used = 0
        self.cap_bits = 0

    # -- quotas (RadioSaber, NSDI'23 Algorithm 1, in RBG units) --------------------------------
    def quotas(self, active, redistribute, random_extra=True):
        """target = w * |RBG| + carried offset; quota = floor(target); the RBGs lost to rounding go
        +1 to a random set of slices; offset = target - quota rolls over to the next slot. With
        `redistribute` the share of slices with no data is split equally among those with data."""
        S = len(self.slices)
        consider = [active[i] or not redistribute for i in range(S)]
        target = [0.0] * S
        for i, s in enumerate(self.slices):
            if consider[i]:
                target[i] = s.weight * N_RBG + s.off
        if redistribute:
            n_act = sum(consider)
            if n_act == 0:
                return [0] * S
            idle = sum(s.weight for i, s in enumerate(self.slices) if not consider[i]) * N_RBG
            for i in range(S):
                if consider[i]:
                    target[i] += idle / n_act
        q = [max(0, int(math.floor(t))) if consider[i] else 0 for i, t in enumerate(target)]
        extra = N_RBG - sum(q)
        if random_extra and extra > 0:
            order = [i for i in range(S) if consider[i]]
            self.rng.shuffle(order)
            k = 0
            while extra > 0 and order:
                q[order[k % len(order)]] += 1
                extra -= 1
                k += 1
        for i, s in enumerate(self.slices):
            s.off = (target[i] - q[i]) if consider[i] else 0.0
        return q

    # -- one grant ------------------------------------------------------------------------------
    def grant(self, b, u, intra):
        bits = BITS[u.cq[b]][b]
        self.assigned[b] = u
        u.budget -= bits
        u.slot_bits += bits
        u.rbgs += 1
        intra.commit(u, b)
        self.prb_used += RBG_PRBS[b]
        self.cap_bits += bits

    # -- the inter-slice policies ----------------------------------------------------------------
    def _static(self):
        q = self.quotas([True] * len(self.slices), redistribute=False)
        b = 0
        for s, qs in zip(self.slices, q):           # fixed, contiguous RBG ranges; unused RBGs idle
            for _ in range(qs):
                if b >= N_RBG:
                    break
                u = s.intra.pick(b)
                if u is not None:
                    self.grant(b, u, s.intra)
                    s.rbgs += 1
                b += 1

    def _nvs(self):
        act = [s for s in self.slices if s.intra.active()]
        if not act:
            return
        chosen, best = None, -1.0
        for s in act:                                # NVS: the whole slot to max weight / ewma
            if s.ewma == 0:
                chosen = s
                break
            score = s.weight / s.ewma
            if score >= best:
                best, chosen = score, s
        for s in act:
            s.ewma = (1 - NVS_BETA) * s.ewma + (NVS_BETA if s is chosen else 0.0)
        for b in range(N_RBG):
            u = chosen.intra.pick(b)
            if u is not None:
                self.grant(b, u, chosen.intra)
                chosen.rbgs += 1

    def _priority(self):
        """HTB-like, the analogue of ADR-019: in priority order each slice first gets up to its
        guaranteed share (share * |RBG| with the fraction carried over), then the RBGs left over
        go, in priority order, to whichever slice can use them. Channel-agnostic RBG order."""
        order = sorted(self.slices, key=lambda s: s.prio)
        free = list(range(N_RBG))
        for s in order:
            t = s.share * N_RBG + s.off
            g = int(math.floor(t))
            s.off = t - g
            taken = 0
            for b in list(free):
                if taken >= g or not s.intra.active():
                    break
                u = s.intra.pick(b)
                if u is not None:
                    self.grant(b, u, s.intra)
                    s.rbgs += 1
                    free.remove(b)
                    taken += 1
        for s in order:
            for b in list(free):
                if not s.intra.active():
                    break
                u = s.intra.pick(b)
                if u is not None:
                    self.grant(b, u, s.intra)
                    s.rbgs += 1
                    free.remove(b)

    def _fill(self, free, channel_aware):
        """Work-conserving fill: RBGs no slice could use within its quota go to any slice that can
        use them (by the best channel quality if channel-aware, else in slice order)."""
        for b in sorted(free):
            best, bs, bv = None, None, -1.0
            for s in self.slices:
                u = s.intra.pick(b)
                if u is None:
                    continue
                v = SE[u.cq[b]] if channel_aware else 0.0
                if v > bv:
                    best, bs, bv = u, s, v
            if best is not None:
                self.grant(b, best, bs.intra)
                bs.rbgs += 1
                free.discard(b)

    def _quota_seq(self):
        active = [s.intra.active() for s in self.slices]
        q = self.quotas(active, redistribute=True)
        free = set(range(N_RBG))
        b = 0
        for s, qs in zip(self.slices, q):
            taken = 0
            while taken < qs and b < N_RBG:
                u = s.intra.pick(b)
                if u is not None:
                    self.grant(b, u, s.intra)
                    s.rbgs += 1
                    free.discard(b)
                taken += 1
                b += 1
        self._fill(free, channel_aware=False)

    def _radiosaber(self):
        """RadioSaber (Chen et al., NSDI'23, Algorithm 2): every slice's enterprise scheduler is
        asked which of its users would get each RBG; repeatedly the (RBG, slice) pair with the best
        channel quality among unassigned RBGs and slices under quota is granted, and that slice is
        asked again (its answer may change once a user's backlog is covered). Then a
        work-conserving fill (our addition) for RBGs no slice could use within its quota."""
        active = [s.intra.active() for s in self.slices]
        q = self.quotas(active, redistribute=True)
        cnt = [0] * len(self.slices)
        free = set(range(N_RBG))
        while free:
            best = None
            bv = -1.0
            for i, s in enumerate(self.slices):
                if not active[i] or cnt[i] >= q[i]:
                    continue
                for b in free:
                    u = s.intra.pick(b)
                    if u is not None:
                        v = SE[u.cq[b]]
                        if v > bv or (v == bv and b < best[0]):
                            bv, best = v, (b, i, u)
            if best is None:
                break
            b, i, u = best
            self.grant(b, u, self.slices[i].intra)
            self.slices[i].rbgs += 1
            cnt[i] += 1
            free.discard(b)
        self._fill(free, channel_aware=True)

    def _none(self):
        for b in range(N_RBG):
            u = self.none.pick(b)
            if u is not None:
                self.grant(b, u, self.none)
                for s in self.slices:
                    if s.name == u.slice:
                        s.rbgs += 1

    # -- one slot ---------------------------------------------------------------------------------
    def step(self, slot):
        self.slot = slot
        now = slot * SLOT_MS
        period = slot // CSI_PERIOD
        self.assigned = [None] * N_RBG
        for u in self.users:
            if u.full:
                u.budget = INF
            else:
                u.budget = u.qbits
            if u.budget > 0:
                u.cq = u.ch.at(period)
        if self.none is not None:
            self.none.prepare(slot, now)
        else:
            for s in self.slices:
                s.intra.prepare(slot, now)
        getattr(self, "_" + self.inter.replace("-", "_"))()


# ------------------------------------------------------------------------------------ scenarios
SLICES = {
    # name: (SST, UEs, weight = guaranteed share of the IP-level scheduler (ADR-019), priority)
    "eMBB": dict(sst=1, n=20, share=0.60, prio=2),
    "URLLC": dict(sst=2, n=10, share=0.10, prio=1, critical=3, size_b=64, period_ms=20.0, budget_ms=10.0),
    "mMTC": dict(sst=3, n=70, share=0.05, prio=3, size_b=10, period_ms=10000.0, budget_ms=1000.0),
}
EMBB_PKT_B = 1500

RADIOS = {
    # ASSUMED values, not a 3GPP deployment model: mean SNR at the cell edge, log-normal shadowing,
    # and the excess delay of the 4-tap channel (larger = more frequency-selective across RBGs)
    "base": dict(snr_edge_db=2.0, shadow_db=4.0, tau_max_us=0.1),
    "spread": dict(snr_edge_db=-4.0, shadow_db=8.0, tau_max_us=1.0),
}

SCENARIOS = {
    # embb: "poisson" (embb_mbps for the whole slice) or "full" (full buffer).
    # urllc_flood_ms: the 7 standard URLLC devices send 64 B every this many ms instead of every 20 ms
    # (the analogue of ADR-019 experiment B); the 3 critical devices keep their 20 ms period.
    "normal": dict(embb="poisson", embb_mbps=30.0, radio="base", urllc_flood_ms=None,
                   desc="eMBB offers 30 Mbps in total (Poisson 1500-byte packets), below capacity"),
    "overload": dict(embb="full", embb_mbps=None, radio="base", urllc_flood_ms=None,
                     desc="eMBB full buffer: demand above capacity; same radio and UE drop as normal"),
    "urllc-flood": dict(embb="full", embb_mbps=None, radio="base", urllc_flood_ms=0.25,
                        desc="overload, and the 7 standard URLLC devices flood (64 B every 0.25 ms, "
                             "14.3 Mbps) — two busy slices"),
    "spread": dict(embb="full", embb_mbps=None, radio="spread", urllc_flood_ms=0.25,
                   desc="as urllc-flood, on a radio with a wide SNR spread (edge -4 dB, 8 dB "
                        "shadowing) and strongly frequency-selective fading: where channel awareness "
                        "should matter"),
}

# per-slice intra schedulers of the inter-slice study (a customised scheduler per slice, the
# RadioSaber idea): throughput-fair eMBB, delay-aware URLLC, plain turns for mMTC reports
DEFAULT_INTRA = {"eMBB": "pf", "URLLC": "mlwdf", "mMTC": "rr"}


def mean_snr_db(rng, radio):
    """Uniform by area in an annulus [0.1 R, R]; path-loss exponent 3.5 from the edge SNR; log-normal
    shadowing; clipped to [-15, 35] dB. ASSUMED values, not a 3GPP deployment model."""
    r = math.sqrt(rng.uniform(0.01, 1.0))
    snr = radio["snr_edge_db"] + 35.0 * math.log10(1.0 / r) + rng.gauss(0.0, radio["shadow_db"])
    return max(-15.0, min(35.0, snr))


def build(scenario, inter, intra, seed, duration_s):
    """Users, slices, cell and the packet arrivals of one run. `intra` is a kind applied to every
    slice, or "default" for DEFAULT_INTRA."""
    sc = SCENARIOS[scenario]
    radio = RADIOS[sc["radio"]]
    base = sorted(RADIOS).index(sc["radio"]) + 1      # the same UE drop and fading for one radio
    rng_pos = random.Random(1_000_003 * seed + 101 * base)
    taus = [radio["tau_max_us"] * 1e-6 * l / 3.0 for l in range(4)]
    phase = []
    for b in range(N_RBG):
        f = (b * RBG_SIZE + RBG_PRBS[b] / 2.0) * 12 * SCS_KHZ * 1000.0
        phase.append([cmath.exp(-2j * math.pi * f * t) for t in taus])
    fd = UE_SPEED_KMH / 3.6 * CARRIER_HZ / 3e8
    rho = bessel_j0(2 * math.pi * fd * CSI_PERIOD * SLOT_MS / 1000.0)
    horizon_ms = duration_s * 1000.0
    arrivals = []
    slices = []
    uid = 0
    for name in ("eMBB", "URLLC", "mMTC"):
        cfg = SLICES[name]
        users = []
        for k in range(cfg["n"]):
            ch = Channel(3_000_017 * seed + 7_919 * base + uid, mean_snr_db(rng_pos, radio), phase, rho)
            rng_tr = random.Random(2_000_003 * seed + 7_927 * base + uid)   # this UE's packets
            if name == "eMBB":
                u = UE(uid, name, ch, full=(sc["embb"] == "full"))
                if not u.full:
                    lam = sc["embb_mbps"] * 1e6 / cfg["n"] / (EMBB_PKT_B * 8) / 1000.0   # per ms
                    t = rng_tr.expovariate(lam)
                    while t < horizon_ms:
                        arrivals.append((t, uid, EMBB_PKT_B * 8))
                        t += rng_tr.expovariate(lam)
            else:
                tier = 1 if (name == "URLLC" and k < cfg["critical"]) else 0
                u = UE(uid, name, ch, tier=tier, budget_ms=cfg["budget_ms"], discard=(name == "URLLC"))
                period = cfg["period_ms"]
                if name == "URLLC" and not tier and sc["urllc_flood_ms"]:
                    period = sc["urllc_flood_ms"]
                t = rng_tr.uniform(0.0, cfg["period_ms"]) % period
                while t < horizon_ms:
                    arrivals.append((t, uid, cfg["size_b"] * 8))
                    t += period
            users.append(u)
            uid += 1
        kind = DEFAULT_INTRA[name] if intra == "default" else intra
        slices.append(Slice(name, users, Intra(users, kind, tiered=(inter == "priority")),
                            weight=cfg["share"], share=cfg["share"], prio=cfg["prio"]))
    arrivals.sort()
    none_kind = "mlwdf" if intra == "default" else intra
    cell = Cell(slices, inter, seed=seed, none_kind=none_kind)
    return cell, arrivals


def simulate(cell, arrivals, n_slots, warmup_slots, on_slot=None):
    """Run the cell; returns the raw statistics. A packet that arrives at t is eligible from the first
    slot boundary at or after t, and delivered at the end of the slot that carries its last bit."""
    users = {u.id: u for u in cell.users}
    delays = {s.name: [] for s in cell.slices}
    tiers = []
    dropped = []                        # tiers of URLLC packets discarded past their budget
    discarders = [u for u in cell.users if u.discard]
    nxt = 0
    alpha = cell.alpha
    bits_after = {u.id: 0 for u in cell.users}
    prb_used = 0
    cap_bits = 0
    slice_rbgs = {s.name: 0 for s in cell.slices}
    for slot in range(n_slots):
        now = slot * SLOT_MS
        while nxt < len(arrivals) and arrivals[nxt][0] <= now:
            t, uid, bits = arrivals[nxt]
            u = users[uid]
            u.q.append([t, bits])
            u.qbits += bits
            nxt += 1
        for u in discarders:            # a packet that cannot make its budget any more is dropped
            q = u.q
            while q and now + SLOT_MS - q[0][0] > u.budget_ms:
                t, rem = q.popleft()
                u.qbits -= rem
                if t >= warmup_slots * SLOT_MS:
                    dropped.append(u.tier)
        for s in cell.slices:
            s.rbgs = 0
        cell.prb_used = 0
        cell.cap_bits = 0
        cell.step(slot)
        end = now + SLOT_MS
        measured = slot >= warmup_slots
        for u in cell.users:
            sb = u.slot_bits
            if sb:
                if u.full:
                    got = sb
                else:
                    got = 0
                    while sb > 0 and u.q:
                        head = u.q[0]
                        take = head[1] if head[1] < sb else sb
                        head[1] -= take
                        sb -= take
                        got += take
                        if head[1] == 0:
                            u.q.popleft()
                            if head[0] >= warmup_slots * SLOT_MS:
                                delays[u.slice].append(end - head[0])
                                if u.slice == "URLLC":
                                    tiers.append(u.tier)
                    u.qbits -= got
                if measured:
                    bits_after[u.id] += got
                u.avg = (1 - alpha) * u.avg + alpha * got
                u.slot_bits = 0
            else:
                u.avg = (1 - alpha) * u.avg
        if measured:
            prb_used += cell.prb_used
            cap_bits += cell.cap_bits
            for s in cell.slices:
                slice_rbgs[s.name] += s.rbgs
        if on_slot:
            on_slot(slot, cell)
    # packets still queued at the end: a URLLC packet already past its budget is a miss (its age is
    # a lower bound on its latency); younger ones are censored and left out
    end_ms = n_slots * SLOT_MS
    censored_late = {s.name: [] for s in cell.slices}
    undelivered = {s.name: 0 for s in cell.slices}
    for u in cell.users:
        if u.rt:
            for t, _ in u.q:
                if t >= warmup_slots * SLOT_MS:
                    undelivered[u.slice] += 1
                    if end_ms - t > u.budget_ms:
                        censored_late[u.slice].append((end_ms - t, u.tier))
                    elif u.slice == "URLLC":
                        undelivered[u.slice] -= 1        # still within budget at the end: censored
    return dict(delays=delays, tiers=tiers, dropped=dropped, bits=bits_after, prb_used=prb_used, cap_bits=cap_bits,
                slice_rbgs=slice_rbgs, censored_late=censored_late, undelivered=undelivered)


# ------------------------------------------------------------------------------------ metrics
def quantile(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    pos = (len(s) - 1) * p
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def jain(xs):
    sq = sum(x * x for x in xs)
    return (sum(xs) ** 2) / (len(xs) * sq) if sq > 0 else float("nan")


def metrics(cell, raw, measured_s):
    by = {s.name: s.users for s in cell.slices}
    embb = [raw["bits"][u.id] / measured_s / 1e6 for u in by["eMBB"]]
    total_bits = sum(raw["bits"].values())
    # URLLC: every packet generated after the warm-up is delivered (latency), discarded past its
    # budget (latency = inf), or still queued at the end past its budget (late); younger ones left out
    ul = list(raw["delays"]["URLLC"]) + [d for d, _ in raw["censored_late"]["URLLC"]] + [INF] * len(raw["dropped"])
    ul_tier = list(raw["tiers"]) + [t for _, t in raw["censored_late"]["URLLC"]] + list(raw["dropped"])
    budget = SLICES["URLLC"]["budget_ms"]
    crit = [d for d, t in zip(ul, ul_tier) if t == 1]
    std = [d for d, t in zip(ul, ul_tier) if t == 0]
    delivered = list(raw["delays"]["URLLC"])
    mm = raw["delays"]["mMTC"]
    n_slots = measured_s * 1000.0 / SLOT_MS
    rb_total = sum(raw["slice_rbgs"].values()) or 1
    return {
        "embb_agg_mbps": sum(embb),
        "embb_mean_ue_mbps": sum(embb) / len(embb),
        "embb_p5_ue_mbps": quantile(embb, 0.05),
        "embb_jain": jain(embb),
        "embb_p95_delay_ms": quantile(raw["delays"]["eMBB"], 0.95) if raw["delays"]["eMBB"] else None,
        "urllc_packets": len(ul),
        "urllc_miss_rate": (sum(1 for d in ul if d > budget) / len(ul)) if ul else float("nan"),
        "urllc_crit_miss_rate": (sum(1 for d in crit if d > budget) / len(crit)) if crit else float("nan"),
        "urllc_std_miss_rate": (sum(1 for d in std if d > budget) / len(std)) if std else float("nan"),
        "urllc_dropped": len(raw["dropped"]),
        "urllc_p99_ms": quantile(delivered, 0.99),
        "urllc_mean_ms": (sum(delivered) / len(delivered)) if delivered else float("nan"),
        "mmtc_reports": len(mm),
        "mmtc_mean_ms": (sum(mm) / len(mm)) if mm else float("nan"),
        "mmtc_p95_ms": quantile(mm, 0.95),
        "mmtc_undelivered": raw["undelivered"]["mMTC"],
        "cell_mbps": total_bits / measured_s / 1e6,
        "cell_se_bps_hz": total_bits / measured_s / BANDWIDTH_HZ,
        "alloc_se_bit_re": (raw["cap_bits"] / (raw["prb_used"] * RE_PER_PRB)) if raw["prb_used"] else 0.0,
        "prb_util": raw["prb_used"] / (n_slots * N_PRB),
        "rbg_share_embb": raw["slice_rbgs"]["eMBB"] / rb_total,
        "rbg_share_urllc": raw["slice_rbgs"]["URLLC"] / rb_total,
        "rbg_share_mmtc": raw["slice_rbgs"]["mMTC"] / rb_total,
    }


def run_one(args):
    scenario, inter, intra, seed, duration_s, warmup_s = args
    t0 = time.time()
    cell, arr = build(scenario, inter, intra, seed, duration_s)
    n = int(round(duration_s * 1000 / SLOT_MS))
    w = int(round(warmup_s * 1000 / SLOT_MS))
    raw = simulate(cell, arr, n, w)
    m = metrics(cell, raw, duration_s - warmup_s)
    m.update(scenario=scenario, inter=inter, intra=intra, seed=seed, wall_s=round(time.time() - t0, 2))
    return m


# 97.5 % quantile of Student's t for n - 1 degrees of freedom
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 19: 2.093, 20: 2.086,
        24: 2.064, 29: 2.045}


def ci95(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    n = len(xs)
    if n == 0:
        return None, None
    mu = sum(xs) / n
    if n < 2:
        return mu, 0.0
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))
    df = n - 1
    t = T975.get(df) or T975[max(k for k in T975 if k <= df)] if df <= 29 else 1.96
    return mu, t * sd / math.sqrt(n)


METRIC_KEYS = ["embb_agg_mbps", "embb_mean_ue_mbps", "embb_p5_ue_mbps", "embb_jain", "embb_p95_delay_ms",
               "urllc_miss_rate", "urllc_crit_miss_rate", "urllc_std_miss_rate", "urllc_dropped",
               "urllc_p99_ms", "urllc_mean_ms",
               "mmtc_mean_ms", "mmtc_p95_ms", "mmtc_undelivered", "cell_mbps", "cell_se_bps_hz",
               "alloc_se_bit_re", "prb_util", "rbg_share_embb", "rbg_share_urllc", "rbg_share_mmtc"]


def summarise(runs):
    groups = {}
    for r in runs:
        groups.setdefault((r["scenario"], r["inter"], r["intra"]), []).append(r)
    out = []
    for (sc, inter, intra), rs in groups.items():
        row = {"scenario": sc, "inter": inter, "intra": intra, "seeds": len(rs)}
        for k in METRIC_KEYS:
            mu, hw = ci95([r[k] for r in rs])
            row[k] = mu
            row[k + "_ci95"] = hw
        out.append(row)
    return out


# ------------------------------------------------------------------------------------ reporting
def fmt(row, k, nd=1, scale=1.0):
    mu, hw = row.get(k), row.get(k + "_ci95")
    if mu is None:
        return "—"
    return ("%.*f ± %.*f" % (nd, mu * scale, nd, (hw or 0.0) * scale))


INTER_STUDY = [("static", "default"), ("nvs", "default"), ("priority", "default"),
               ("quota-seq", "default"), ("radiosaber", "default"), ("none", "default")]
INTRA_STUDY = [("static", k) for k in INTRA_KINDS]
INTER_LABEL = {"static": "static split", "nvs": "NVS", "priority": "priority (HTB-like)",
               "quota-seq": "quota-seq (ablation)", "radiosaber": "RadioSaber", "none": "none (one M-LWDF)"}
INTRA_LABEL = {"rr": "Round Robin", "maxci": "Max-C/I", "pf": "Proportional Fair", "mlwdf": "M-LWDF",
               "exppf": "EXP/PF"}


def tables_md(summary, seeds, duration_s, warmup_s):
    idx = {(r["scenario"], r["inter"], r["intra"]): r for r in summary}
    lines = []
    lines.append("All values: mean ± 95 %% confidence half-width over %d seeds; %.1f s per run "
                 "(%.1f s warm-up excluded). SIMULATION, not the testbed." % (seeds, duration_s, warmup_s))
    lines.append("")
    for study, combos, title in (("inter", INTER_STUDY, "Inter-slice schedulers (intra: eMBB PF, URLLC M-LWDF, mMTC RR)"),
                                 ("intra", INTRA_STUDY, "Intra-slice schedulers (inter: static split; the same scheduler in every slice)")):
        lines.append("### %s" % title)
        lines.append("")
        for sc in SCENARIOS:
            lines.append("**%s** — %s" % (sc, SCENARIOS[sc]["desc"]))
            lines.append("")
            lines.append("| Scheduler | eMBB aggregate (Mbps) | eMBB 5th pct UE (Mbps) | eMBB Jain | URLLC miss (%) "
                         "| URLLC critical miss (%) | URLLC p99 delivered (ms) | mMTC mean / p95 (ms) | Cell SE (bit/s/Hz) "
                         "| SE of used RBGs (bit/RE) | PRB use (%) |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for inter, intra in combos:
                r = idx.get((sc, inter, intra))
                if not r:
                    continue
                name = INTER_LABEL[inter] if study == "inter" else INTRA_LABEL[intra]
                lines.append("| %s | %s | %s | %s | %s | %s | %s | %s / %s | %s | %s | %s |" % (
                    name, fmt(r, "embb_agg_mbps"), fmt(r, "embb_p5_ue_mbps", 2), fmt(r, "embb_jain", 2),
                    fmt(r, "urllc_miss_rate", 2, 100), fmt(r, "urllc_crit_miss_rate", 2, 100),
                    fmt(r, "urllc_p99_ms", 1), fmt(r, "mmtc_mean_ms", 1), fmt(r, "mmtc_p95_ms", 1),
                    fmt(r, "cell_se_bps_hz", 2), fmt(r, "alloc_se_bit_re", 2), fmt(r, "prb_util", 0, 100)))
            lines.append("")
    return "\n".join(lines)


# Paired by seed: every scheduler of a seed sees the same radio and packets, so the per-seed
# difference removes the radio's variance and isolates the scheduler's own effect.
PAIRS = [("radiosaber", "quota-seq", "channel awareness alone (same quotas, same fill)"),
         ("radiosaber", "static", "RadioSaber against a static split"),
         ("priority", "static", "priority (ADR-019 analogue) against a static split")]
PAIR_METRICS = [("alloc_se_bit_re", "SE of used RBGs (bit/RE)", 1.0, 3),
                ("cell_se_bps_hz", "cell SE (bit/s/Hz)", 1.0, 3),
                ("embb_agg_mbps", "eMBB aggregate (Mbps)", 1.0, 2),
                ("urllc_miss_rate", "URLLC miss (pp)", 100.0, 2),
                ("mmtc_mean_ms", "mMTC mean delay (ms)", 1.0, 2)]


def paired(runs, a, b, scenario, metric, intra="default"):
    by = {}
    for r in runs:
        if r["scenario"] == scenario and r["intra"] == intra and r["inter"] in (a, b):
            by.setdefault(r["seed"], {})[r["inter"]] = r[metric]
    diffs = [v[a] - v[b] for v in by.values() if a in v and b in v and v[a] is not None and v[b] is not None
             and not (math.isnan(v[a]) or math.isnan(v[b]))]
    return ci95(diffs)


def paired_md(runs):
    lines = ["### Paired differences (A minus B, per seed; mean ± 95 % CI)", "",
             "The same seed gives every scheduler the same UE drop, fading and packets, so a per-seed "
             "difference whose interval excludes 0 is a real effect of the scheduler in this model.", ""]
    for a, b, what in PAIRS:
        lines.append("**%s − %s** — %s" % (INTER_LABEL[a], INTER_LABEL[b], what))
        lines.append("")
        lines.append("| Scenario | " + " | ".join("Δ " + m[1] for m in PAIR_METRICS) + " |")
        lines.append("|---|" + "---|" * len(PAIR_METRICS))
        for sc in SCENARIOS:
            cells = []
            for k, _, scale, nd in PAIR_METRICS:
                mu, hw = paired(runs, a, b, sc, k)
                cells.append("—" if mu is None else "%+.*f ± %.*f" % (nd, mu * scale, nd, hw * scale))
            lines.append("| %s | %s |" % (sc, " | ".join(cells)))
        lines.append("")
    return "\n".join(lines)


def plots(summary, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    idx = {(r["scenario"], r["inter"], r["intra"]): r for r in summary}
    made = []
    for metric, ylabel, scale, fname in (
            ("embb_agg_mbps", "eMBB aggregate throughput (Mbps)", 1.0, "inter-embb-throughput.png"),
            ("urllc_miss_rate", "URLLC deadline-miss rate (%)", 100.0, "inter-urllc-miss.png"),
            ("alloc_se_bit_re", "spectral efficiency of used RBGs (bit/RE)", 1.0, "inter-se.png")):
        fig, ax = plt.subplots(figsize=(9, 4))
        scs = list(SCENARIOS)
        w = 0.8 / len(INTER_STUDY)
        for i, (inter, intra) in enumerate(INTER_STUDY):
            ys, es = [], []
            for sc in scs:
                r = idx.get((sc, inter, intra), {})
                ys.append((r.get(metric) or 0.0) * scale)
                es.append((r.get(metric + "_ci95") or 0.0) * scale)
            xs = [j + (i - (len(INTER_STUDY) - 1) / 2) * w for j in range(len(scs))]
            ax.bar(xs, ys, w, yerr=es, capsize=2, label=INTER_LABEL[inter])
        ax.set_xticks(range(len(scs)))
        ax.set_xticklabels(scs)
        ax.set_ylabel(ylabel)
        ax.set_title("SIMULATION — inter-slice schedulers, mean ± 95 % CI")
        ax.legend(fontsize=7, ncol=3)
        fig.tight_layout()
        fig.savefig(os.path.join(out, fname), dpi=110)
        plt.close(fig)
        made.append(fname)
    return made


def main():
    ap = argparse.ArgumentParser(description="SIMULATION: slot-based 5G NR downlink MAC slicing study (ADR-022)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                                  "docs", "evidence", "mac-sim"))
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--duration", type=float, default=5.5, help="seconds per run, warm-up included")
    ap.add_argument("--warmup", type=float, default=0.5)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--no-plots", action="store_true")
    ap.add_argument("--one", nargs=4, metavar=("SCENARIO", "INTER", "INTRA", "SEED"),
                    help="one run, its metrics printed as JSON")
    ap.add_argument("--report-only", action="store_true",
                    help="regenerate results.md and the plots from an existing results.json")
    a = ap.parse_args()
    if a.report_only:
        out = os.path.abspath(a.out)
        with open(os.path.join(out, "results.json"), encoding="utf-8") as f:
            d = json.load(f)
        print(report(out, d["config"], d["summary"], d["runs"], plots_too=not a.no_plots))
        return 0
    if a.one:
        print(json.dumps(run_one((a.one[0], a.one[1], a.one[2], int(a.one[3]), a.duration, a.warmup)), indent=1))
        return 0
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    combos = []
    seen = set()
    for inter, intra in INTER_STUDY + INTRA_STUDY:
        if (inter, intra) not in seen:
            seen.add((inter, intra))
            combos.append((inter, intra))
    jobs = [(sc, inter, intra, seed, a.duration, a.warmup)
            for sc in SCENARIOS for inter, intra in combos for seed in range(1, a.seeds + 1)]
    t0 = time.time()
    if a.jobs > 1:
        import multiprocessing as mp
        with mp.Pool(a.jobs) as pool:
            runs = []
            for i, r in enumerate(pool.imap_unordered(run_one, jobs, chunksize=1)):
                runs.append(r)
                if (i + 1) % 20 == 0:
                    print("  %d/%d runs, %.0f s" % (i + 1, len(jobs), time.time() - t0), file=sys.stderr)
    else:
        runs = [run_one(j) for j in jobs]
    for r in runs:
        r.pop("wall_s", None)           # so that a re-run reproduces the files byte for byte
    runs.sort(key=lambda r: (r["scenario"], r["inter"], r["intra"], r["seed"]))
    summary = summarise(runs)
    config = {
        "label": "SIMULATION (tools/mac-sim/macsim.py), not a measurement of the testbed",
        "carrier": {"scs_khz": SCS_KHZ, "slot_ms": SLOT_MS, "n_prb": N_PRB, "rbg_prbs": RBG_PRBS,
                    "re_per_prb": RE_PER_PRB, "csi_period_slots": CSI_PERIOD, "carrier_hz": CARRIER_HZ,
                    "ue_speed_kmh": UE_SPEED_KMH, "snr_gap_db": SNR_GAP_DB, "harq": "not modelled"},
        "cqi_table": "TS 38.214 Table 5.2.2.1-2", "slices": SLICES, "scenarios": SCENARIOS,
        "default_intra": DEFAULT_INTRA, "seeds": a.seeds, "duration_s": a.duration, "warmup_s": a.warmup,
        "command": "python3 tools/mac-sim/macsim.py --seeds %d --duration %g --warmup %g"
                   % (a.seeds, a.duration, a.warmup),
    }
    with open(os.path.join(out, "results.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"config": config, "summary": summary, "runs": runs}, f, indent=1, sort_keys=True)
        f.write("\n")
    for name, rows, keys in (("summary.csv", summary, None), ("runs.csv", runs, None)):
        keys = sorted(rows[0].keys())
        lead = [k for k in ("scenario", "inter", "intra", "seed", "seeds") if k in keys]
        keys = lead + [k for k in keys if k not in lead]
        with open(os.path.join(out, name), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow(r)
    made = report(out, config, summary, runs, plots_too=not a.no_plots)
    print("%d runs in %.0f s -> %s %s" % (len(runs), time.time() - t0, out, made))
    return 0


def report(out, config, summary, runs, plots_too=True):
    """results.md (tables and paired differences) and the plots, from the runs."""
    md = tables_md(summary, config["seeds"], config["duration_s"], config["warmup_s"])
    with open(os.path.join(out, "results.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# MAC scheduler simulation — results (generated by tools/mac-sim/macsim.py)\n\n")
        f.write("SIMULATION. Reproduce: `%s`\n\n" % config["command"])
        f.write(md + "\n")
        f.write(paired_md(runs) + "\n")
    return plots(summary, out) if plots_too else []


if __name__ == "__main__":
    sys.exit(main())
