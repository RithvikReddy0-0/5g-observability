#!/usr/bin/env python3
"""Unit tests for the packet scheduler (ADR-019). No stack, no root: the filters are data, and
matches() evaluates them byte for byte the way the kernel's u32 classifier does.

The packets are built the way UERANSIM v3.3.0 builds them (src/lib/rls/rls_pdu.cpp,
EncodeRlsMessage), inside an outer IPv4/UDP header as they cross the UE container's link. If a
capture of real traffic is present (tests/fixtures/rls-sample.pcap, made by
scripts/open5gs/scheduler_experiment.sh), every packet in it is classified too.
"""

import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scheduler as S  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
GNB = {"eMBB": "10.53.0.30", "URLLC": "10.53.0.32", "mMTC": "10.53.0.34"}
UE_HOST = "10.53.0.31"


def ip4(a):
    return bytes(int(x) for x in a.split("."))


def ipv4(src, dst, proto, payload, ihl=5):
    hdr = struct.pack("!BBHHHBBH4s4s", (4 << 4) | ihl, 0, 20 + len(payload), 0, 0, 64, proto, 0, ip4(src), ip4(dst))
    return hdr + payload


def udp(sport, dport, payload):
    return struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload


def rls(msg_type, sti=0x1122334455667788, pdu_type=None, payload=0, pdu=b""):
    """EncodeRlsMessage, byte for byte."""
    out = bytes([0x03, 3, 3, 0, msg_type]) + struct.pack("!Q", sti)
    if msg_type == S.HEARTBEAT:
        out += struct.pack("!iii", 1, 2, 3)
    elif msg_type == S.HEARTBEAT_ACK:
        out += struct.pack("!i", -60)
    elif msg_type == S.PDU_TRANSMISSION:
        out += bytes([pdu_type]) + struct.pack("!III", 7, payload, len(pdu)) + pdu
    elif msg_type == S.PDU_TRANSMISSION_ACK:
        out += struct.pack("!II", 1, 7)
    return out


def downlink(slice_name, msg):
    return ipv4(GNB[slice_name], UE_HOST, 17, udp(S.RLS_PORT, 40001, msg))


def uplink(slice_name, msg):
    return ipv4(UE_HOST, GNB[slice_name], 17, udp(40001, S.RLS_PORT, msg))


def user_ip(src, dst, size=64):
    return ipv4(src, dst, 17, udp(5000, 6000, b"x" * max(0, size - 28)))


ENV = """
SLICE_A_NAME="eMBB"
SLICE_A_ARP_PRIORITY=8
SLICE_A_MAX_LATENCY_MS=300
SLICE_A_SCHED_SHARE=60
SLICE_A_SCHED_PRIO=3
SLICE_B_NAME="URLLC"
SLICE_B_ARP_TIERS="3:1:MAY_PREEMPT:NOT_PREEMPTABLE 7:2:NOT_PREEMPT:PRE_EMPTABLE"
SLICE_B_MAX_LATENCY_MS=10
SLICE_B_SCHED_SHARE=10
SLICE_B_SCHED_PRIO=1
SLICE_C_NAME="mMTC"
SLICE_C_ARP_PRIORITY=12
SLICE_C_MAX_LATENCY_MS=1000
SLICE_C_SCHED_SHARE=5
SLICE_C_SCHED_PRIO=4
O5GS_UES="A=20 B=10 C=70"
SCHED_MODE=sliced
SCHED_CELL_DL_MBPS=250
SCHED_CELL_UL_MBPS=100
SCHED_SIGNALLING_SHARE=2
SCHED_DEFAULT_SHARE=3
"""
COMPOSE = 'UE_PLAN: "ue-slice-a.yaml:20:10.45.0.1:eMBB ue-slice-b.yaml:10:10.46.0.1:URLLC ue-slice-c.yaml:70:10.47.0.1:mMTC"\n'


def config(env=ENV):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "slices.env"), "w") as f:
        f.write(env)
    with open(os.path.join(d, "compose.yaml"), "w") as f:
        f.write(COMPOSE)
    return S.load_config(os.path.join(d, "slices.env"), os.path.join(d, "compose.yaml"))


CRITICAL = {("URLLC", 0): ["10.46.0.5", "10.46.0.7", "10.46.0.9"]}


def label(tree, classid):
    return next(c["label"] for c in tree["classes"] if c["classid"] == classid)


class Format(unittest.TestCase):
    def test_offsets_follow_the_rls_layout(self):
        msg = rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1, pdu=user_ip("10.46.0.5", "10.46.0.1"))
        pkt = uplink("URLLC", msg)
        self.assertEqual(pkt[S.OFF_MSG_TYPE], S.PDU_TRANSMISSION)
        self.assertEqual(pkt[S.OFF_PDU_TYPE], S.PDU_DATA)
        self.assertEqual(struct.unpack("!I", pkt[S.OFF_PSI:S.OFF_PSI + 4])[0], 1)
        self.assertEqual(pkt[S.OFF_INNER] >> 4, 4, "the inner packet must start at offset 54")
        self.assertEqual(pkt[S.OFF_INNER_SRC:S.OFF_INNER_SRC + 4], ip4("10.46.0.5"))

    def test_u32_offsets_need_no_alignment_tricks_beyond_u16(self):
        # 66 and 70 are 2 mod 4: an address must be matched as two u16 keys, never one u32
        for off in (S.OFF_INNER_SRC, S.OFF_INNER_DST):
            self.assertEqual(off % 4, 2)
            for width, _, _, o in S.ip_keys("10.46.0.5", off):
                self.assertEqual(width, 2)
                self.assertEqual(o % 2, 0)


class Classification(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.trees = {d: S.build(self.cfg, d, CRITICAL) for d in S.DIRECTIONS}

    def cls(self, direction, pkt):
        t = self.trees[direction]
        return label(t, S.classify(pkt, t["filters"], t["default"]))

    def test_signalling_is_recognised_in_both_directions(self):
        for d, wrap in (("downlink", downlink), ("uplink", uplink)):
            for m in (rls(S.HEARTBEAT), rls(S.HEARTBEAT_ACK), rls(S.PDU_TRANSMISSION_ACK),
                      rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_RRC, payload=2, pdu=b"\x10" * 40)):
                self.assertEqual(self.cls(d, wrap("eMBB", m)), ("signalling", ""), (d, m[:6]))

    def test_user_data_goes_to_the_slice_of_the_inner_address(self):
        cases = [("10.45.0.12", ("eMBB", "")), ("10.47.0.3", ("mMTC", "")), ("10.46.0.8", ("URLLC", "arp2"))]
        for addr, want in cases:
            dl = downlink("eMBB", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1, pdu=user_ip("1.1.1.1", addr)))
            ul = uplink("eMBB", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1, pdu=user_ip(addr, "1.1.1.1")))
            self.assertEqual(self.cls("downlink", dl), want, addr)
            self.assertEqual(self.cls("uplink", ul), want, addr)

    def test_slice_comes_from_the_packet_not_from_the_gnb(self):
        # a URLLC session carried through the eMBB gNB (a multi-session UE) is still URLLC
        pkt = downlink("eMBB", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=2,
                                   pdu=user_ip("10.53.0.52", "10.46.0.8")))
        self.assertEqual(self.cls("downlink", pkt), ("URLLC", "arp2"))

    def test_critical_subscribers_get_their_own_tier(self):
        for addr in CRITICAL[("URLLC", 0)]:
            pkt = downlink("URLLC", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1,
                                        pdu=user_ip("10.53.0.52", addr)))
            self.assertEqual(self.cls("downlink", pkt), ("URLLC", "arp1"), addr)

    def test_direction_matters(self):
        # the downlink tree reads the inner DESTINATION: an uplink packet's destination is the DN
        ul = uplink("URLLC", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1,
                                 pdu=user_ip("10.46.0.5", "10.53.0.52")))
        self.assertEqual(self.cls("uplink", ul), ("URLLC", "arp1"))
        self.assertEqual(self.cls("downlink", ul), ("other", ""))

    def test_anything_else_lands_in_the_default_class(self):
        scrape = ipv4("10.53.0.10", UE_HOST, 6, b"\x23\x99" + b"\0" * 30)   # TCP, e.g. Prometheus
        self.assertEqual(self.cls("downlink", scrape), ("other", ""))
        other_udp = downlink("eMBB", b"\x00" * 30)[:20] + udp(53, 40001, b"\0" * 20)
        self.assertEqual(self.cls("downlink", other_udp), ("other", ""))

    def test_truncated_packets_do_not_match(self):
        pkt = downlink("eMBB", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1, pdu=b""))
        self.assertEqual(self.cls("downlink", pkt), ("other", ""))


class Tree(unittest.TestCase):
    def test_priorities_order_the_classes(self):
        t = S.build(config(), "downlink", CRITICAL)
        prio = {c["label"]: c.get("prio") for c in t["classes"]}
        self.assertLess(prio[("signalling", "")], prio[("URLLC", "arp1")])
        self.assertLess(prio[("URLLC", "arp1")], prio[("URLLC", "arp2")])
        self.assertLess(prio[("URLLC", "arp2")], prio[("eMBB", "")])
        self.assertLess(prio[("eMBB", "")], prio[("mMTC", "")])
        self.assertLess(prio[("mMTC", "")], prio[("other", "")])

    def test_guarantees_are_shares_of_the_cell_and_every_class_may_borrow_it(self):
        t = S.build(config(), "downlink", CRITICAL)
        rate = {c["label"]: c["rate"] for c in t["classes"]}
        self.assertAlmostEqual(rate[("eMBB", "")], 150.0)
        self.assertAlmostEqual(rate[("URLLC", "all")], 25.0)
        self.assertAlmostEqual(rate[("URLLC", "arp1")] + rate[("URLLC", "arp2")], 25.0)
        self.assertAlmostEqual(rate[("URLLC", "arp1")], 7.5)       # 3 of 10 UEs
        for c in t["classes"]:
            self.assertEqual(c["ceil"], 250.0)

    def test_uplink_uses_the_uplink_cell(self):
        t = S.build(config(), "uplink", CRITICAL)
        self.assertEqual(next(c for c in t["classes"] if c["classid"] == "1:1")["rate"], 100.0)

    def test_delay_critical_slices_get_short_fifos_others_fair_queues(self):
        t = S.build(config(), "downlink", CRITICAL)
        leaf = {c["label"]: c.get("leaf") for c in t["classes"]}
        self.assertTrue(leaf[("URLLC", "arp1")].startswith("pfifo"))
        self.assertEqual(leaf[("eMBB", "")], "fq_codel")

    def test_fifo_mode_is_one_queue_at_the_same_capacity_with_no_filters(self):
        cfg = config()
        cfg["mode"] = "fifo"
        t = S.build(cfg, "downlink", CRITICAL)
        self.assertEqual(t["filters"], [])
        leaves = [c for c in t["classes"] if "leaf" in c]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["rate"], 250.0)
        self.assertTrue(leaves[0]["leaf"].startswith("pfifo"))

    def test_off_mode_installs_nothing(self):
        cfg = config()
        cfg["mode"] = "off"
        self.assertEqual(S.tc_batch(S.build(cfg, "downlink", {}), "veth0"), [])

    def test_tc_lines_are_well_formed(self):
        lines = S.tc_batch(S.build(config(), "downlink", CRITICAL), "veth0")
        self.assertTrue(lines[0].startswith("qdisc add dev veth0 root handle 1: htb default 99"))
        for line in lines:
            self.assertIn(" dev veth0 ", line)
        filters = [l for l in lines if l.startswith("filter")]
        data = [l for l in filters if "match u8 0x2 0xff at 41" in l]               # user data
        self.assertTrue(data and all("at 70" in l and "at 66" not in l for l in data))   # downlink reads dst
        self.assertEqual(sum("flowid 1:31" in l for l in filters), 3)          # one per critical address

    def test_unknown_critical_addresses_fall_back_to_the_pool(self):
        t = S.build(config(), "downlink", {})
        pkt = downlink("URLLC", rls(S.PDU_TRANSMISSION, pdu_type=S.PDU_DATA, payload=1,
                                    pdu=user_ip("10.53.0.52", "10.46.0.5")))
        self.assertEqual(label(t, S.classify(pkt, t["filters"], t["default"])), ("URLLC", "arp2"))


class Configuration(unittest.TestCase):
    def test_the_repository_configuration_is_valid(self):
        cfg = S.load_config()
        self.assertEqual(S.validate(cfg), [])
        self.assertEqual([s["name"] for s in cfg["slices"]], ["eMBB", "URLLC", "mMTC"])

    def test_tiers_follow_imsi_order(self):
        urllc = next(s for s in config()["slices"] if s["name"] == "URLLC")
        self.assertEqual(urllc["tiers"][0]["imsis"], [208930000000021, 208930000000022, 208930000000023])
        self.assertEqual(urllc["tiers"][1]["imsis"][0], 208930000000024)
        self.assertEqual(urllc["pool"], "10.46.")

    def test_overcommitted_guarantees_are_rejected(self):
        bad = config(ENV.replace("SLICE_A_SCHED_SHARE=60", "SLICE_A_SCHED_SHARE=95"))
        self.assertTrue(any("add up to" in e for e in S.validate(bad)))

    def test_a_slice_without_a_share_is_rejected(self):
        bad = config(ENV.replace("SLICE_C_SCHED_SHARE=5\n", ""))
        self.assertTrue(any("mMTC has no guaranteed share" in e for e in S.validate(bad)))

    def test_an_unknown_mode_is_rejected(self):
        bad = config(ENV.replace("SCHED_MODE=sliced", "SCHED_MODE=wfq"))
        self.assertTrue(any("mode must be" in e for e in S.validate(bad)))


def read_pcap(path):
    """Minimal pcap reader (stdlib): yields the IPv4 packets of an Ethernet or Linux-cooked capture."""
    with open(path, "rb") as f:
        head = f.read(24)
        magic = struct.unpack("<I", head[:4])[0]
        endian = "<" if magic in (0xa1b2c3d4, 0xa1b23c4d) else ">"
        linktype = struct.unpack(endian + "I", head[20:24])[0]
        skip = {1: 14, 113: 16, 276: 20}.get(linktype)
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            incl = struct.unpack(endian + "IIII", rec)[2]
            frame = f.read(incl)
            if skip is not None and len(frame) > skip:
                yield frame[skip:]


FIXTURE = os.path.join(REPO, "tests", "fixtures", "rls-sample.pcap")


@unittest.skipUnless(os.path.exists(FIXTURE), "no captured traffic (tests/fixtures/rls-sample.pcap)")
class RealTraffic(unittest.TestCase):
    """Packets captured on the UE container's link while every slice carried traffic."""

    def test_every_captured_radio_packet_is_classified_into_a_slice_or_signalling(self):
        cfg = S.load_config()
        trees = {d: S.build(cfg, d, {}) for d in S.DIRECTIONS}
        seen = {}
        for pkt in read_pcap(FIXTURE):
            if len(pkt) < 24 or pkt[9] != 17:
                continue
            sport, dport = struct.unpack("!HH", pkt[20:24])
            d = "downlink" if sport == S.RLS_PORT else "uplink" if dport == S.RLS_PORT else None
            if d is None:
                continue
            t = trees[d]
            lab = label(t, S.classify(pkt, t["filters"], t["default"]))
            seen[(d, lab[0])] = seen.get((d, lab[0]), 0) + 1
            self.assertNotEqual(lab[0], "other", "a radio packet fell through every filter: %r" % pkt[:60])
        for d in S.DIRECTIONS:
            self.assertGreater(seen.get((d, "signalling"), 0), 0, seen)
        self.assertGreater(sum(v for (d, s), v in seen.items() if s == "URLLC"), 0, seen)
        self.assertGreater(sum(v for (d, s), v in seen.items() if s == "mMTC"), 0, seen)


if __name__ == "__main__":
    unittest.main(verbosity=1)
