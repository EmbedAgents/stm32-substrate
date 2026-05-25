#!/usr/bin/env bash
# capture-banner.sh — snapshot the attached board's CubeProgrammer connect
# banner into the real-board parser-fixture set. Works on ANY attached board
# (no buildable project needed) — the way to catalog boards we can't build.
#
# Usage:
#   tools/capture-banner.sh <slug>
#
# <slug> names the fixture (e.g. nucleo-l053r8). Older on-board ST-LINKs
# report "Board : --" in the connect banner, so a slug is required rather
# than auto-derived. The connect banner (stdout of
# `STM32_Programmer_CLI -c port=swd sn=<sn>`) is saved — ANSI-stripped, CRs
# normalised — to tests/fixtures/cubeprogrammer/banners/realboards/<slug>.txt,
# the same clean format parse_banner() consumes, so the capture doubles as a
# regression fixture (see tests/test_cubeprogrammer_realboard_banners.py).
#
# Requires STM32_PROGRAMMER_CLI on PATH or set in the environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/tests/fixtures/cubeprogrammer/banners/realboards"
CLI="${STM32_PROGRAMMER_CLI:-STM32_Programmer_CLI}"

slug="${1:-}"
if [ -z "$slug" ]; then
  echo "usage: capture-banner.sh <slug>   (e.g. nucleo-l053r8)" >&2
  exit 2
fi

# Strip ANSI CSI sequences + trailing CRs — CubeProgrammer 2.22 colours its
# output even when stdout is a file.
strip_ansi() { sed -E $'s/\x1b\\[[0-9;]*[A-Za-z]//g; s/\r$//'; }

# SN of the first attached probe, from the (ANSI-laden) probe list.
sn="$("$CLI" -l 2>/dev/null | strip_ansi | grep -oiE 'ST-LINK SN *: *[A-F0-9]{16,}' | head -1 | grep -oiE '[A-F0-9]{16,}' || true)"
if [ -z "$sn" ]; then
  echo "capture-banner: no ST-LINK probe detected (is a board attached?)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
out="$OUT_DIR/$slug.txt"
"$CLI" -c port=swd "sn=$sn" 2>&1 | strip_ansi > "$out"

echo "captured: $out  (sn=$sn)"
grep -iE 'STM32CubeProgrammer v|Device (ID|name|CPU)|Flash size|NVM size|Board' "$out" | sed 's/^/  /'
