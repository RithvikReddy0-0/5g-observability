#!/usr/bin/env bash
# validate_gtp5g.sh — build + load the gtp5g kernel module against the RUNNING
# kernel and validate it. Milestone 0 deliverable D6 — the central M0 objective.
# Contract: MILESTONE_0_BRIEF.md section 8.
#
# HARD-STOP RULE (brief 8.6): if gtp5g fails to BUILD or LOAD on the ODE kernel,
# this is a BLOCKING ARCHITECTURAL condition. This script writes diagnostics and
# exits non-zero. It MUST NOT autonomously switch kernels, pick a different gtp5g
# version, patch source, or downgrade Ubuntu — those are human decisions that
# re-ratify VERSIONS.lock / ADR-003.
#
# SAFETY: gtp5g build+load is only meaningful on a CONFORMING ODE (bare-metal
# Ubuntu 24.04). This script REFUSES to run on WSL2 / non-baseline kernels unless
# ALLOW_NON_BASELINE=1 is exported — this is deliberate, per SPEC ADR-003 which
# excludes WSL2 from the baseline.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
GTP5G_DIR="$REPO_ROOT/external/gtp5g"
VERSIONS_LOCK="$REPO_ROOT/VERSIONS.lock"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
BUILD_LOG="$LOG_DIR/gtp5g-build.log"
DMESG_LOG="$LOG_DIR/gtp5g-dmesg.log"

KREL="$(uname -r)"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== validate_gtp5g.sh =="
echo "kernel     : $KREL"
echo "gtp5g dir  : $GTP5G_DIR"
echo "build log  : $BUILD_LOG"

# --- Safety guard: refuse on WSL / non-baseline unless explicitly overridden ---
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  if [ "${ALLOW_NON_BASELINE:-0}" != "1" ]; then
    cat <<EOF

REFUSING TO RUN: WSL/non-baseline kernel detected ($KREL).
gtp5g build+load MUST be validated on a conforming bare-metal Ubuntu 24.04 ODE.
Per SPEC ADR-003 / brief section 8, WSL2 is excluded from the baseline and its
kernel is not representative of the ODE. This is by design, not an error.

(Override ONLY if you fully understand the consequences: ALLOW_NON_BASELINE=1)
EOF
    exit 3
  fi
  echo "WARNING: ALLOW_NON_BASELINE=1 set — proceeding on a non-baseline kernel."
fi

[ -d "$GTP5G_DIR/.git" ] || { echo "ERROR: $GTP5G_DIR missing — run scripts/bootstrap.sh first."; exit 2; }
GTP5G_SHA="$(git -C "$GTP5G_DIR" rev-parse HEAD 2>/dev/null || echo UNKNOWN)"
echo "gtp5g SHA  : $GTP5G_SHA"

# --- Preconditions: kernel headers for the RUNNING kernel + build toolchain ---
echo "== ensuring linux-headers-$KREL + build toolchain =="
if ! dpkg -s "linux-headers-$KREL" >/dev/null 2>&1; then
  echo "installing linux-headers-$KREL build-essential (sudo apt) ..."
  sudo apt-get update -y
  if ! sudo apt-get install -y "linux-headers-$KREL" build-essential make gcc; then
    echo "ERROR: failed to install kernel headers/toolchain for $KREL"; exit 2
  fi
fi

# --- Build ---
echo "== building gtp5g =="
make -C "$GTP5G_DIR" clean >/dev/null 2>&1 || true
if ! make -C "$GTP5G_DIR" >"$BUILD_LOG" 2>&1; then
  echo "BUILD FAILED — see $BUILD_LOG"
  echo
  echo "### HARD STOP (brief 8.6): gtp5g failed to BUILD on kernel $KREL."
  echo "### Do NOT auto-switch kernel or gtp5g version. Escalate for a human decision."
  echo "----- last 40 lines of build log -----"
  tail -40 "$BUILD_LOG" 2>/dev/null || true
  exit 1
fi
echo "build OK"

# --- Load ---
echo "== loading module =="
sudo dmesg -C 2>/dev/null || true
sudo make -C "$GTP5G_DIR" install >>"$BUILD_LOG" 2>&1 || echo "note: 'make install' returned non-zero; will try modprobe/insmod"
if ! sudo modprobe gtp5g 2>/dev/null; then
  KO="$(find "$GTP5G_DIR" -name 'gtp5g.ko' 2>/dev/null | head -1)"
  [ -n "$KO" ] && sudo insmod "$KO" 2>/dev/null || true
fi
sudo dmesg > "$DMESG_LOG" 2>/dev/null || true

# --- Validate (all must hold) ---
echo "== validating =="
OK=1
if lsmod | grep -q '^gtp5g'; then echo "  [ OK ] lsmod shows gtp5g"; else echo "  [FAIL] gtp5g not present in lsmod"; OK=0; fi
if grep -qiE 'gtp5g.*(error|fail|taint)' "$DMESG_LOG" 2>/dev/null; then echo "  [FAIL] dmesg shows gtp5g error/taint"; OK=0; else echo "  [ OK ] no gtp5g error/taint in dmesg"; fi
GMODVER="$(modinfo gtp5g 2>/dev/null | awk '/^version:/{print $2}')"
if [ -n "${GMODVER:-}" ]; then echo "  [ OK ] modinfo version: $GMODVER"; else echo "  [WARN] modinfo returned no version string"; fi

if [ "$OK" -ne 1 ]; then
  echo
  echo "### HARD STOP (brief 8.6): gtp5g failed to LOAD/validate on kernel $KREL."
  echo "### Diagnostics: $BUILD_LOG , $DMESG_LOG . Escalate for a human decision."
  exit 1
fi

# --- Record empirical results into VERSIONS.lock (if jq present) ---
if command -v jq >/dev/null 2>&1 && [ -f "$VERSIONS_LOCK" ]; then
  tmp="$(mktemp)"
  if jq \
      --arg k "$KREL" --arg sha "$GTP5G_SHA" --arg mv "${GMODVER:-unknown}" --arg ts "$STAMP" '
        .environment.kernel_release = $k
        | .dependencies.gtp5g.commit = $sha
        | .dependencies.gtp5g.module_version = $mv
        | .gtp5g_validation = {built:true, loaded:true, kernel:$k, module_version:$mv, timestamp:$ts}
      ' "$VERSIONS_LOCK" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$VERSIONS_LOCK"
    echo "recorded gtp5g results into $VERSIONS_LOCK"
  else
    rm -f "$tmp"
    echo "note: could not update $VERSIONS_LOCK automatically; record fields manually."
  fi
fi

echo
echo "GTP5G VALIDATION: PASS (kernel=$KREL gtp5g=$GTP5G_SHA module=${GMODVER:-?})"
exit 0
