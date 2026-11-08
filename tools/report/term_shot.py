#!/usr/bin/env python3
"""
term_shot.py — a terminal-style PNG of a command's REAL output, for the project report.

  python3 tools/report/term_shot.py <out.png> "<command>" [--title T] [--max-lines N] [--cwd DIR]

The command is run (bash -lc) and its stdout+stderr are captured verbatim; nothing is typed in by
hand. The output is laid out as an Ubuntu terminal (prompt, then output) in HTML and screenshotted
with headless Chromium in a throwaway container, the same way capture_screenshots.sh takes the
Grafana screenshots. Long output can be cut to the last --max-lines lines (marked in the image).
A sidecar <out>.txt keeps the exact captured text next to the image.
"""

import argparse
import html
import os
import re
import subprocess
import sys

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
IMAGE = os.environ.get("CHROME_IMAGE", "zenika/alpine-chrome:latest")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("command")
    ap.add_argument("--title", default="rithvik@5g-lab: ~/5g-observability")
    ap.add_argument("--max-lines", type=int, default=40)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--width", type=int, default=1100)
    ap.add_argument("--show", default=None, help="command text to show at the prompt (default: the command)")
    a = ap.parse_args()

    r = subprocess.run(["bash", "-lc", a.command], cwd=a.cwd, capture_output=True, text=True, timeout=900)
    text = ANSI.sub("", (r.stdout + r.stderr)).replace("\t", "    ").rstrip("\n")
    lines = [l for l in text.splitlines() if "screen size is bogus" not in l]
    cut = len(lines) > a.max_lines
    if cut:
        lines = ["... (%d earlier lines not shown)" % (len(lines) - a.max_lines)] + lines[-a.max_lines:]
    open(os.path.splitext(a.out)[0] + ".txt", "w").write("$ %s\n%s\n" % (a.show or a.command, "\n".join(lines)))

    body = "\n".join(html.escape(l) for l in lines)
    shown = html.escape(a.show or a.command)
    page = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:#2c001e;}
.bar{background:#3b3b3b;color:#e6e6e6;font:13px 'DejaVu Sans',sans-serif;padding:6px 12px;}
.dots span{display:inline-block;width:11px;height:11px;border-radius:50%%;margin-right:6px;}
pre{margin:0;padding:10px 14px 14px;color:#f2f2f2;font:13.5px/1.35 'DejaVu Sans Mono',monospace;white-space:pre-wrap;word-break:break-all;}
.p{color:#8ae234;font-weight:bold}.d{color:#729fcf;font-weight:bold}
</style></head><body><div class="bar"><span class="dots"><span style="background:#e95420"></span><span style="background:#888"></span><span style="background:#888"></span></span>%s</div>
<pre><span class="p">rithvik@5g-lab</span>:<span class="d">~/5g-observability</span>$ %s
%s</pre></body></html>""" % (html.escape(a.title), shown, body)

    outdir = os.path.dirname(os.path.abspath(a.out))
    name = os.path.basename(a.out)
    htmlfile = os.path.join(outdir, "." + name + ".html")
    open(htmlfile, "w").write(page)
    # One row ~18.2 px; long rows wrap, so estimate from width (about 118 columns per row).
    rows = 2 + sum(max(1, -(-len(l) // 118)) for l in lines)
    height = int(46 + rows * 18.3)
    subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/out" % outdir,
                    "--entrypoint", "chromium-browser", IMAGE, "--headless", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--window-size=%d,%d" % (a.width, height),
                    "--screenshot=/out/%s" % name, "file:///out/.%s.html" % name],
                   capture_output=True, timeout=120)
    os.remove(htmlfile)
    ok = os.path.exists(a.out) and os.path.getsize(a.out) > 0
    print("  [%s] %-46s %d lines%s" % ("OK" if ok else "FAIL", name, len(lines), " (cut)" if cut else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
