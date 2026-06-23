#!/usr/bin/env bash
# test-release-local-wheel.sh — PyPI-fidelity LOCAL release test for
# embedagents-stm32, on Linux/macOS. Bash mirror of tools/test-release-local-wheel.ps1.
#
# WHY THIS EXISTS (vs. tools/test-release-linux.sh):
#   test-release-linux.sh installs from a git+https spec (needs a published tag)
#   or editable `-e` (runs from source). Neither tests the actual built artifact.
#   Editable installs in particular HIDE packaging bugs: `pip install -e` does not
#   regenerate the console-script shim or dist metadata, so a renamed package can
#   leave a stale `stm32` shim importing a module that no longer exists
#   (ModuleNotFoundError) — exactly what bit us after stm32_substrate→embedagents.stm32.
#   This script builds the REAL wheel from this dev tree (byte-identical to what PyPI
#   would serve) and installs/runs/uninstalls it as an end user would — WITHOUT
#   publishing to PyPI. Use it to validate a staged version (e.g. 0.3.1) over as many
#   rounds as you like before the one-shot, immutable PyPI upload.
#
# INSTALL METHODS (--method, all install the SAME built wheel; they differ in HOW
# the wheel is fetched, i.e. how close to a real `pip install name==version`):
#
#   A. pipx   (DEFAULT)  pipx install ./dist/*.whl
#        + Real on-PATH user UX; matches the README's recommended install (esp. on
#          Windows, where bare `pip install` drops stm32.exe off PATH). `pipx uninstall`
#          guarantees a clean removal — directly prevents the stale-shim bug above.
#        + Same command shape on Linux and Windows (pipx is pure Python).
#        - Runtime deps (jsonschema, pyserial) still come from real PyPI; does not
#          exercise index/metadata resolution of OUR package (pipx is handed a file).
#
#   B. venv   python -m venv + pip install --no-index --find-links dist name==ver
#        + Resolves our package by name+version (more realistic than a file path);
#          fully offline/hermetic (this script pre-downloads the wheel's deps into the
#          dist dir so --no-index works out of the box).
#        - `stm32` is on PATH only while the venv is active; not the global UX.
#
#   C. index  pypiserver over dist/ + pip install -i http://localhost name==ver
#        + Highest fidelity: a true `pip install embedagents-stm32==<ver>` against an
#          index URL — exercises name+version resolution and metadata parsing like PyPI.
#        - Most setup (server lifecycle, port); deps still proxied from real PyPI.
#
# SCOPE: full user experience = pip package (the wheel) + Claude plugin channel.
#   Cons of the plugin half: it needs the external `claude` CLI (skipped if absent),
#   and `claude plugin marketplace add` mutates GLOBAL ~/.claude/plugins/ (not isolated)
#   — always torn down here. Only install→registration-at-<ver>→uninstall is
#   scriptable; actually running /stm32build needs a live Claude session.
#
# FLAGS:
#   --method {pipx|venv|index}   install mechanism (default: pipx)
#   --no-plugin                  skip the Claude plugin channel (default: on)
#   --project <dir>              CubeIDE project dir for the --run-build smoke
#   --run-build                  run `stm32 build <project>` end-to-end (needs --project
#                                + ST tools configured; also asserts the RES-050 workspace
#                                lands OUTSIDE the project tree)
#   --keep                       skip teardown — leave a usable installed CLI + plugin
#   -h | --help                  print this header and exit
#
# Prints a PASS/FAIL table; exits non-zero if any executed check failed.

set -o pipefail

METHOD="pipx"
WITH_PLUGIN=1
PROJECT=""
RUN_BUILD=0
KEEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --method)     METHOD="${2:?--method needs a value}"; shift 2;;
        --method=*)   METHOD="${1#*=}"; shift;;
        --no-plugin)  WITH_PLUGIN=0; shift;;
        --with-plugin) WITH_PLUGIN=1; shift;;
        --project)    PROJECT="${2:?--project needs a path}"; shift 2;;
        --project=*)  PROJECT="${1#*=}"; shift;;
        --run-build)  RUN_BUILD=1; shift;;
        --keep)       KEEP=1; shift;;
        -h|--help)    sed -n '2,80p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) printf 'unknown flag: %s (try --help)\n' "$1" >&2; exit 2;;
    esac
done

case "$METHOD" in pipx|venv|index) ;; *) printf 'bad --method: %s (pipx|venv|index)\n' "$METHOD" >&2; exit 2;; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WORKDIR="$(mktemp -d -t stm32-localwheel.XXXXXX)"
DISTDIR="$WORKDIR/dist"
SEP=$'\x1f'

# Staged version under test = this checkout's pyproject version.
EXPECTED_VER="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$REPO_ROOT/pyproject.toml" 2>/dev/null | head -n1)"
PLUGIN_VER="$(sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p' "$REPO_ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -n1)"

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

# --- teardown (always, unless --keep) ----------------------------------------
PIPX_INSTALLED=0; PLUGIN_INSTALLED=0; MARKETPLACE_ADDED=0; SERVER_PID=""
cleanup() {
    local code=$?
    if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" >/dev/null 2>&1 || true; fi
    if [ "$KEEP" = "1" ]; then
        printf '\n%s--keep: leaving install + workdir in place%s\n' "$GRY" "$RST"
        printf '%s  workdir: %s%s\n' "$GRY" "$WORKDIR" "$RST"
        [ "$PIPX_INSTALLED" = "1" ] && printf '%s  pipx app: embedagents-stm32 (stm32 on PATH)%s\n' "$GRY" "$RST"
        [ "$PLUGIN_INSTALLED" = "1" ] && printf '%s  plugin: embedagents-stm32@embedagents registered%s\n' "$GRY" "$RST"
        return $code
    fi
    section "Teardown"
    if [ "$PLUGIN_INSTALLED" = "1" ]; then
        claude plugin uninstall embedagents-stm32 >/dev/null 2>&1 && printf '  removed plugin\n' || printf '  (plugin uninstall skipped)\n'
    fi
    if [ "$MARKETPLACE_ADDED" = "1" ]; then
        claude plugin marketplace remove embedagents >/dev/null 2>&1 && printf '  removed marketplace\n' || printf '  (marketplace remove skipped)\n'
    fi
    if [ "$PIPX_INSTALLED" = "1" ]; then
        pipx uninstall embedagents-stm32 >/dev/null 2>&1 && printf '  pipx uninstalled\n' || printf '  (pipx uninstall skipped)\n'
    fi
    rm -rf "$WORKDIR"
    return $code
}
trap cleanup EXIT

# --- 1. Preflight ------------------------------------------------------------
section "Preflight"
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
record "method = $METHOD" 1
command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1 || HAS_CLAUDE=0
if [ "$METHOD" = "pipx" ]; then
    command -v pipx >/dev/null 2>&1 && HAS_PIPX=1 || HAS_PIPX=0
    record "pipx available" "$HAS_PIPX" "$([ "$HAS_PIPX" = 1 ] && pipx --version 2>&1)"
    if [ "$HAS_PIPX" != "1" ]; then
        printf '\n%spipx not found; install it or rerun with --method venv.%s\n' "$RED" "$RST"; exit 1
    fi
fi
mkdir -p "$DISTDIR"

# Build-tooling venv (isolated build + twine, never touches the system env).
BUILDVENV="$WORKDIR/venv-build"
"$PY" -m venv "$BUILDVENV"
BPY="$BUILDVENV/bin/python"
"$BPY" -m pip install -q --upgrade pip build twine >"$WORKDIR/buildtools.log" 2>&1 \
    && record "build tooling (build+twine)" 1 \
    || { record "build tooling (build+twine)" 0 "$(tail -n3 "$WORKDIR/buildtools.log" | tr '\n' ' ')"; exit 1; }

# --- 2. Version gate ---------------------------------------------------------
section "Version gate"
if [ -z "$EXPECTED_VER" ]; then
    record "read pyproject version" 0 "no version in pyproject.toml"
else
    record "pyproject version" 1 "$EXPECTED_VER"
    if [ "$PLUGIN_VER" = "$EXPECTED_VER" ]; then
        record "plugin.json == pyproject" 1 "$PLUGIN_VER"
    else
        record "plugin.json == pyproject" 0 "plugin.json=$PLUGIN_VER vs pyproject=$EXPECTED_VER (version skew)"
    fi
fi

# --- 3. Clean build ----------------------------------------------------------
section "Clean build (python -m build)"
rm -rf "$REPO_ROOT/build"
if ( cd "$REPO_ROOT" && "$BPY" -m build --outdir "$DISTDIR" ) >"$WORKDIR/build.log" 2>&1; then
    WHEEL="$(ls "$DISTDIR"/*.whl 2>/dev/null | head -n1)"
    record "python -m build" 1 "$(basename "${WHEEL:-?}")"
else
    record "python -m build" 0 "$(tail -n3 "$WORKDIR/build.log" | tr '\n' ' ')"
    exit 1
fi

# --- 4. Wheel gate (reuse the CI artifact assertions) ------------------------
section "Wheel gate"
if "$BPY" -m twine check "$DISTDIR"/* >"$WORKDIR/twine.log" 2>&1; then
    record "twine check" 1
else
    record "twine check" 0 "$(tail -n3 "$WORKDIR/twine.log" | tr '\n' ' ')"
fi
cat >"$WORKDIR/wheel_gate.py" <<'PY'
import glob, os, re, tarfile, zipfile
distdir = os.environ["DISTDIR"]; expected = os.environ["EXPECTED_VER"]
wheel = glob.glob(os.path.join(distdir, "*.whl"))[0]
names = zipfile.ZipFile(wheel).namelist()
schemas = [n for n in names if n.endswith(".schema.json")]
assert len(schemas) >= 3, f"schemas missing from wheel: {schemas}"
assert not [n for n in names if n.startswith("tests/")], "tests leaked into wheel"
assert "embedagents/__init__.py" not in names, "namespace broken: embedagents/__init__.py shipped"
assert any(n.startswith("embedagents/stm32/") for n in names), "package not under embedagents/stm32/"
sdist = glob.glob(os.path.join(distdir, "*.tar.gz"))[0]
snames = tarfile.open(sdist).getnames()
assert not [n for n in snames if "/tests/" in n], "tests leaked into sdist"
built = re.search(r"-(\d[^-]*)-py3", os.path.basename(wheel)).group(1)
assert built == expected, f"built version {built} != expected {expected}"
print(f"WHEEL_GATE_OK {len(names)} files / {len(schemas)} schemas / version {built}")
PY
GATE="$(DISTDIR="$DISTDIR" EXPECTED_VER="$EXPECTED_VER" "$BPY" "$WORKDIR/wheel_gate.py" 2>&1)"
case "$GATE" in
    *WHEEL_GATE_OK*) record "wheel contents (schemas/namespace/version)" 1 "${GATE#WHEEL_GATE_OK }";;
    *)               record "wheel contents (schemas/namespace/version)" 0 "$(printf '%s' "$GATE" | tail -n1)";;
esac

# --- 5. Install (method-dependent) -------------------------------------------
section "Install (--method $METHOD)"
INSTALL_PY=""; STM32=""
case "$METHOD" in
  pipx)
    if pipx install --force "$WHEEL" >"$WORKDIR/install.log" 2>&1; then
        PIPX_INSTALLED=1
        local_bin="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null)"; [ -n "$local_bin" ] || local_bin="$HOME/.local/bin"
        pipx_home="$(pipx environment --value PIPX_HOME 2>/dev/null)"; [ -n "$pipx_home" ] || pipx_home="$HOME/.local/share/pipx"
        STM32="$local_bin/stm32"
        INSTALL_PY="$pipx_home/venvs/embedagents-stm32/bin/python"
        record "pipx install wheel" 1 "$(basename "$WHEEL")"
    else
        record "pipx install wheel" 0 "$(tail -n3 "$WORKDIR/install.log" | tr '\n' ' ')"
    fi
    ;;
  venv)
    RUNVENV="$WORKDIR/venv-run"; "$PY" -m venv "$RUNVENV"; INSTALL_PY="$RUNVENV/bin/python"; STM32="$RUNVENV/bin/stm32"
    "$INSTALL_PY" -m pip install -q --upgrade pip >/dev/null 2>&1
    # Pre-download the wheel's deps into dist/ so --no-index is fully offline.
    "$INSTALL_PY" -m pip download -q --dest "$DISTDIR" --find-links "$DISTDIR" \
        "embedagents-stm32==$EXPECTED_VER" >"$WORKDIR/download.log" 2>&1 || true
    if "$INSTALL_PY" -m pip install -q --no-index --find-links "$DISTDIR" \
        "embedagents-stm32==$EXPECTED_VER" >"$WORKDIR/install.log" 2>&1; then
        record "venv install (--no-index --find-links)" 1
    else
        record "venv install (--no-index --find-links)" 0 "$(tail -n3 "$WORKDIR/install.log" | tr '\n' ' ')"
    fi
    ;;
  index)
    if ! command -v pypi-server >/dev/null 2>&1; then
        skip "index install" "pypi-server not installed (pip install pypiserver)"
    else
        PORT="${PORT:-8765}"
        pypi-server run -p "$PORT" -i 127.0.0.1 "$DISTDIR" >"$WORKDIR/server.log" 2>&1 &
        SERVER_PID=$!
        # wait for readiness
        ready=0
        for _ in $(seq 1 30); do
            if "$PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/simple/',timeout=1)" 2>/dev/null; then ready=1; break; fi
            sleep 0.3
        done
        if [ "$ready" != "1" ]; then
            record "local index server up" 0 "$(tail -n2 "$WORKDIR/server.log" | tr '\n' ' ')"
        else
            record "local index server up" 1 "127.0.0.1:$PORT"
            RUNVENV="$WORKDIR/venv-run"; "$PY" -m venv "$RUNVENV"; INSTALL_PY="$RUNVENV/bin/python"; STM32="$RUNVENV/bin/stm32"
            "$INSTALL_PY" -m pip install -q --upgrade pip >/dev/null 2>&1
            if "$INSTALL_PY" -m pip install -q -i "http://127.0.0.1:$PORT/simple/" \
                --extra-index-url https://pypi.org/simple --trusted-host 127.0.0.1 \
                "embedagents-stm32==$EXPECTED_VER" >"$WORKDIR/install.log" 2>&1; then
                record "index install (pip -i localhost name==ver)" 1
            else
                record "index install (pip -i localhost name==ver)" 0 "$(tail -n3 "$WORKDIR/install.log" | tr '\n' ' ')"
            fi
        fi
    fi
    ;;
esac

# --- 6. CLI smoke ------------------------------------------------------------
section "CLI smoke"
if [ -z "$STM32" ] || [ ! -x "$STM32" ]; then
    skip "stm32 --version" "no installed stm32 binary (install phase failed)"
else
    VER="$("$STM32" --version 2>&1)"
    case "$VER" in *"$EXPECTED_VER"*) record "stm32 --version = $EXPECTED_VER" 1 "$VER";;
                   *)                 record "stm32 --version = $EXPECTED_VER" 0 "$VER";; esac
    if [ -n "$INSTALL_PY" ] && [ -x "$INSTALL_PY" ]; then
        cat >"$WORKDIR/schema_check.py" <<'PY'
import json, importlib.resources as r
p = r.files("embedagents.stm32.schemas").joinpath("stm32-project.schema.json")
d = json.loads(p.read_text(encoding="utf-8"))
assert str(d.get("$id", "")).endswith("stm32-project.schema.json"), "unexpected $id"
print("SCHEMA_OK")
PY
        OUT="$("$INSTALL_PY" "$WORKDIR/schema_check.py" 2>&1)"
        case "$OUT" in *SCHEMA_OK*) record "bundled schemas load" 1;;
                       *)           record "bundled schemas load" 0 "$OUT";; esac
    fi
    if [ "$RUN_BUILD" = "1" ]; then
        if [ -z "$PROJECT" ]; then
            skip "stm32 build (end-to-end)" "--run-build needs --project <dir>"
        elif [ ! -d "$PROJECT" ]; then
            skip "stm32 build (end-to-end)" "project dir not found: $PROJECT"
        else
            # Run from the PROJECT dir: the real "build my project" scenario, and the
            # exact cwd==project-root case RES-050 was made for. This lets the
            # substrate's cwd-upward search find a .claude/stm32-tools.local.jsonc
            # (tools may also come from env/PATH); the in-tree-workspace assertion
            # below still proves RES-050 keeps the Eclipse workspace OUT of the tree.
            ( cd "$PROJECT" && "$STM32" build "$PROJECT" ) >"$WORKDIR/build_smoke.log" 2>&1
            if [ $? -eq 0 ]; then
                record "stm32 build (end-to-end)" 1 "$(grep -aiE 'errors|warnings|build of' "$WORKDIR/build_smoke.log" | tail -n1)"
            else
                # Surface the structured error's message+hint, not the JSON tail.
                detail="$(grep -aoE '"(message|hint)": "[^"]*"' "$WORKDIR/build_smoke.log" | tr '\n' ' ')"
                [ -n "$detail" ] || detail="$(tail -n3 "$WORKDIR/build_smoke.log" | tr '\n' ' ')"
                record "stm32 build (end-to-end)" 0 "$detail"
            fi
            if [ -e "$PROJECT/.stm32-substrate-workspace" ]; then
                record "workspace kept out of project tree (RES-050)" 0 "found $PROJECT/.stm32-substrate-workspace"
            else
                record "workspace kept out of project tree (RES-050)" 1 "no in-tree .stm32-substrate-workspace"
            fi
        fi
    fi
fi

# --- 7. Plugin channel -------------------------------------------------------
section "Plugin channel"
if [ "$WITH_PLUGIN" != "1" ]; then
    skip "plugin channel" "--no-plugin"
elif [ "$HAS_CLAUDE" != "1" ]; then
    skip "plugin channel" "claude CLI not on PATH"
else
    if claude plugin marketplace add "$REPO_ROOT" >"$WORKDIR/mkt.log" 2>&1; then
        MARKETPLACE_ADDED=1; record "marketplace add (local source)" 1
        if claude plugin install embedagents-stm32@embedagents >"$WORKDIR/pluginst.log" 2>&1; then
            PLUGIN_INSTALLED=1; record "plugin install" 1
            LIST="$(claude plugin list 2>&1)"
            # `claude plugin list` prints the plugin name but NOT its version, so
            # verify the version via the plugin cache path, which encodes
            # <marketplace>/<plugin>/<version>. Fall back to a version string in the
            # listing in case a future claude release starts printing one.
            CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
            VER_DIR="$CLAUDE_HOME/plugins/cache/embedagents/embedagents-stm32/$EXPECTED_VER"
            NAME_OK=0; printf '%s' "$LIST" | grep -qi 'embedagents-stm32' && NAME_OK=1
            VER_OK=0; { [ -d "$VER_DIR" ] || printf '%s' "$LIST" | grep -q "$EXPECTED_VER"; } && VER_OK=1
            if [ "$NAME_OK" = 1 ] && [ "$VER_OK" = 1 ]; then
                record "plugin registered at $EXPECTED_VER" 1
            else
                record "plugin registered at $EXPECTED_VER" 0 "name_in_list=$NAME_OK; cache $VER_DIR present=$([ -d "$VER_DIR" ] && echo yes || echo no)"
            fi
        else
            record "plugin install" 0 "$(tail -n3 "$WORKDIR/pluginst.log" | tr '\n' ' ')"
        fi
    else
        record "marketplace add (local source)" 0 "$(tail -n3 "$WORKDIR/mkt.log" | tr '\n' ' ')"
    fi
fi

# --- 8. Summary --------------------------------------------------------------
section "Summary"
printf '%-6s %-44s %s\n' "STATUS" "CHECK" "DETAIL"
for r in "${ROWS[@]}"; do
    IFS="$SEP" read -r st nm dt <<<"$r"
    printf '%-6s %-44s %s\n' "$st" "$nm" "$dt"
done
echo
if [ "$fails" -eq 0 ]; then
    printf '%sRESULT: PASS%s (%d checks, %d skipped) — wheel %s\n' "$GRN" "$RST" "$total" "$skips" "$EXPECTED_VER"
else
    printf '%sRESULT: FAIL%s (%d failed, %d skipped)\n' "$RED" "$RST" "$fails" "$skips"
fi

[ "$fails" -eq 0 ] && exit 0 || exit 1
