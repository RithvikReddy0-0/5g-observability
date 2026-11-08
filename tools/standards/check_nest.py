#!/usr/bin/env python3
"""
check_nest.py — validate the slices' GSMA NG.116 NESTs and compare a KPI run with them (ADR-023).

    tools/standards/check_nest.py                       validate deployments/open5gs/slice-templates/*.nest.yaml
    tools/standards/check_nest.py --run RUN_DIR         ... then compare that run's standard instance
    tools/standards/check_nest.py --run RUN_DIR --label embb_ues=5 --slice URLLC

VALIDATION (CI; no stack needed)
  - every attribute and parameter name, its clause and its unit are NG.116 v10.0's, from
    tools/standards/ng116-v10-gst.json (extracted from the document); allowed values are respected;
  - required fields: each template names its slice (SST/SD as in deployments/slices.env) and every
    value has a unit and a source; no parameter appears twice;
  - NG.116's conditional rules: Periodicity when deterministic communication is Supported, a
    tunnelling mechanism for a private data network, and Resource Type / PDB / PER for a 5QI that
    is not a standardised one (3.4.26); a standardised 5QI given a non-default PDB or PER is flagged;
  - every value with `from:` equals its source today (slices.env, kpi-scorecard.json,
    kpi-gates.json, or TS 23.501's 5QI table), after unit conversion: a template cannot drift.

COMPARISON (`--run`): per attribute with a `check:`
  pdb_delay_critical  delay-critical GBR (5QI 82): TS 23.501 5.7.3.4 counts a packet later than the
                      PDB as lost. MET if no packet exceeded it (max <= PDB); MISSED if p99 > PDB
                      (more than ~1 % late); UNRESOLVED in between (the run keeps p95/p99/max only).
  pdb_98pct           non-GBR: "98 percent of the packets should not experience a delay exceeding
                      the 5QI's PDB" (5.7.3.4). MET if p99 <= PDB; MISSED if p95 > PDB; else UNRESOLVED.
  per                 Packet Error Rate against measured loss: MET if the one-sided 95 % upper
                      bound (Clopper-Pearson) is <= PER, MISSED if the lower bound is > PER,
                      otherwise UNRESOLVED (too few packets to tell).
  max_within          a ceiling: WITHIN if the measurement does not exceed it, else EXCEEDS.
  (none)              CONFIG: a configuration value the KPI run does not measure.
The measured latency is one-way UE -> data network (kpi_collector.py), which includes the N6 hop
that the PDB (UE -> N6 termination at the UPF) excludes: a MISSED is conservative.
"""

import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "kpi"))
import stats  # noqa: E402

GST_FILE = os.path.join(HERE, "ng116-v10-gst.json")
TEMPLATES = os.path.join(REPO, "deployments", "open5gs", "slice-templates")
SLICES_ENV = os.path.join(REPO, "deployments", "slices.env")
SCORECARD = os.path.join(REPO, "deployments", "open5gs", "kpi-scorecard.json")
GATES = os.path.join(REPO, "deployments", "open5gs", "kpi-gates.json")
GST_VERSION = "GSMA NG.116 v10.0"

REQUIRED = ("attribute", "clause", "parameter", "value", "unit", "source")
OPTIONAL = ("from", "check", "measure", "measure_unit")
CHECKS = ("pdb_delay_critical", "pdb_98pct", "per", "max_within")

# Unit -> (dimension, factor to the dimension's base unit). Base units: second, kbps, 1.
UNITS = {
    "Seconds": ("time", 1.0), "second": ("time", 1.0), "s": ("time", 1.0),
    "millisecond": ("time", 1e-3), "ms": ("time", 1e-3),
    "kbps": ("rate", 1.0), "Mbps": ("rate", 1e3), "Gbps": ("rate", 1e6),
    "NA": ("number", 1.0), "ratio": ("number", 1.0), "PDU sessions": ("number", 1.0),
    "devices": ("number", 1.0), "percent": ("number", 1.0),
}


# ------------------------------------------------------------------ restricted YAML

class YamlError(ValueError):
    pass


def _scalar(text):
    t = text.strip()
    if t == "":
        return None
    if t[0] in "\"'":
        q = t[0]
        if len(t) < 2 or t[-1] != q:
            raise YamlError("unterminated string: %s" % t)
        body = t[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\") if q == '"' else body.replace("''", "'")
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if t in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", t):
        return float(t)
    if t[0] in "[{&*!|>%@`" or ": " in t or t.endswith(":"):
        raise YamlError("unsupported YAML syntax (restricted subset; quote strings containing ': '): %s" % t)
    return t


def _strip_comment(line):
    out, q = [], None
    for i, c in enumerate(line):
        if q:
            if c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(c)
    return "".join(out).rstrip()


def parse_yaml(text):
    """The YAML subset the templates use: nested block mappings, block lists of mappings or
    scalars, plain/quoted scalars and comments. Anything else is an error, not a guess."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise YamlError("line %d: tab indentation" % n)
        s = _strip_comment(raw)
        if s.strip():
            lines.append((n, len(s) - len(s.lstrip(" ")), s.strip()))
    pos = [0]

    def block(indent):
        n, ind, s = lines[pos[0]]
        if ind != indent:
            raise YamlError("line %d: bad indentation" % n)
        return seq(indent) if s.startswith("- ") or s == "-" else mapping(indent)

    def keyval(n, s):
        m = re.match(r"^([A-Za-z0-9_.\-]+):(?:\s+(.*))?$", s)
        if not m:
            raise YamlError("line %d: expected 'key: value', got %r" % (n, s))
        return m.group(1), m.group(2)

    def mapping(indent, first=None):
        out = {}
        if first is not None:
            k, v = first
            out[k] = v
        while pos[0] < len(lines):
            n, ind, s = lines[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError("line %d: unexpected indentation" % n)
            if s.startswith("- "):
                break
            k, v = keyval(n, s)
            if k in out:
                raise YamlError("line %d: duplicate key %r" % (n, k))
            pos[0] += 1
            if v is None or v == "":
                if pos[0] < len(lines) and lines[pos[0]][1] > indent:
                    out[k] = block(lines[pos[0]][1])
                elif pos[0] < len(lines) and lines[pos[0]][1] == indent and lines[pos[0]][2].startswith("- "):
                    out[k] = seq(indent)
                else:
                    out[k] = None
            else:
                out[k] = _scalar(v)
        return out

    def seq(indent):
        out = []
        while pos[0] < len(lines):
            n, ind, s = lines[pos[0]]
            if ind != indent or not s.startswith("-"):
                if ind > indent:
                    raise YamlError("line %d: unexpected indentation" % n)
                break
            rest = s[1:].strip()
            pos[0] += 1
            if rest == "":
                out.append(block(lines[pos[0]][1]) if pos[0] < len(lines) and lines[pos[0]][1] > indent else None)
            elif re.match(r"^[A-Za-z0-9_.\-]+:(\s|$)", rest):
                k, v = keyval(n, rest)
                inner = indent + 2
                if v is None or v == "":
                    if pos[0] < len(lines) and lines[pos[0]][1] > inner:
                        first = (k, block(lines[pos[0]][1]))
                    else:
                        first = (k, None)
                else:
                    first = (k, _scalar(v))
                out.append(mapping(inner, first) if pos[0] < len(lines) and lines[pos[0]][1] == inner else dict([first]))
            else:
                out.append(_scalar(rest))
        return out

    if not lines:
        return {}
    doc = block(lines[0][1])
    if pos[0] != len(lines):
        raise YamlError("line %d: unparsed content" % lines[pos[0]][0])
    return doc


# ------------------------------------------------------------------ sources

def load_env(path=SLICES_ENV):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^\s*([A-Z0-9_]+)=("([^"]*)"|(\S*))', line)
            if m:
                env[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(4)
    return env


def load_gst(path=GST_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def convert(value, from_unit, to_unit):
    if from_unit == to_unit:
        return value
    if from_unit not in UNITS or to_unit not in UNITS:
        raise ValueError("cannot convert %s to %s" % (from_unit, to_unit))
    (d1, f1), (d2, f2) = UNITS[from_unit], UNITS[to_unit]
    if d1 != d2:
        raise ValueError("cannot convert %s (%s) to %s (%s)" % (from_unit, d1, to_unit, d2))
    return value * f1 / f2


def resolve_from(ref, gst, env=None, scorecard=None, gates=None):
    """-> (value, unit) that a `from:` reference names today."""
    kind, _, rest = ref.partition(":")
    if kind == "slices.env":
        env = env if env is not None else load_env()
        parts = rest.split(":")
        raw = env.get(parts[0])
        if raw is None:
            raise KeyError("%s is not set in deployments/slices.env" % parts[0])
        if len(parts) == 2:                       # O5GS_UES:B -> the B=<n> item
            items = dict(i.split("=", 1) for i in raw.split())
            return float(items[parts[1]]), "NA"
        m = re.fullmatch(r"\s*([\d.]+)\s*([kMG]bps)\s*", raw)
        if m:
            return float(m.group(1)), m.group(2)
        if parts[0].endswith("_MBPS"):
            return float(raw), "Mbps"
        return float(raw), "NA"
    if kind == "kpi-scorecard.json":
        if scorecard is None:
            with open(SCORECARD, encoding="utf-8") as f:
                scorecard = json.load(f)
        for s in scorecard["slices"]:
            for k in s["kpis"]:
                if k["id"] == rest:
                    t = k.get("project_target", k.get("research_target"))
                    if t is None:
                        raise KeyError("scorecard KPI %s has no target" % rest)
                    return float(t), k["unit"]
        raise KeyError("no scorecard KPI %s" % rest)
    if kind == "kpi-gates.json":
        if gates is None:
            with open(GATES, encoding="utf-8") as f:
                gates = json.load(f)
        g = next((g for g in gates["gates"] if g["id"] == rest), None)
        if g is None:
            raise KeyError("no gate %s" % rest)
        return float(g["threshold"]), "NA"
    if kind == "TS23501":
        qi, _, field = rest.partition(":")
        row = gst["standardized_5qi_used"][qi]
        v = row[field]
        unit = "Seconds" if field.endswith("_s") else "millisecond" if field.endswith("_ms") else "NA"
        return v, unit
    raise KeyError("unknown source %r" % ref)


# ------------------------------------------------------------------ validation

def _index(gst):
    idx = {}
    for a in gst["attributes"]:
        idx[a["name"]] = a
    return idx


def validate(doc, gst, slice_env=None, path="template", **sources):
    """-> list of (level, message), level 'error' or 'warning'."""
    probs = []
    err = lambda m: probs.append(("error", "%s: %s" % (path, m)))   # noqa: E731
    warn = lambda m: probs.append(("warning", "%s: %s" % (path, m)))  # noqa: E731
    if not isinstance(doc, dict):
        return [("error", "%s: not a mapping" % path)]
    if doc.get("gst") != GST_VERSION:
        err("gst must be %r, is %r" % (GST_VERSION, doc.get("gst")))
    nest = doc.get("nest") or {}
    for k in ("name", "sst", "sd"):
        if nest.get(k) in (None, ""):
            err("nest.%s is required" % k)
    env = sources.get("env")
    if env is None:
        env = load_env()
        sources["env"] = env
    letter = next((k[len("SLICE_"):-len("_NAME")] for k, v in env.items()
                   if k.startswith("SLICE_") and k.endswith("_NAME") and v == nest.get("name")), None)
    if letter is None:
        err("nest.name %r is not a slice in deployments/slices.env" % nest.get("name"))
    else:
        if str(nest.get("sst")) != env.get("SLICE_%s_SST" % letter):
            err("nest.sst %s differs from slices.env SLICE_%s_SST=%s" % (nest.get("sst"), letter, env.get("SLICE_%s_SST" % letter)))
        if str(nest.get("sd")).lower() != str(env.get("SLICE_%s_SD" % letter)).lower():
            err("nest.sd %s differs from slices.env SLICE_%s_SD=%s" % (nest.get("sd"), letter, env.get("SLICE_%s_SD" % letter)))
    attrs = doc.get("attributes")
    if not isinstance(attrs, list) or not attrs:
        err("attributes: a non-empty list is required")
        return probs
    idx = _index(gst)
    seen = {}
    for i, a in enumerate(attrs):
        where = "attributes[%d]" % i
        if not isinstance(a, dict):
            err("%s: not a mapping" % where)
            continue
        for k in REQUIRED:
            if a.get(k) in (None, ""):
                err("%s: %s is required" % (where, k))
        for k in a:
            if k not in REQUIRED + OPTIONAL:
                err("%s: unknown field %r" % (where, k))
        g = idx.get(a.get("attribute"))
        if g is None:
            err("%s: %r is not a GST attribute of NG.116 v10.0" % (where, a.get("attribute")))
            continue
        if str(a.get("clause")) != g["clause"]:
            err("%s: %r is clause %s, not %s" % (where, g["name"], g["clause"], a.get("clause")))
        p = next((p for p in g["parameters"] if p["name"] == a.get("parameter")), None)
        if p is None:
            err("%s: %r has no parameter %r (NG.116 lists: %s)" % (where, g["name"], a.get("parameter"),
                                                                   ", ".join(q["name"] for q in g["parameters"])))
            continue
        if a.get("unit") != p["unit"]:
            err("%s: %s / %s is measured in %r (NG.116), not %r" % (where, g["name"], p["name"], p["unit"], a.get("unit")))
        if "allowed" in p and a.get("value") not in p["allowed"]:
            err("%s: %s / %s must be one of %s, is %r" % (where, g["name"], p["name"], p["allowed"], a.get("value")))
        key = (g["name"], p["name"])
        if key in seen:
            err("%s: %s / %s already given in attributes[%d]" % (where, key[0], key[1], seen[key]))
        seen[key] = i
        chk = a.get("check")
        if chk is not None and chk not in CHECKS:
            err("%s: check must be one of %s" % (where, ", ".join(CHECKS)))
        if chk and not a.get("measure"):
            err("%s: check %s needs measure" % (where, chk))
        if chk == "max_within" and a.get("measure_unit") not in UNITS:
            err("%s: max_within needs measure_unit (one of %s)" % (where, ", ".join(sorted(UNITS))))
        if a.get("from"):
            try:
                v, unit = resolve_from(a["from"], gst, **sources)
                if isinstance(v, str) or isinstance(a.get("value"), str):
                    if v != a.get("value"):
                        err("%s: value %r differs from %s = %r" % (where, a.get("value"), a["from"], v))
                else:
                    want = convert(float(v), unit, a["unit"])
                    if abs(want - float(a["value"])) > 1e-9 * max(1.0, abs(want)):
                        err("%s: value %r differs from %s = %s %s (%r %s)" % (where, a.get("value"), a["from"], v, unit, want, a["unit"]))
            except (KeyError, ValueError) as e:
                err("%s: from %r: %s" % (where, a["from"], e))

    def val(attr, param):
        i = seen.get((attr, param))
        return attrs[i].get("value") if i is not None else None

    # NG.116 conditional parameters
    if val("Deterministic communication", "Availability") == "Supported" and val("Deterministic communication", "Periodicity") is None:
        err("3.4.4: Periodicity must be present when deterministic communication Availability is Supported")
    if val("Data network access", "Data access per data network") == "Termination in the private network" and \
            val("Data network access", "Tunnelling mechanism") is None:
        err("3.4.34: Tunnelling mechanism must be present for Termination in the private network")
    qi = val("Slice quality of service", "3GPP 5QI")
    std = gst["standardized_5qi_used"]
    if qi is not None:
        row = std.get(str(qi))
        if row is None:
            for param in ("Resource Type", "Packet Delay Budget", "Packet Error Rate"):
                if val("Slice quality of service", param) is None:
                    err("3.4.26: 5QI %s is not a standardised 5QI known here; %s must be given" % (qi, param))
        else:
            for param, field in (("Resource Type", "resource_type"), ("Priority Level", "priority_level"),
                                 ("Packet Delay Budget", "packet_delay_budget_s"), ("Packet Error Rate", "packet_error_rate")):
                v = val("Slice quality of service", param)
                if v is not None and v != row[field]:
                    warn("3.4.26: %s %r differs from the default %r for standardised 5QI %s (non-default values must be signalled)"
                         % (param, v, row[field], qi))
    return probs


def load_templates(directory=TEMPLATES):
    out = []
    for p in sorted(glob.glob(os.path.join(directory, "*.nest.yaml"))):
        with open(p, encoding="utf-8") as f:
            out.append((p, parse_yaml(f.read())))
    return out


# ------------------------------------------------------------------ comparison with a run

def load_instance(run_dir, label="standard"):
    with open(os.path.join(run_dir, "instances.jsonl"), encoding="utf-8") as f:
        recs = [json.loads(line) for line in f if line.strip()]
    recs = [r for r in recs if r.get("label") == label]
    if not recs:
        raise SystemExit("%s: no instance labelled %r" % (run_dir, label))
    return recs[-1]


def _counts(m, prefix):
    """(sent, lost) for a slice's measurement packets."""
    recv = m.get(prefix + ".packets", m.get(prefix + ".reports"))
    if recv is None:
        return None, None
    exp = m.get(prefix + ".expected")
    if exp is None:
        loss = m.get(prefix + ".loss_ratio") or 0.0
        exp = int(round(recv / (1 - loss))) if loss < 1 else recv
    return int(exp), int(exp - recv)


def compare(a, m):
    """-> (verdict, explanation) for one attribute against measured values m."""
    chk = a.get("check")
    if not chk:
        return "CONFIG", "configuration; not measured by a KPI run"
    v = a["value"]
    if chk in ("pdb_delay_critical", "pdb_98pct"):
        budget = convert(float(v), a["unit"], "ms")
        p95, p99, mx = (m.get("%s.owd_%s_ms" % (a["measure"], s)) for s in ("p95", "p99", "max"))
        txt = "PDB %g ms; measured p95 %s, p99 %s, max %s ms" % (budget, p95, p99, mx)
        if chk == "pdb_delay_critical":
            if mx is not None and mx <= budget:
                return "MET", txt + ": no packet over the PDB"
            if p99 is not None and p99 > budget:
                return "MISSED", txt + ": more than ~1 % of packets later than the PDB, which TS 23.501 counts as lost for delay-critical GBR"
            return "UNRESOLVED", txt + ": between 0 and ~1 % late; resolving it against the PER needs a finer quantile than the run keeps"
        if p99 is not None and p99 <= budget:
            return "MET", txt + ": at least 99 % within the PDB (98 % required)"
        if p95 is not None and p95 > budget:
            return "MISSED", txt + ": more than 5 % of packets later than the PDB"
        return "UNRESOLVED", txt
    if chk == "per":
        n, k = _counts(m, a["measure"])
        if not n:
            return "NOT MEASURED", "no packet counts in this run"
        hi = stats.clopper_pearson_upper(k, n)
        lo = stats.clopper_pearson(k, n, alpha=0.10)[0]      # one-sided 95 % lower bound
        txt = "PER %g; %d lost of %d (one-sided 95 %% bounds %.2g .. %.2g)" % (float(v), k, n, lo, hi)
        if hi <= float(v):
            return "MET", txt + (": loss only; late packets are judged by the PDB check" if a["measure"] == "urllc" else "")
        if lo > float(v):
            return "MISSED", txt
        return "UNRESOLVED", txt + ": %d packets can only show rates down to ~%.1g" % (n, stats.rule_of_three(n))
    if chk == "max_within":
        x = m.get(a["measure"])
        if x is None:
            return "NOT MEASURED", "%s not in this run" % a["measure"]
        x_conv = convert(float(x), a["measure_unit"], a["unit"])
        txt = "%s = %s %s (%g %s) against %g %s" % (a["measure"], x, a["measure_unit"], x_conv, a["unit"], float(v), a["unit"])
        return ("WITHIN", txt) if x_conv <= float(v) else ("EXCEEDS", txt)
    return "?", chk


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--templates", default=TEMPLATES)
    ap.add_argument("--run", help="KPI run directory (instances.jsonl) to compare with the NESTs")
    ap.add_argument("--label", default="standard")
    ap.add_argument("--slice", help="only this slice")
    args = ap.parse_args(argv)
    gst = load_gst()
    templates = load_templates(args.templates)
    if not templates:
        print("no *.nest.yaml in %s" % args.templates)
        return 1
    env = load_env()
    errors = 0
    for path, doc in templates:
        rel = os.path.relpath(path, REPO)
        for level, msg in validate(doc, gst, path=rel, env=env):
            print("%s: %s" % (level.upper(), msg))
            errors += level == "error"
    names = [d.get("nest", {}).get("name") for _, d in templates]
    print("%d NEST template(s) (%s) checked against %s: %d error(s)"
          % (len(templates), ", ".join(str(n) for n in names), GST_VERSION, errors))
    if errors:
        return 1
    if not args.run:
        return 0
    rec = load_instance(args.run, args.label)
    m = rec["measured"]
    print()
    print("Run %s, instance %r (%s s, started %s)" % (os.path.basename(os.path.normpath(args.run)), rec["label"],
                                                       rec["duration_s"], rec.get("started")))
    for _, doc in templates:
        name = doc["nest"]["name"]
        if args.slice and name != args.slice:
            continue
        print()
        print("%s (SST %s, SD %s)" % (name, doc["nest"]["sst"], doc["nest"]["sd"]))
        for a in doc["attributes"]:
            verdict, txt = compare(a, m)
            print("  %-12s %s / %s = %s %s - %s" % (verdict, a["attribute"], a["parameter"], a["value"], a["unit"], txt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
