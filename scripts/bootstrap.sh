#!/usr/bin/env bash
# bootstrap.sh — materialize pinned upstream dependencies into external/.
# Milestone 0 deliverable D4. Contract: MILESTONE_0_BRIEF.md section 6.
#
# Reads manifest.lock (repo root), clones/fetches each upstream into its
# `dest`, checks out the exact pinned commit SHA, optionally recurses
# submodules, then VERIFIES that HEAD equals the pin. Writes only under
# external/. Never commits anything.
#
# Modes:
#   (default)       fetch + checkout + verify
#   --clean         remove external/ entirely, then fetch fresh
#   --verify-only   do NOT fetch; assert existing external/ matches manifest.lock
#                   (this is the CI / integrity gate; needs no network)
#
# Exit: 0 on "BOOTSTRAP: PASS", non-zero on "BOOTSTRAP: FAIL" or usage/setup error.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
MANIFEST="$REPO_ROOT/manifest.lock"
EXTERNAL_DIR="$REPO_ROOT/external"

MODE="default"
case "${1:-}" in
  "")            MODE="default" ;;
  --clean)       MODE="clean" ;;
  --verify-only) MODE="verify-only" ;;
  -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
  *) echo "ERROR: unknown argument '$1' (use --clean or --verify-only)" >&2; exit 2 ;;
esac

fail() { echo "  [FAIL] $*"; }
ok()   { echo "  [ OK ] $*"; }
info() { echo "  [INFO] $*"; }

command -v git >/dev/null 2>&1 || { echo "ERROR: git not found in PATH" >&2; exit 2; }
command -v jq  >/dev/null 2>&1 || { echo "ERROR: jq not found in PATH (required to parse manifest.lock)" >&2; exit 2; }
[ -f "$MANIFEST" ] || { echo "ERROR: manifest.lock not found at $MANIFEST" >&2; exit 2; }

# Safety: only ever operate on <repo>/external.
if [ "$EXTERNAL_DIR" != "$REPO_ROOT/external" ]; then
  echo "ERROR: refusing to operate on unexpected external dir: $EXTERNAL_DIR" >&2
  exit 2
fi

if [ "$MODE" = "clean" ]; then
  info "--clean: removing $EXTERNAL_DIR"
  rm -rf "$EXTERNAL_DIR"
fi
[ "$MODE" = "verify-only" ] || mkdir -p "$EXTERNAL_DIR"

# verify a materialized dependency: HEAD == pin, and submodules in sync.
verify_dep() {
  local dest="$1" commit="$2" recurse="$3" name="$4"
  if [ ! -d "$dest/.git" ]; then
    fail "$name: '$dest' is not a git checkout (run bootstrap without --verify-only)"
    return 1
  fi
  local head
  head="$(git -C "$dest" rev-parse HEAD 2>/dev/null || echo UNKNOWN)"
  if [ "$head" != "$commit" ]; then
    fail "$name: HEAD $head != pinned $commit"
    return 1
  fi
  ok "$name: HEAD matches pin ($commit)"
  if [ "$recurse" = "true" ]; then
    # In `git submodule status`, a leading '+' (checked-out differs from the
    # superproject gitlink) or '-' (not initialized) indicates drift.
    local bad
    bad="$(git -C "$dest" submodule status --recursive 2>/dev/null | grep -E '^[+-]' || true)"
    if [ -n "$bad" ]; then
      fail "$name: submodule state does not match superproject:"
      echo "$bad" | sed 's/^/         /'
      return 1
    fi
    ok "$name: submodules in sync with superproject"
  fi
  return 0
}

# clone-or-fetch, then checkout the pin, then submodules.
materialize_dep() {
  local url="$1" dest="$2" commit="$3" recurse="$4" name="$5"
  if [ ! -d "$dest/.git" ]; then
    info "$name: cloning $url"
    if ! git clone "$url" "$dest"; then
      fail "$name: clone failed from $url (network? URL moved?)"
      return 1
    fi
  else
    info "$name: fetching updates from origin"
    if ! git -C "$dest" fetch --tags --force origin; then
      fail "$name: fetch failed from $url (network? URL moved?)"
      return 1
    fi
  fi
  if ! git -C "$dest" -c advice.detachedHead=false checkout --quiet "$commit"; then
    fail "$name: checkout of $commit failed"
    return 1
  fi
  if [ "$recurse" = "true" ]; then
    if ! git -C "$dest" submodule update --init --recursive; then
      fail "$name: submodule update failed"
      return 1
    fi
  fi
  return 0
}

echo "==================================================================="
echo "bootstrap.sh   mode=$MODE"
echo "repo root : $REPO_ROOT"
echo "manifest  : $MANIFEST"
echo "external  : $EXTERNAL_DIR"
echo "==================================================================="

overall=0
count="$(jq '.dependencies | length' "$MANIFEST")"
i=0
while [ "$i" -lt "$count" ]; do
  name="$(jq -r ".dependencies[$i].name" "$MANIFEST")"
  url="$(jq -r ".dependencies[$i].url" "$MANIFEST")"
  commit="$(jq -r ".dependencies[$i].commit" "$MANIFEST")"
  destrel="$(jq -r ".dependencies[$i].dest" "$MANIFEST")"
  recurse="$(jq -r ".dependencies[$i].recurse_submodules" "$MANIFEST")"
  dest="$REPO_ROOT/$destrel"

  echo
  echo "--- dependency: $name ($url @ ${commit:0:12}) ---"

  if ! printf '%s' "$commit" | grep -qE '^[0-9a-f]{40}$'; then
    fail "$name: pinned commit is not a 40-char SHA: '$commit'"
    overall=1; i=$((i+1)); continue
  fi

  if [ "$MODE" = "verify-only" ]; then
    verify_dep "$dest" "$commit" "$recurse" "$name" || overall=1
  else
    if materialize_dep "$url" "$dest" "$commit" "$recurse" "$name"; then
      verify_dep "$dest" "$commit" "$recurse" "$name" || overall=1
    else
      overall=1
    fi
  fi
  i=$((i+1))
done

echo
echo "==================================================================="
if [ "$overall" -eq 0 ]; then
  echo "BOOTSTRAP: PASS"
  exit 0
else
  echo "BOOTSTRAP: FAIL"
  exit 1
fi
