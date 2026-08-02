#!/usr/bin/env bash
# verify_env.sh — assert this machine against the Official Development
# Environment (ODE). Milestone 0 deliverable D2. Contract: MILESTONE_0_BRIEF
# section 9 / SPEC ADR-003.
#
# Runs every check and classifies it PASS / WARN / FAIL, then prints a summary.
# A CONFORMING ODE requires zero FAIL-class checks AND virtualization == none.
# Otherwise a prominent NON-BASELINE banner is printed. Exit 0 only when fully
# conforming; non-zero otherwise.
#
# NOTE: intentionally does NOT use `set -e` — we want every check to run and be
# reported even if earlier ones fail.

set -uo pipefail

PASS=0; WARN=0; FAILN=0
line() { printf '  %-22s : %-5s %s\n' "$1" "$2" "$3"; }
pass() { PASS=$((PASS+1));  line "$1" "PASS" "$2"; }
warn() { WARN=$((WARN+1));  line "$1" "WARN" "$2"; }
failc(){ FAILN=$((FAILN+1)); line "$1" "FAIL" "$2"; }

echo "==================================================================="
echo "verify_env.sh — ODE conformance check (SPEC ADR-003)"
echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==================================================================="

# --- OS = Ubuntu 24.04 LTS ---
OS_ID=""; OS_VER=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-}"; OS_VER="${VERSION_ID:-}"
fi
if [ "$OS_ID" = "ubuntu" ] && [ "$OS_VER" = "24.04" ]; then
  pass "OS" "Ubuntu $OS_VER LTS"
else
  failc "OS" "found '${OS_ID:-unknown} ${OS_VER:-?}', require Ubuntu 24.04"
fi

# --- Arch = x86_64 ---
ARCH="$(uname -m 2>/dev/null || echo unknown)"
if [ "$ARCH" = "x86_64" ]; then pass "Arch" "$ARCH"; else failc "Arch" "found '$ARCH', require x86_64"; fi

# --- Not WSL (HARD) ---
PROCVER="$(cat /proc/version 2>/dev/null || echo '')"
if printf '%s' "$PROCVER" | grep -qiE 'microsoft|wsl'; then
  failc "Not-WSL (hard)" "WSL kernel signature present in /proc/version"
else
  pass "Not-WSL (hard)" "no WSL signature in /proc/version"
fi

# --- Virtualization (none = bare-metal) -> WARN if not none ---
if command -v systemd-detect-virt >/dev/null 2>&1; then
  VIRT="$(systemd-detect-virt 2>/dev/null || echo unknown)"
else
  VIRT="unknown"
fi
if [ "$VIRT" = "none" ]; then
  pass "Virtualization" "none (bare-metal)"
else
  warn "Virtualization" "$VIRT (baseline requires bare-metal)"
fi

# --- CPU threads >= 8 ---
NPROC="$(nproc 2>/dev/null || echo 0)"
if [ "${NPROC:-0}" -ge 8 ] 2>/dev/null; then pass "CPU threads" "$NPROC (>=8)"; else failc "CPU threads" "$NPROC (<8)"; fi

# --- RAM >= 16 GB ---
# Threshold 15,000,000 KiB (~14.3 GiB) tolerates firmware/iGPU-reserved RAM so a
# genuine 16 GB box is not false-failed, while still rejecting small VMs/WSL.
MEM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
MEM_GB=$(( ${MEM_KB:-0} / 1024 / 1024 ))
if [ "${MEM_KB:-0}" -ge 15000000 ] 2>/dev/null; then pass "RAM" "${MEM_GB} GiB (>=16 GB)"; else failc "RAM" "${MEM_GB} GiB (<16 GB)"; fi

# --- Free SSD >= 100 GB on the repo path ---
TARGET="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
AVAIL_KB="$(df -Pk "$TARGET" 2>/dev/null | awk 'NR==2{print $4}')"
AVAIL_KB="${AVAIL_KB:-0}"
AVAIL_GB=$(( AVAIL_KB / 1024 / 1024 ))
if [ "$AVAIL_KB" -ge 100000000 ] 2>/dev/null; then pass "Free disk" "${AVAIL_GB} GiB on $TARGET (>=100 GB)"; else failc "Free disk" "${AVAIL_GB} GiB on $TARGET (<100 GB)"; fi

# --- sudo available ---
if sudo -n true 2>/dev/null; then
  pass "sudo" "passwordless sudo available"
else
  warn "sudo" "could not confirm non-interactive sudo (required on ODE for gtp5g)"
fi

# --- Docker Engine present ---
if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
  DVER="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
  pass "Docker Engine" "$DVER"
else
  failc "Docker Engine" "docker not available or daemon unreachable"
fi

# --- Docker Compose v2 (plugin) ---
if docker compose version >/dev/null 2>&1; then
  CVER="$(docker compose version --short 2>/dev/null || echo '?')"
  pass "Docker Compose v2" "$CVER"
else
  failc "Docker Compose v2" "'docker compose' plugin not available"
fi

echo "-------------------------------------------------------------------"
echo "discovered: kernel=$(uname -r 2>/dev/null) os=${OS_ID:-?}-${OS_VER:-?} arch=${ARCH} nproc=${NPROC} ram=${MEM_GB}GiB virt=${VIRT}"
echo "summary: PASS=$PASS  WARN=$WARN  FAIL=$FAILN"

CONFORMS=1
[ "$FAILN" -gt 0 ] && CONFORMS=0
[ "$VIRT" != "none" ] && CONFORMS=0

if [ "$CONFORMS" -eq 1 ]; then
  echo "VERDICT: CONFORMING ODE"
  exit 0
else
  echo
  echo "###################################################################"
  echo "#  NON-BASELINE ENVIRONMENT                                        #"
  echo "#  This machine does NOT satisfy the ODE (SPEC ADR-003).           #"
  echo "#  File/authoring work is fine here, but the M0 sign-off checklist #"
  echo "#  (build+load gtp5g, freeze VERSIONS.lock) MUST be completed on a #"
  echo "#  conforming bare-metal Ubuntu 24.04 ODE.                         #"
  echo "###################################################################"
  exit 1
fi
