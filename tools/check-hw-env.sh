#!/usr/bin/env bash
# Detect attached ST-LINK probes and report board names.
#
# Output (always on stdout):
#     BOARDS=<comma-separated-board-names>
#
# Always exits 0 — no probe / no CLI installed both produce ``BOARDS=``
# rather than a non-zero exit, so CI workflows can call this unconditionally
# and pytest hardware markers can skip tests cleanly when the required
# board is absent (per RES-019: NUCLEO-L476RG, NUCLEO-H745ZI-Q,
# NUCLEO-N657X0-Q).
#
# Resolution order for the probe CLI:
#   1. ``STM32_PROGRAMMER_CLI`` env var (if executable)
#   2. ``STM32_Programmer_CLI`` on PATH
#   3. ``st-info`` on PATH (open-source fallback)
#
# Status notes (board count, which CLI was used, any errors) go to stderr
# so the BOARDS= line on stdout is easy for tools to parse.

set -u

log() { echo "$@" >&2; }

strip_ansi() {
    # Remove ANSI CSI escape sequences (color codes) + carriage returns.
    # Live STM32_Programmer_CLI v2.22 emits color codes even when piped to
    # a non-TTY; they prefix every line (e.g. "\e[90m   Board Name : ...")
    # and break the line-start anchors in parse_boards_cubeprog, making the
    # detector report 0 boards while a probe is actually attached. The
    # Python substrate parser strips these already; this mirrors it.
    sed -E "s/$(printf '\033')\[[0-9;]*[a-zA-Z]//g; s/$(printf '\r')//g"
}

resolve_cli() {
    if [[ -n "${STM32_PROGRAMMER_CLI:-}" && -x "${STM32_PROGRAMMER_CLI}" ]]; then
        echo "${STM32_PROGRAMMER_CLI}"
        return 0
    fi
    if command -v STM32_Programmer_CLI >/dev/null 2>&1; then
        command -v STM32_Programmer_CLI
        return 0
    fi
    return 1
}

resolve_stinfo() {
    if command -v st-info >/dev/null 2>&1; then
        command -v st-info
        return 0
    fi
    return 1
}

parse_boards_cubeprog() {
    # Block-scoped parser mirroring src/stm32_substrate/cubeprogrammer/
    # parsers.py:parse_probe_list. Open a new probe block on
    # "ST-Link Probe N :"; close it on the next "===== ... =====" section
    # heading. Only "Board" / "Board Name" lines INSIDE a probe block
    # count - filters duplicate matches in the UART / DFU / J-Link
    # sections (live v2.22 re-prints "Board Name :" inside the UART
    # interface block per attached VCP). Accepts both "Board" (legacy /
    # synthesised) and "Board Name" (live v2.22) field labels.
    awk '
        /^[[:space:]]*ST-Link Probe[[:space:]]+[0-9]+[[:space:]]*:/ { in_block = 1; next }
        /^[[:space:]]*=====.*=====[[:space:]]*$/ { in_block = 0; next }
        in_block && /^[[:space:]]*Board([[:space:]]+Name)?[[:space:]]*:/ {
            sub(/^[[:space:]]*Board([[:space:]]+Name)?[[:space:]]*:[[:space:]]*/, "")
            sub(/[[:space:]]+$/, "")
            if ($0 != "" && $0 != "--") print
        }
    '
}

parse_boards_stinfo() {
    # st-info --probe output: ``descr:       NUCLEO-L476RG``
    grep -E "^[[:space:]]*descr:" | sed -E 's/^[[:space:]]*descr:[[:space:]]*//; s/[[:space:]]+$//'
}

boards=""

if cli=$(resolve_cli); then
    log "check-hw-env: using ${cli}"
    raw=$("${cli}" -l 2>/dev/null || true)
    boards=$(printf '%s\n' "${raw}" | strip_ansi | parse_boards_cubeprog | paste -sd, -)
elif stinfo=$(resolve_stinfo); then
    log "check-hw-env: using ${stinfo} (STM32_Programmer_CLI not found)"
    raw=$("${stinfo}" --probe 2>/dev/null || true)
    boards=$(printf '%s\n' "${raw}" | parse_boards_stinfo | paste -sd, -)
else
    log "check-hw-env: no probe CLI found (set STM32_PROGRAMMER_CLI or install st-info)"
fi

count=$(printf '%s\n' "${boards}" | awk -F, 'NF{print NF}')
log "check-hw-env: detected ${count:-0} board(s)"

echo "BOARDS=${boards}"
exit 0
