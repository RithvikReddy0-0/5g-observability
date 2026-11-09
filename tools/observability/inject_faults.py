#!/usr/bin/env python3
"""
inject_faults.py — a held-out test of the fault diagnosis (ADR-024): inject faults on the live stack
that the recorded incidents never contained, and score what diagnose.py says while each is active.

Why: diagnose.py --validate scores 49 recorded incidents, but its rules were written knowing them, so
that is an in-sample consistency check. Here every case's EXPECTED and ACCEPTABLE causes are fixed in
this file before anything runs, and one case is a fault no rule describes (extra delay on the URLLC
user plane's N6), where the right answer is to name no cause.

For each case: inject, hold, run `diagnose.py --last <hold>` (the baseline is the window before), save
its JSON, undo the fault, re-attach UEs if any lost their data path, then let the stack settle so the
next case's baseline is clean. Scoring per case (as incidents.py does): top-1 = the first cause
reported is an expected one; a reported cause that is neither expected nor acceptable is a false
positive; for a case with no expected cause, any cause at critical/warning severity is a false positive.

    python3 tools/observability/inject_faults.py [case ...]     all cases, or the named ones
    python3 tools/observability/inject_faults.py --posthoc       the post-hoc cases (not held-out)
Writes docs/evidence/open5gs-diagnosis-heldout/<UTC timestamp>/ (results.json, summary.md, one
diagnosis JSON per case, run.log). Run from the repo root on the Docker host, nothing else running.
"""

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "kpi"))
import campaign  # noqa: E402  (broken_ues)

HOLD = int(os.environ.get("HOLD", "180"))          # seconds a fault is held before diagnosing
SETTLE = int(os.environ.get("SETTLE", "240"))      # seconds of clean stack before the next case
PROM = os.environ.get("PROMETHEUS", "http://localhost:9091")

# Pre-registered: written before the first run (2026-11-09) and not changed after it.
CASES = [
    dict(id="healthy-before", fault="none: the stack as it is", expected=[], acceptable=[]),
    dict(id="upf-urllc-stopped", fault="docker stop o5gs-upf-urllc",
         expected=["pfcp-association-loss"], acceptable=["target-down", "rlf-no-data-path"]),
    dict(id="gnb-mmtc-stopped", fault="docker stop o5gs-gnb-mmtc",
         expected=["gnb-n2-down"], acceptable=["target-down", "rlf-no-data-path", "registration-failure"]),
    dict(id="pcf-stopped", fault="docker stop o5gs-pcf", expected=["target-down"], acceptable=[]),
    dict(id="upf-urllc-cpu-hog", fault="two busy loops inside o5gs-upf-urllc",
         expected=["cpu-saturation"], acceptable=[]),
    dict(id="n6-delay-urllc", fault="netem +15 ms on o5gs-upf-urllc's packets to the URLLC data network "
         "(no rule describes this)", expected=[], acceptable=[]),
    dict(id="healthy-after", fault="none: the stack after all faults were undone", expected=[], acceptable=[]),
]

# NOT held-out: written after the first run (20261109-075900Z) had been scored, to check two changes
# made because of it (the gNB -> RLF causal link; live per-container CPU) and to inject a CPU fault
# that actually starves the UPF (the first, two busy loops on a 12-core host with no quota, harmed
# nothing: URLLC p95 fell). Reported separately from the held-out score.
POSTHOC = [
    dict(id="gnb-mmtc-stopped", fault="docker stop o5gs-gnb-mmtc (re-run after the causal-link fix)",
         expected=["gnb-n2-down"], acceptable=["target-down", "rlf-no-data-path", "registration-failure"]),
    dict(id="upf-urllc-cpu-starved", fault="o5gs-upf-urllc limited to 0.5 CPU (docker update) plus two busy loops in it",
         expected=["cpu-saturation"], acceptable=[]),
]


def sh(cmd, timeout=120, check=False):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError("%s: %s" % (cmd, r.stderr.strip()))
    return r.stdout.strip()


def pid(container):
    return sh(["docker", "inspect", "-f", "{{.State.Pid}}", container], check=True)


def ip_of(container):
    return sh(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container]).split()[0]


def in_netns(container, args):
    """Run a command in a container's network namespace, through the privileged scheduler container
    (host PID namespace, has tc and nsenter), as the scheduler experiments do."""
    return sh(["docker", "exec", "o5gs-scheduler", "nsenter", "-t", pid(container), "-n"] + args, check=True)


def urllc_p95():
    q = urllib.parse.urlencode({"query": 'max(kpi_owd_recent_ms{slice="URLLC",stat="p95"})'})
    try:
        with urllib.request.urlopen(PROM + "/api/v1/query?" + q, timeout=10) as r:
            res = json.loads(r.read())["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except (OSError, ValueError, KeyError, IndexError):
        return None


# ------------------------------------------------------------------ inject / undo

def inject(case):
    c = case["id"]
    if c in ("healthy-before", "healthy-after"):
        return {}
    if c == "upf-urllc-stopped":
        sh(["docker", "stop", "o5gs-upf-urllc"], check=True)
    elif c == "gnb-mmtc-stopped":
        sh(["docker", "stop", "o5gs-gnb-mmtc"], check=True)
    elif c == "pcf-stopped":
        sh(["docker", "stop", "o5gs-pcf"], check=True)
    elif c in ("upf-urllc-cpu-hog", "upf-urllc-cpu-starved"):
        if c == "upf-urllc-cpu-starved":
            sh(["docker", "update", "--cpus", "0.5", "o5gs-upf-urllc"], check=True)
        for _ in range(2):
            sh(["docker", "exec", "-d", "o5gs-upf-urllc", "timeout", str(HOLD + 60), "sh", "-c", "while :; do :; done"],
               check=True)
    elif c == "n6-delay-urllc":
        dn = ip_of("o5gs-dn-urllc")
        dev = in_netns("o5gs-upf-urllc", ["ip", "route", "get", dn]).split(" dev ")[1].split()[0]
        in_netns("o5gs-upf-urllc", ["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "prio", "bands", "4"])
        in_netns("o5gs-upf-urllc", ["tc", "qdisc", "add", "dev", dev, "parent", "1:4", "handle", "40:", "netem", "delay", "15ms"])
        in_netns("o5gs-upf-urllc", ["tc", "filter", "add", "dev", dev, "parent", "1:0", "protocol", "ip", "prio", "1",
                                    "u32", "match", "ip", "dst", dn + "/32", "flowid", "1:4"])
        return {"dev": dev, "dn": dn}
    return {}


def undo(case, state):
    c = case["id"]
    if c == "upf-urllc-stopped":
        sh(["docker", "start", "o5gs-upf-urllc"], check=True)
    elif c == "gnb-mmtc-stopped":
        sh(["docker", "start", "o5gs-gnb-mmtc"], check=True)
    elif c == "pcf-stopped":
        sh(["docker", "start", "o5gs-pcf"], check=True)
    elif c in ("upf-urllc-cpu-hog", "upf-urllc-cpu-starved"):
        # The UPF image has no ps/awk: kill the loops from the scheduler container (host PID namespace).
        killer = ("import os,signal\n"
                  "me=os.getpid(); n=0\n"
                  "for p in os.listdir('/proc'):\n"
                  "  if not p.isdigit() or int(p)==me: continue\n"
                  "  try:\n"
                  "    if b'while :; do :; done' in open('/proc/%s/cmdline'%p,'rb').read(): os.kill(int(p),signal.SIGKILL); n+=1\n"
                  "  except OSError: pass\n"
                  "print(n)\n")
        state["killed"] = sh(["docker", "exec", "o5gs-scheduler", "python3", "-c", killer])
        if c == "upf-urllc-cpu-starved":
            # "unlimited" is a quota of every host core: `docker update --cpus 0` is a silent no-op
            sh(["docker", "update", "--cpus", str(os.cpu_count() or 12), "o5gs-upf-urllc"], check=True)
    elif c == "n6-delay-urllc":
        in_netns("o5gs-upf-urllc", ["tc", "qdisc", "del", "dev", state["dev"], "root"])


def heal(log):
    """Back to 100 UEs with a data path each (130 sessions); re-attach if not."""
    time.sleep(20)
    broken = campaign.broken_ues()
    if broken:
        log("  %d UEs without a data path after undo: re-attaching" % len(broken))
        subprocess.run(["bash", os.path.join(REPO, "scripts", "open5gs", "start_ues.sh")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        broken = campaign.broken_ues()
    return broken


# ------------------------------------------------------------------ scoring

def score(case, result):
    causes = result.get("causes", [])
    ids = [c["id"] for c in causes]
    exp, acc = set(case["expected"]), set(case["acceptable"])
    if exp:
        top1 = bool(ids) and ids[0] in exp
        found = sorted(exp & set(ids))
        false_pos = [i for i in ids if i not in exp | acc]
    else:
        top1 = None
        found = []
        false_pos = [c["id"] for c in causes if c.get("severity") in ("critical", "warning") and c["id"] not in acc]
    return {"reported": [(c["id"], c.get("severity"), round(c.get("score", 0), 2), c.get("slices")) for c in causes],
            "healthy_verdict": result.get("healthy"), "top1": top1, "expected_found": found,
            "expected_missed": sorted(exp - set(ids)), "false_positives": false_pos}


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    posthoc = "--posthoc" in argv
    argv = [a for a in argv if a != "--posthoc"]
    cases = [c for c in (POSTHOC if posthoc else CASES) if not argv or c["id"] in argv]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%SZ") + ("-posthoc" if posthoc else "")
    out = os.path.join(REPO, "docs", "evidence", "open5gs-diagnosis-heldout", ts)
    os.makedirs(out, exist_ok=True)
    logf = open(os.path.join(out, "run.log"), "a", encoding="utf-8")

    def log(msg):
        line = "%s %s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%SZ"), msg)
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    commit = sh(["git", "-C", REPO, "rev-parse", "--short", "HEAD"])
    log("%s diagnosis test, commit %s, hold %ds, settle %ds"
        % ("POST-HOC (not held-out)" if posthoc else "held-out", commit, HOLD, SETTLE))
    rows = []
    for case in cases:
        log("== %s: %s (expected %s, acceptable %s)" % (case["id"], case["fault"], case["expected"], case["acceptable"]))
        p95_before = urllc_p95()
        state = inject(case)
        t0 = time.time()
        time.sleep(max(0, HOLD - 30))
        p95_during = urllc_p95()
        time.sleep(max(0, HOLD - (time.time() - t0)))
        dj = os.path.join(out, "%s.diagnosis.json" % case["id"])
        r = subprocess.run([sys.executable, os.path.join(HERE, "diagnose.py"), "--last", "%ds" % HOLD, "--json", dj],
                           capture_output=True, text=True, timeout=600)
        with open(os.path.join(out, "%s.report.txt" % case["id"]), "w", encoding="utf-8") as f:
            f.write(r.stdout)
        try:
            undo(case, state)
        finally:
            broken = heal(log)
        result = json.load(open(dj, encoding="utf-8")) if os.path.exists(dj) else {"causes": [], "error": r.stderr[-500:]}
        row = dict(case, **score(case, result), urllc_p95_before=p95_before, urllc_p95_during=p95_during,
                   broken_after_heal=len(broken), injected=state)
        rows.append(row)
        log("   reported %s | top1 %s | missed %s | false positives %s | URLLC p95 %s -> %s ms"
            % (row["reported"], row["top1"], row["expected_missed"], row["false_positives"], p95_before, p95_during))
        json.dump(rows, open(os.path.join(out, "results.json"), "w", encoding="utf-8"), indent=1, default=str)
        if case is not cases[-1]:
            time.sleep(SETTLE)
    write_summary(rows, out, commit)
    log("done: %s" % out)
    return 0


def write_summary(rows, out, commit):
    faulty = [r for r in rows if r["expected"]]
    clean = [r for r in rows if not r["expected"]]
    L = ["# Held-out fault diagnosis, commit %s" % commit, "",
         "| Case | Fault | Expected | Reported (id, severity, score) | Top-1 right | False positives | URLLC p95 before → during |",
         "|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append("| %s | %s | %s | %s | %s | %s | %s → %s ms |" % (
            r["id"], r["fault"], ", ".join(r["expected"]) or "none",
            "; ".join("%s %s %.2f" % (i, s, sc) for i, s, sc, _ in r["reported"]) or "nothing",
            {True: "yes", False: "**no**", None: "—"}[r["top1"]], ", ".join(r["false_positives"]) or "0",
            r["urllc_p95_before"], r["urllc_p95_during"]))
    L += ["", "Faulty cases: top-1 right %d of %d; expected causes found %d of %d."
          % (sum(1 for r in faulty if r["top1"]), len(faulty), sum(len(r["expected_found"]) for r in faulty),
             sum(len(r["expected"]) for r in faulty)),
          "Cases with no expected cause (healthy and uncovered): %d of %d with no critical/warning cause."
          % (sum(1 for r in clean if not r["false_positives"]), len(clean)),
          "False positives in all: %d." % sum(len(r["false_positives"]) for r in rows)]
    open(os.path.join(out, "summary.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
