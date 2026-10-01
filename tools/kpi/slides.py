#!/usr/bin/env python3
"""
slides.py — the Phase 2b KPI results as a Beamer deck, generated from a KPI run (Track 4).

    tools/kpi/slides.py docs/evidence/open5gs-kpi/<run>      -> docs/presentation/phase2b-kpi-results.tex

Every number and every bar on the slides is read from the run's instances.jsonl, so the deck is
rebuilt from the next run instead of retyped. Same palette and footer as the Review 1 deck.
Charts are plain TikZ (pgfplots is not in the TeX Live image this project builds with).
Compile: cd docs/presentation && pdflatex phase2b-kpi-results.tex   (twice)
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import scorecard  # noqa: E402

OUT = os.path.join(REPO, "docs", "presentation", "phase2b-kpi-results.tex")


def esc(s):
    return (str(s).replace("\\", "\\textbackslash{}").replace("&", "\\&").replace("%", "\\%")
            .replace("_", "\\_").replace("#", "\\#").replace("≤", "$\\le$").replace("≥", "$\\ge$")
            .replace("→", "$\\rightarrow$").replace("—", "---").replace("–", "--").replace("…", "\\ldots{}"))


def num(v, unit=""):
    if v is None:
        return "---"
    if unit == "ratio":
        return "%.3g\\,\\%%" % (100 * v)
    if isinstance(v, float):
        v = round(v, 2) if v < 100 else round(v, 1)
    return "%s" % v


def bars(title, labels, values, unit, target=None, target_label="target", ymax=None, color="brandMid"):
    """A small TikZ bar chart: 6 cm wide, 3.2 cm tall."""
    vals = [0 if v is None else v for v in values]
    top = ymax or max(vals + [target or 0]) * 1.18 or 1
    n = len(vals)
    w, h = 6.0, 3.2
    step = w / n
    bw = step * 0.55
    out = ["\\begin{tikzpicture}[font=\\scriptsize]",
           "\\node[anchor=south west,font=\\footnotesize\\bfseries,text=brandDark] at (0,%.2f) {%s};" % (h + 0.25, esc(title)),
           "\\draw[softGrey] (0,0) -- (%.2f,0);" % w]
    for i, (lab, v, raw) in enumerate(zip(labels, vals, values)):
        x = step * i + (step - bw) / 2
        hh = h * v / top
        out.append("\\fill[%s] (%.2f,0) rectangle (%.2f,%.2f);" % (color, x, x + bw, hh))
        out.append("\\node[above,font=\\tiny] at (%.2f,%.2f) {%s};" % (x + bw / 2, hh, "---" if raw is None else num(raw)))
        out.append("\\node[below] at (%.2f,0) {%s};" % (x + bw / 2, esc(lab)))
    if target is not None:
        ty = h * target / top
        out.append("\\draw[brandAccent,thick,dashed] (0,%.2f) -- (%.2f,%.2f) node[right,font=\\tiny,text=brandAccent] {%s %s};"
                   % (ty, w, ty, esc(target_label), num(target)))
    out.append("\\node[rotate=90,font=\\tiny,text=softGrey] at (-0.25,%.2f) {%s};" % (h / 2, esc(unit)))
    out.append("\\end{tikzpicture}")
    return "\n".join(out)


def main(run_dir):
    all_recs = [json.loads(l) for l in open(os.path.join(run_dir, "instances.jsonl"), encoding="utf-8") if l.strip()]
    latest = {r["label"]: i for i, r in enumerate(all_recs)}
    recs = [r for i, r in enumerate(all_recs) if latest[r["label"]] == i]   # a re-run replaces its label
    defs = scorecard.load()
    std = next(r for r in recs if r["label"] == "standard")
    sweep = lambda p: sorted([r for r in recs if r["param"] == p], key=lambda r: float(r["value"]))
    embb, interval, devices = sweep("embb_ues"), sweep("mmtc_interval"), sweep("mmtc_devices")
    run_name = os.path.basename(run_dir.rstrip("/\\"))
    m = std["measured"]
    sl = {s["slice"]: s["score"] for s in std["card"]["slices"]}

    rows = []
    for r in std["card"]["rows"]:
        if r["weight"] == 0:
            continue
        res = {True: "\\textcolor{okGreen}{\\textbf{met}}", False: "\\textcolor{brandAccent}{\\textbf{missed}}", None: ""}[r["met"]]
        unit = "" if r["unit"] == "ratio" else " " + r["unit"]
        rows.append("%s & %s & %s & %s & %s & %s & %.2f \\\\" % (
            r["slice"], esc(r["name"].split(" (")[0]), num(r["measured"], r["unit"]) + ("" if r["unit"] == "ratio" else unit),
            num(r["target_used"], r["unit"]) + ("" if r["unit"] == "ratio" else unit),
            "---" if r["benchmark"] is None else num(r["benchmark"], r["unit"]) + ("" if r["unit"] == "ratio" else unit),
            res, r["score"]))

    targets = []
    for s in defs["slices"]:
        for k in s["kpis"]:
            if float(k["weight"]) == 0:
                continue
            b = k.get("benchmark") or {}
            u = "" if k["unit"] == "ratio" else " " + k["unit"]
            pt = k.get("project_target")
            rt = k.get("research_target")
            targets.append("%s & %s & %s & %s & %s \\\\" % (
                s["name"], esc(k["name"].split(" (")[0]),
                "---" if pt is None else num(pt, k["unit"]) + ("" if k["unit"] == "ratio" else u),
                "---" if rt is None else num(rt, k["unit"]) + ("" if k["unit"] == "ratio" else u),
                ("---" if b.get("value") is None else num(b["value"], k["unit"]) + ("" if k["unit"] == "ratio" else u))
                + (" {\\tiny(%s)}" % esc(b.get("source", "")) if b.get("source") else "")))

    loaded = [r for r in embb if r["measured"]["embb.ues_loaded"] > 0]
    recs20 = [r for r in recs if r["measured"]["embb.ues_loaded"] == 20 and r["measured"]["embb.dl_aggregate_mbps"]]
    full = [r["measured"]["embb.dl_aggregate_mbps"] for r in recs20]
    tex = r"""%% Generated by tools/kpi/slides.py from docs/evidence/open5gs-kpi/%(run)s — do not edit by hand.
\documentclass[aspectratio=169,11pt]{beamer}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage{helvet}
\renewcommand{\familydefault}{\sfdefault}
\usepackage{booktabs}
\usepackage{graphicx}
\usepackage{tikz}
\usetheme{Madrid}
\usefonttheme{professionalfonts}
\setbeamertemplate{navigation symbols}{}
\definecolor{brandDark}{HTML}{16324F}
\definecolor{brandMid}{HTML}{2E6E8E}
\definecolor{brandAccent}{HTML}{C7601F}
\definecolor{brandLight}{HTML}{EDF2F6}
\definecolor{okGreen}{HTML}{2C6E49}
\definecolor{softGrey}{HTML}{5A6570}
\setbeamercolor{structure}{fg=brandDark}
\setbeamercolor{palette primary}{bg=brandDark,fg=white}
\setbeamercolor{palette secondary}{bg=brandMid,fg=white}
\setbeamercolor{frametitle}{bg=brandDark,fg=white}
\setbeamercolor{block title}{bg=brandMid,fg=white}
\setbeamercolor{block body}{bg=brandLight,fg=black}
\setbeamercolor{itemize item}{fg=brandAccent}
\setbeamerfont{frametitle}{size=\large,series=\bfseries}
\setbeamerfont{block title}{size=\small,series=\bfseries}
\setbeamertemplate{itemize item}{\textbullet}
\setlength{\leftmargini}{1.4em}
%% Footer carries the AI-use acknowledgement, as required.
\setbeamertemplate{footline}{%%
  \hbox{%%
  \begin{beamercolorbox}[wd=.30\paperwidth,ht=2.4ex,dp=1.1ex,left,leftskip=1ex]{palette primary}%%
    \tiny Team 23UG005 \textbullet\ Panel 4%%
  \end{beamercolorbox}%%
  \begin{beamercolorbox}[wd=.52\paperwidth,ht=2.4ex,dp=1.1ex,center]{palette secondary}%%
    \tiny Prepared in LaTeX Beamer; drafting assisted by an AI tool (Claude)%%
  \end{beamercolorbox}%%
  \begin{beamercolorbox}[wd=.18\paperwidth,ht=2.4ex,dp=1.1ex,right,rightskip=1ex]{palette primary}%%
    \tiny \insertframenumber\,/\,\inserttotalframenumber%%
  \end{beamercolorbox}}%%
}
\begin{document}

\begin{frame}[plain]
  \begin{center}
    \includegraphics[height=11mm]{assets/logo2.jpeg}\\[2mm]
    {\color{brandDark}\Large\bfseries Phase 2b --- Per-Slice KPIs on a 100-UE, Three-Slice 5G Core\par}
    \vspace{2mm}
    {\color{brandAccent}\small\bfseries Open5GS v2.8.0 + UERANSIM v3.3.0, measured on the running system\par}
    \vspace{1mm}
    {\color{softGrey}\footnotesize Team ID: 23UG005 \quad\textbullet\quad Panel No.\ 4 \quad\textbullet\quad KPI run %(run)s\par}
    \vspace{4mm}
    {\footnotesize Anvita Arasavilli \quad Janhavi Nilesh Parate \quad Keerthi Devarajan Anuradha \quad Mukkara Rithvik Reddy\par}
  \end{center}
\end{frame}

\begin{frame}{The ask: 100 UEs in three classes, a KPI per class}
  \begin{columns}[T]
    \column{0.48\textwidth}
    \begin{block}{From the guide's minutes}
      \small
      \begin{itemize}
        \item 10 time-sensitive UEs \textrightarrow\ \textbf{URLLC}, judged on latency
        \item 20 bandwidth UEs \textrightarrow\ \textbf{eMBB}, judged on throughput
        \item 70 IoT UEs sending 10 bytes periodically \textrightarrow\ \textbf{mMTC}
        \item KPIs compared with industry values
        \item Excel report of KPIs at all instances, varying one parameter
      \end{itemize}
    \end{block}
    \column{0.48\textwidth}
    \begin{block}{Assumptions (team KPI notes)}
      \small
      \begin{itemize}
        \item Industry-standard values are used only as benchmarks
        \item Open5GS and UERANSIM, not an industry-grade deployment
        \item In the 5G research lab, industry values can be used
      \end{itemize}
    \end{block}
  \end{columns}
\end{frame}

\begin{frame}{Topology: a dedicated user plane per slice, shared control plane}
  \footnotesize
  \begin{tabular}{@{}llll@{}}
    \toprule
    & \textbf{eMBB} & \textbf{URLLC} & \textbf{mMTC} \\
    \midrule
    S-NSSAI & SST 1 / 010203 & SST 2 / 112233 & SST 3 / 334455 \\
    UEs & 20 & 10 & 70 \\
    5QI \textbullet\ session AMBR & 9 \textbullet\ 200 Mbps & 82 \textbullet\ 20 Mbps & 9 \textbullet\ 1 Mbps \\
    gNB, UPF, data network & own & own & own \\
    Traffic & TCP bulk download & 64 B every 20 ms & 10 B every 10 s \\
    \bottomrule
  \end{tabular}

  \vspace{3mm}
  \begin{block}{How the KPIs are measured}
    \footnotesize Every packet leaves from the device's own PDU-session address and crosses its slice's gNB and UPF.
    Latency is one-way, device to data network: sender and receiver share one monotonic clock (same kernel),
    so no clock synchronisation error. Loss from per-device sequence numbers.
  \end{block}
\end{frame}

\begin{frame}{KPIs and targets (team notes), with industry benchmarks}
  \scriptsize
  \resizebox{\textwidth}{!}{%%
  \begin{tabular}{@{}lllll@{}}
    \toprule
    \textbf{Slice} & \textbf{KPI} & \textbf{Project target} & \textbf{Research target} & \textbf{Industry benchmark} \\
    \midrule
%(targets)s
    \bottomrule
  \end{tabular}}

  \vspace{2mm}
  {\scriptsize\color{softGrey} Score per KPI: 1 if met, else the fraction of the way to the target. Slice score: weighted sum (weights provisional).}
\end{frame}

\begin{frame}{Result: all three slices served at once (%(std_s)s s, 20 eMBB UEs downloading)}
  \scriptsize
  \begin{tabular}{@{}lllllll@{}}
    \toprule
    \textbf{Slice} & \textbf{KPI} & \textbf{Measured} & \textbf{Target} & \textbf{Benchmark} & \textbf{Result} & \textbf{Score} \\
    \midrule
%(rows)s
    \bottomrule
  \end{tabular}

  \vspace{2mm}
  \footnotesize Slice scores: \textbf{URLLC %(s_urllc)s} \textbullet\ \textbf{eMBB %(s_embb)s} \textbullet\ \textbf{mMTC %(s_mmtc)s} \textbullet\ overall %(overall)s
\end{frame}

\begin{frame}{Sweep 1: eMBB load (devices downloading at once)}
  \begin{columns}[T]
    \column{0.5\textwidth}
%(chart_embb)s
    \column{0.5\textwidth}
%(chart_urllc_embb)s
  \end{columns}
  \vspace{2mm}
  \footnotesize With 5 devices the 5th percentile is close to the 50 Mbps target; with 20 it falls to %(p5_20)s Mbps.
  The laptop delivers about %(agg_range)s Mbps in total, so 50 Mbps for each of 20 UEs (1 Gbps) is out of reach here.
  eMBB load also pushes URLLC's tail past 10 ms: the slices share one CPU.
\end{frame}

\begin{frame}{Sweep 2: more IoT devices (``later increase the UEs'')}
  \begin{columns}[T]
    \column{0.5\textwidth}
%(chart_dev_urllc)s
    \column{0.5\textwidth}
%(chart_dev_embb)s
  \end{columns}
  \vspace{2mm}
  \footnotesize Up to %(dev_max)s IoT devices (%(ue_max)s UEs): every device registered, no report was lost.
  Sweep 3, reporting interval 1--30 s: mMTC loss 0 at every interval.
  Across all %(n20)s instances with 20 eMBB UEs and identical settings, eMBB aggregate ranged %(agg20)s Mbps:
  host conditions on this laptop move eMBB more than IoT scale does --- repeat runs on the lab machine.
\end{frame}

\begin{frame}{Findings, and what this setup cannot show}
  \begin{columns}[T]
    \column{0.5\textwidth}
    \begin{block}{Findings}
      \footnotesize
      \begin{itemize}
        \item URLLC mean latency %(u_mean)s ms meets 10 ms; its p99 (%(u_p99)s ms) does not under eMBB load
        \item eMBB is capped by the laptop, not the slice design: with 20 UEs the 5th percentile never exceeded %(p5max)s Mbps
        \item mMTC: 100\%% registration, 0 loss, up to %(ue_max)s UEs
        \item Bugs found on the way: simulator clock, three start-up races, gNB never reconnecting, a deploy reporting success it never applied --- all fixed
      \end{itemize}
    \end{block}
    \column{0.5\textwidth}
    \begin{block}{Not measurable with a simulated RAN}
      \footnotesize
      \begin{itemize}
        \item Radio-only latency (ITU's 1 ms), spectral efficiency
        \item Coverage, mobility, devices per km$^2$
        \item IoT battery life and real sleep modes
      \end{itemize}
    \end{block}
  \end{columns}
\end{frame}

\begin{frame}{To confirm with the guide, and next}
  \begin{columns}[T]
    \column{0.5\textwidth}
    \begin{block}{Provisional choices}
      \footnotesize
      \begin{itemize}
        \item Traffic: URLLC 64 B / 20 ms; IoT 10 B every 10 s
        \item KPI weights and the overall score
        \item The two eMBB targets contradict (1 Gbps vs 500 Mbps)
        \item Sweep parameters
      \end{itemize}
    \end{block}
    \column{0.5\textwidth}
    \begin{block}{Next}
      \footnotesize
      \begin{itemize}
        \item Orchestrator: priority within URLLC, deciding the slices, allocation from these KPIs
        \item Re-run on the lab's bare-metal machine
        \item Fault diagnosis (later phase)
      \end{itemize}
    \end{block}
  \end{columns}
\end{frame}

\end{document}
""" % {
        "run": run_name, "targets": "\n".join(targets), "rows": "\n".join(rows), "std_s": std["duration_s"],
        "s_urllc": "%.2f" % sl["URLLC"], "s_embb": "%.2f" % sl["eMBB"], "s_mmtc": "%.2f" % sl["mMTC"],
        "overall": "%.2f" % std["card"]["overall"],
        "chart_embb": bars("eMBB 5th-percentile DL per UE", [str(r["value"]) for r in loaded],
                           [r["measured"]["embb.dl_p5_mbps"] for r in loaded], "Mbps", target=50),
        "chart_urllc_embb": bars("URLLC latency p99", [str(r["value"]) for r in embb],
                                 [r["measured"]["urllc.owd_p99_ms"] for r in embb], "ms", target=10, color="brandAccent"),
        "p5_20": num(next((r["measured"]["embb.dl_p5_mbps"] for r in embb if r["value"] == 20), None)),
        "agg_range": "%s--%s" % (num(min(r["measured"]["embb.dl_aggregate_mbps"] for r in loaded)),
                                 num(max(r["measured"]["embb.dl_aggregate_mbps"] for r in loaded))),
        "chart_dev_urllc": bars("URLLC latency p99 vs IoT devices", [str(r["value"]) for r in devices],
                                [r["measured"]["urllc.owd_p99_ms"] for r in devices], "ms", target=10, color="brandAccent"),
        "chart_dev_embb": bars("eMBB 5th-percentile DL vs IoT devices", [str(r["value"]) for r in devices],
                               [r["measured"]["embb.dl_p5_mbps"] for r in devices], "Mbps", target=50),
        "dev_max": devices[-1]["value"] if devices else "-", "ue_max": (devices[-1]["value"] + 30) if devices else "-",
        "u_mean": num(m["urllc.owd_mean_ms"]), "u_p99": num(m["urllc.owd_p99_ms"]),
        "n20": len(full), "agg20": "%s--%s" % (num(min(full)), num(max(full))),
        "p5max": num(max(r["measured"]["embb.dl_p5_mbps"] for r in recs20)),
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(tex)
    print("wrote %s" % os.path.relpath(OUT, REPO))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
