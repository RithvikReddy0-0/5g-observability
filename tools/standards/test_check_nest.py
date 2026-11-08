#!/usr/bin/env python3
"""Unit tests for tools/standards/check_nest.py (ADR-023). No stack: the templates, the GST list
and the deployment's own definitions are files. Each rejection test breaks one thing in a copy of
a real template and expects exactly that to be reported — a validator never seen to reject
anything is indistinguishable from one that accepts everything."""

import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_nest as C  # noqa: E402

GST = C.load_gst()
ENV = C.load_env()
TEMPLATES = dict((os.path.basename(p), d) for p, d in C.load_templates())


def errors(doc):
    return [m for level, m in C.validate(doc, GST, env=ENV) if level == "error"]


def warnings(doc):
    return [m for level, m in C.validate(doc, GST, env=ENV) if level == "warning"]


def find(doc, param):
    return next(a for a in doc["attributes"] if a["parameter"] == param)


class Parser(unittest.TestCase):
    def test_subset(self):
        doc = C.parse_yaml(
            "# comment\n"
            "a: 1\n"
            "b: \"010203\"  # a comment after a value\n"
            "c:\n"
            "  d: x # y\n"
            "  e: 0.0001\n"
            "list:\n"
            "  - k: v\n"
            "    n: 2\n"
            "  - k: 'it''s'\n"
            "scalars:\n"
            "  - 1e-6\n"
            "  - plain text\n")
        self.assertEqual(doc["a"], 1)
        self.assertEqual(doc["b"], "010203")
        self.assertEqual(doc["c"], {"d": "x", "e": 0.0001})
        self.assertEqual(doc["list"], [{"k": "v", "n": 2}, {"k": "it's"}])
        self.assertEqual(doc["scalars"], [1e-6, "plain text"])

    def test_rejects_what_it_does_not_understand(self):
        for bad in ("a: [1, 2]\n", "a: {b: 1}\n", "a: &x 1\n", "a: 1\n  b: 2\n", "a: 1\na: 2\n", "a: 'open\n",
                    "a:\n  - plain key: no\n", "a: b: c\n"):
            with self.assertRaises(C.YamlError, msg=bad):
                C.parse_yaml(bad)

    def test_agrees_with_pyyaml_on_the_templates(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        for p, d in C.load_templates():
            with open(p, encoding="utf-8") as f:
                self.assertEqual(yaml.safe_load(f), d, p)


class Templates(unittest.TestCase):
    def test_repository_templates_are_valid(self):
        self.assertEqual(set(TEMPLATES), {"embb.nest.yaml", "urllc.nest.yaml", "mmtc.nest.yaml"})
        for name, doc in TEMPLATES.items():
            self.assertEqual(errors(doc), [], name)
            self.assertEqual(warnings(doc), [], name)

    def test_every_value_has_a_source(self):
        for doc in TEMPLATES.values():
            for a in doc["attributes"]:
                self.assertTrue(a.get("source"), a)

    def test_sources_resolve(self):
        self.assertEqual(C.resolve_from("slices.env:O5GS_UES:B", GST, env=ENV), (10.0, "NA"))
        self.assertEqual(C.resolve_from("slices.env:SLICE_A_SESSION_AMBR_DL", GST, env=ENV), (200.0, "Mbps"))
        self.assertEqual(C.resolve_from("slices.env:SLICE_B_CAPACITY_MBPS", GST, env=ENV), (20.0, "Mbps"))
        self.assertEqual(C.resolve_from("TS23501:82:packet_delay_budget_s", GST), (0.01, "Seconds"))
        self.assertEqual(C.resolve_from("kpi-scorecard.json:urllc-latency-p99", GST), (10.0, "ms"))

    def test_unit_conversion(self):
        self.assertAlmostEqual(C.convert(10, "ms", "Seconds"), 0.01)
        self.assertAlmostEqual(C.convert(200, "Mbps", "kbps"), 200000)
        with self.assertRaises(ValueError):
            C.convert(1, "ms", "kbps")


class Rejections(unittest.TestCase):
    def setUp(self):
        self.doc = copy.deepcopy(TEMPLATES["urllc.nest.yaml"])

    def expect(self, fragment):
        errs = errors(self.doc)
        self.assertTrue(any(fragment in e for e in errs), "expected %r in %s" % (fragment, errs))

    def test_unknown_attribute(self):
        find(self.doc, "Packet Error Rate")["attribute"] = "Slice reliability"
        self.expect("is not a GST attribute")

    def test_unknown_parameter(self):
        find(self.doc, "Packet Error Rate")["parameter"] = "Jitter"
        self.expect("has no parameter 'Jitter'")

    def test_wrong_unit(self):
        find(self.doc, "Packet Delay Budget")["unit"] = "millisecond"
        self.expect("is measured in 'Seconds'")

    def test_wrong_clause(self):
        find(self.doc, "Packet Delay Budget")["clause"] = "3.4.36"
        self.expect("is clause 3.4.26")

    def test_value_drift_from_the_deployment(self):
        find(self.doc, "Maximum number of UEs")["value"] = 12
        self.expect("differs from slices.env:O5GS_UES:B")

    def test_value_drift_from_the_scorecard(self):
        find(self.doc, "Packet Delay Budget")["value"] = 0.02
        self.expect("differs from kpi-scorecard.json:urllc-latency-p99")

    def test_missing_source(self):
        del find(self.doc, "Supported device velocity")["source"]
        self.expect("source is required")

    def test_duplicate(self):
        self.doc["attributes"].append(copy.deepcopy(find(self.doc, "3GPP 5QI")))
        self.expect("already given")

    def test_unknown_field(self):
        find(self.doc, "3GPP 5QI")["note"] = "x"
        self.expect("unknown field 'note'")

    def test_allowed_values(self):
        find(self.doc, "Resource Type")["value"] = "Delay-critical"
        find(self.doc, "Resource Type").pop("from")
        self.expect("must be one of")

    def test_wrong_slice_identity(self):
        self.doc["nest"]["sd"] = "010203"
        self.expect("differs from slices.env SLICE_B_SD")

    def test_wrong_gst_version(self):
        self.doc["gst"] = "GSMA NG.116 v9.0"
        self.expect("gst must be")

    def test_operator_specific_5qi_needs_its_characteristics(self):
        q = find(self.doc, "3GPP 5QI")
        q["value"] = 134
        q.pop("from")
        self.doc["attributes"] = [a for a in self.doc["attributes"] if a["parameter"] != "Packet Error Rate"]
        self.expect("Packet Error Rate must be given")

    def test_deterministic_communication_needs_a_periodicity(self):
        self.doc["attributes"].append({"attribute": "Deterministic communication", "clause": "3.4.4",
                                       "parameter": "Availability", "value": "Supported", "unit": "NA", "source": "test"})
        self.expect("Periodicity must be present")

    def test_non_default_pdb_is_flagged(self):
        a = find(self.doc, "Packet Delay Budget")
        a["value"] = 0.005
        a.pop("from")
        self.assertTrue(any("differs from the default" in w for w in warnings(self.doc)))


class Comparison(unittest.TestCase):
    def setUp(self):
        self.urllc = TEMPLATES["urllc.nest.yaml"]
        self.mmtc = TEMPLATES["mmtc.nest.yaml"]

    def test_delay_critical_pdb(self):
        a = find(self.urllc, "Packet Delay Budget")
        self.assertEqual(C.compare(a, {"urllc.owd_p95_ms": 3, "urllc.owd_p99_ms": 6, "urllc.owd_max_ms": 9.9})[0], "MET")
        self.assertEqual(C.compare(a, {"urllc.owd_p95_ms": 3, "urllc.owd_p99_ms": 6, "urllc.owd_max_ms": 409})[0], "UNRESOLVED")
        self.assertEqual(C.compare(a, {"urllc.owd_p95_ms": 5, "urllc.owd_p99_ms": 12.2, "urllc.owd_max_ms": 64})[0], "MISSED")

    def test_non_gbr_pdb(self):
        a = find(self.mmtc, "Packet Delay Budget")
        self.assertEqual(C.compare(a, {"mmtc.owd_p99_ms": 8, "mmtc.owd_max_ms": 900})[0], "MET")
        self.assertEqual(C.compare(a, {"mmtc.owd_p95_ms": 350, "mmtc.owd_p99_ms": 900})[0], "MISSED")
        self.assertEqual(C.compare(a, {"mmtc.owd_p95_ms": 250, "mmtc.owd_p99_ms": 900})[0], "UNRESOLVED")

    def test_per_against_loss(self):
        a = find(self.urllc, "Packet Error Rate")
        self.assertEqual(C.compare(a, {"urllc.packets": 76700, "urllc.loss_ratio": 0.0})[0], "MET")
        self.assertEqual(C.compare(a, {"urllc.packets": 7000, "urllc.loss_ratio": 0.0})[0], "UNRESOLVED")
        self.assertEqual(C.compare(a, {"urllc.packets": 100293, "urllc.loss_ratio": 0.028815})[0], "MISSED")
        b = find(self.mmtc, "Packet Error Rate")
        self.assertEqual(C.compare(b, {"mmtc.reports": 1076, "mmtc.expected": 1076})[0], "UNRESOLVED")

    def test_ceiling_and_configuration(self):
        a = find(self.urllc, "Maximum number of UEs")
        self.assertEqual(C.compare(a, {"urllc.devices": 10})[0], "WITHIN")
        self.assertEqual(C.compare(a, {"urllc.devices": 11})[0], "EXCEEDS")
        self.assertEqual(C.compare(a, {})[0], "NOT MEASURED")
        self.assertEqual(C.compare(find(self.urllc, "3GPP 5QI"), {})[0], "CONFIG")


if __name__ == "__main__":
    unittest.main(verbosity=1)
