#!/usr/bin/env bash
# test-release-linux.sh — validate a published stm32-substrate release on Linux,
# no hardware. Bash mirror of tools/test-release-windows.ps1.
#
#   Tier 1 — published install (as a brand-new user would experience it):
#       fresh venv, `pip install git+https://…EmbedAgents…` (pinned to
#       RELEASE_TAG when given), verify the `stm32` console script + version,
#       verify the bundled JSON schemas load.
#   Tier 2 — this checkout:
#       editable `.[dev]` install in a second venv, run the unit suite,
#       `claude plugin validate --strict`.
#
# Prints a PASS/FAIL table and exits non-zero if any executed check failed. The
# interactive bit (the five /stm32* commands registering in a live Claude session)
# and the ST-tools/board end-to-end (Tier 3) can't be automated; printed at the end.
#
# Env knobs: REPO_URL, RELEASE_TAG, WORKDIR, KEEP_WORKDIR=1.
#   RELEASE_TAG (e.g. v0.1.1): Tier 1 installs git+REPO_URL@RELEASE_TAG and asserts
#   `stm32 --version` matches the tag (minus the leading v). Unset: Tier 1 installs
#   the default branch and the expected version is read from this checkout's
#   pyproject.toml (dev-run mode).
#   Usage:  RELEASE_TAG=v0.1.1 tools/test-release-linux.sh
#           (run from a checkout of that tag, so Tier 2 tests the same code)

REPO_URL="${REPO_URL:-https://github.com/EmbedAgents/stm32-substrate.git}"
RELEASE_TAG="${RELEASE_TAG:-}"
WORKDIR="${WORKDIR:-$(mktemp -d -t stm32-release-test.XXXXXX)}"
KEEP_WORKDIR="${KEEP_WORKDIR:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# What to install in Tier 1, and which version string `stm32 --version` must show.
if [ -n "$RELEASE_TAG" ]; then
    PIP_SPEC="git+${REPO_URL}@${RELEASE_TAG}"
    EXPECTED_VER="${RELEASE_TAG#v}"
else
    PIP_SPEC="git+${REPO_URL}"
    EXPECTED_VER="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$REPO_ROOT/pyproject.toml" 2>/dev/null | head -n1)"
fi
SEP=$'\x1f'   # row field separator unlikely to appear in details

if [ -t 1 ]; then GRN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; CYN=$'\e[36m'; GRY=$'\e[90m'; RST=$'\e[0m'
else GRN=; RED=; YEL=; CYN=; GRY=; RST=; fi

fails=0; skips=0; total=0
ROWS=()
record() { # name ok detail
    local name="$1" ok="$2" detail="${3:-}" status
    total=$((total+1))
    if [ "$ok" = "1" ]; then status="PASS"; printf '  %s[PASS]%s %s%s\n' "$GRN" "$RST" "$name" "${detail:+ - $detail}"
    else status="FAIL"; fails=$((fails+1)); printf '  %s[FAIL]%s %s%s\n' "$RED" "$RST" "$name" "${detail:+ - $detail}"; fi
    ROWS+=("${status}${SEP}${name}${SEP}${detail}")
}
skip() { skips=$((skips+1)); ROWS+=("SKIP${SEP}${1}${SEP}${2}"); printf '  %s[SKIP]%s %s - %s\n' "$YEL" "$RST" "$1" "$2"; }
section() { printf '\n%s=== %s ===%s\n' "$CYN" "$1" "$RST"; }
cleanup() { [ "$KEEP_WORKDIR" = "1" ] || rm -rf "$WORKDIR"; }
trap cleanup EXIT

# --- prerequisites -----------------------------------------------------------
section "Prerequisites"
PY=""
for c in python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    record "python 3.11+ available" 0 "no python>=3.11 on PATH"
    printf '\n%sCannot continue without Python 3.11+.%s\n' "$RED" "$RST"; exit 1
fi
record "python 3.11+ available" 1 "$PY ($("$PY" --version 2>&1))"
command -v git    >/dev/null 2>&1 && HAS_GIT=1    || HAS_GIT=0;    record "git available" "$HAS_GIT"
command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1 || HAS_CLAUDE=0

mkdir -p "$WORKDIR"

# --- Tier 1: published install ----------------------------------------------
section "Tier 1 - published install (git+https${RELEASE_TAG:+, tag $RELEASE_TAG})"
if [ "$HAS_GIT" != "1" ]; then
    skip "Tier 1 (all)" "git required for a git+https install"
else
    T1="$WORKDIR/venv-install"
    "$PY" -m venv "$T1"
    T1PY="$T1/bin/python"
    if [ ! -x "$T1PY" ]; then
        record "create install venv" 0 "no python at $T1PY"
    else
        record "create install venv" 1
        "$T1PY" -m pip install -q --upgrade pip >/dev/null 2>&1
        if "$T1PY" -m pip install -q "$PIP_SPEC" >"$WORKDIR/pip1.log" 2>&1; then
            record "pip install $PIP_SPEC" 1
            VER="$("$T1/bin/stm32" --version 2>&1)"
            if [ -n "$EXPECTED_VER" ]; then
                case "$VER" in *"$EXPECTED_VER"*) record "stm32 --version = $EXPECTED_VER" 1 "$VER";;
                               *)                 record "stm32 --version = $EXPECTED_VER" 0 "$VER";; esac
            else
                # No tag given and no readable pyproject — at least require the
                # console script to run and print something version-shaped.
                case "$VER" in *[0-9].[0-9]*) record "stm32 --version runs" 1 "$VER";;
                               *)             record "stm32 --version runs" 0 "$VER";; esac
            fi
            cat >"$WORKDIR/schema_check.py" <<'PY'
import json, importlib.resources as r
p = r.files("stm32_substrate.schemas").joinpath("stm32-project.schema.json")
d = json.loads(p.read_text(encoding="utf-8"))
assert str(d.get("$id", "")).endswith("stm32-project.schema.json"), "unexpected $id"
print("SCHEMA_OK")
PY
            OUT="$("$T1PY" "$WORKDIR/schema_check.py" 2>&1)"
            case "$OUT" in *SCHEMA_OK*) record "bundled schemas load" 1;;
                           *)           record "bundled schemas load" 0 "$OUT";; esac
        else
            record "pip install $PIP_SPEC" 0 "$(tail -n3 "$WORKDIR/pip1.log" | tr '\n' ' ')"
        fi
    fi
fi

# --- Tier 2: this checkout ---------------------------------------------------
section "Tier 2 - this checkout (unit suite + plugin validate)"
if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
    skip "Tier 2 (all)" "run from a clone (no pyproject.toml at $REPO_ROOT)"
else
    T2="$WORKDIR/venv-dev"
    "$PY" -m venv "$T2"
    T2PY="$T2/bin/python"
    if [ ! -x "$T2PY" ]; then
        record "create dev venv" 0
    else
        record "create dev venv" 1
        "$T2PY" -m pip install -q --upgrade pip >/dev/null 2>&1
        if "$T2PY" -m pip install -q -e "$REPO_ROOT[dev]" >"$WORKDIR/pip2.log" 2>&1; then
            record "pip install -e .[dev]" 1
            ( cd "$REPO_ROOT" && "$T2PY" -m pytest -q ) >"$WORKDIR/pytest.log" 2>&1
            CODE=$?
            SUMMARY="$(grep -aE 'passed|failed|error' "$WORKDIR/pytest.log" | tail -n1)"
            if [ "$CODE" -eq 0 ]; then
                record "unit suite (pytest)" 1 "$SUMMARY"
            else
                record "unit suite (pytest)" 0 "$SUMMARY"
                tail -n15 "$WORKDIR/pytest.log" | while IFS= read -r line; do printf '      %s%s%s\n' "$GRY" "$line" "$RST"; done
            fi
        else
            record "pip install -e .[dev]" 0 "$(tail -n3 "$WORKDIR/pip2.log" | tr '\n' ' ')"
        fi
    fi
    if [ "$HAS_CLAUDE" = "1" ]; then
        VALOUT="$(claude plugin validate --strict "$REPO_ROOT" 2>&1)"; VCODE=$?
        record "claude plugin validate --strict" "$([ "$VCODE" -eq 0 ] && echo 1 || echo 0)" \
            "$(printf '%s' "$VALOUT" | grep -i 'Validation' | head -n1)"
    else
        skip "claude plugin validate" "claude CLI not on PATH"
    fi
fi

# --- summary -----------------------------------------------------------------
section "Summary"
printf '%-6s %-34s %s\n' "STATUS" "CHECK" "DETAIL"
for r in "${ROWS[@]}"; do
    IFS="$SEP" read -r st nm dt <<<"$r"
    printf '%-6s %-34s %s\n' "$st" "$nm" "$dt"
done
echo
if [ "$fails" -eq 0 ]; then
    printf '%sRESULT: PASS%s (%d checks, %d skipped)\n' "$GRN" "$RST" "$((total))" "$skips"
else
    printf '%sRESULT: FAIL%s (%d failed, %d skipped)\n' "$RED" "$RST" "$fails" "$skips"
fi
[ "$KEEP_WORKDIR" = "1" ] && printf '%sScratch venvs kept at: %s%s\n' "$GRY" "$WORKDIR" "$RST"

# --- manual steps the script can't automate ----------------------------------
section "Manual steps (not automated)"
cat <<'EOF'
These need a human / hardware and aren't covered above:

  Plugin registration (interactive):
    claude plugin marketplace add EmbedAgents/stm32-substrate
    claude plugin install stm32-substrate@stm32
    # restart Claude Code, type '/', confirm /stm32prog /stm32build /stm32debug
    # /stm32project /stm32agent all appear.

  Tier 3 - ST tools + board (real end-to-end):
    1. cp .claude/stm32-tools.local.jsonc.example .claude/stm32-tools.local.jsonc
       and fill your Linux tool paths (e.g. /opt/st/stm32cubeclt_*/...), or set env
       vars like STM32_PROGRAMMER_CLI.
    2. pytest -m smoke            # vendor CLIs reachable, no board needed
    3. attach a NUCLEO + ST-LINK, then in Claude (in a folder with a
       stm32-project.jsonc):  "list the connected probe"  then  "build it and
       flash my Nucleo."
EOF

[ "$fails" -eq 0 ] && exit 0 || exit 1
