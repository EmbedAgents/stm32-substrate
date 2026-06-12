# Detect attached ST-LINK probes and report board names. PowerShell
# counterpart to tools/check-hw-env.sh. Output contract is identical
# across both OSes so test-side parsing is the same.
#
# Output (always on stdout):
#     BOARDS=<comma-separated-board-names>
#
# Always exits 0 - no probe / no CLI installed both produce ``BOARDS=``
# rather than a non-zero exit, so CI workflows can call this
# unconditionally and pytest hardware markers can skip tests cleanly
# when the required board is absent (per RES-019: NUCLEO-L476RG,
# NUCLEO-H745ZI-Q, NUCLEO-N657X0-Q).
#
# Resolution order for the probe CLI:
#   1. ``$env:STM32_PROGRAMMER_CLI`` env var (if file exists)
#   2. ``STM32_Programmer_CLI`` on PATH (Get-Command)
#   3. ``st-info`` on PATH (open-source fallback)
#
# Probe-list parsing accepts both ``Board`` (legacy / synthesised
# fixtures) and ``Board Name`` (live v2.22.0 Windows output). Board
# value ``--`` (CubeProgrammer's placeholder for a bare ST-LINK with
# no attached NUCLEO) is filtered out.
#
# Status notes (board count, which CLI was used, any errors) go to
# stderr so the BOARDS= line on stdout is easy for tools to parse.

$ErrorActionPreference = 'Continue'

function Write-Status {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Resolve-Cli {
    if ($env:STM32_PROGRAMMER_CLI -and (Test-Path -LiteralPath $env:STM32_PROGRAMMER_CLI -PathType Leaf)) {
        return $env:STM32_PROGRAMMER_CLI
    }
    $cmd = Get-Command STM32_Programmer_CLI -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Resolve-StInfo {
    $cmd = Get-Command st-info -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Parse-Boards-Cubeprog {
    param([string[]]$Lines)
    # Block-scoped parser - mirrors src/embedagents/stm32/cubeprogrammer/
    # parsers.py:parse_probe_list. Open a new probe block on
    # "ST-Link Probe N :"; close it on the next "===== ... =====" section
    # heading. Only consider "Board" / "Board Name" lines INSIDE a probe
    # block - this filters duplicate matches in the UART / DFU / J-Link
    # sections (live v2.22 re-prints "Board Name :" inside the UART
    # interface block per attached VCP).
    $boards = @()
    $inBlock = $false
    foreach ($line in $Lines) {
        if ($line -match '^\s*ST-Link Probe\s+\d+\s*:') {
            $inBlock = $true
            continue
        }
        $stripped = $line.Trim()
        if ($stripped.StartsWith('=====') -and $stripped.EndsWith('=====')) {
            $inBlock = $false
            continue
        }
        if (-not $inBlock) { continue }
        if ($line -match '^\s*Board(\s+Name)?\s*:\s*(.+?)\s*$') {
            $value = $Matches[2].Trim()
            if ($value -and $value -ne '--') {
                $boards += $value
            }
        }
    }
    return $boards
}

function Parse-Boards-StInfo {
    param([string[]]$Lines)
    $boards = @()
    foreach ($line in $Lines) {
        # st-info --probe output: "descr:       NUCLEO-L476RG"
        if ($line -match '^\s*descr:\s*(.+?)\s*$') {
            $value = $Matches[1].Trim()
            if ($value -and $value -ne '--') {
                $boards += $value
            }
        }
    }
    return $boards
}

$boards = @()

$cli = Resolve-Cli
if ($cli) {
    Write-Status "check-hw-env: using $cli"
    try {
        $raw = & $cli -l 2>$null
    } catch {
        $raw = @()
    }
    $boards = Parse-Boards-Cubeprog -Lines $raw
} else {
    $stinfo = Resolve-StInfo
    if ($stinfo) {
        Write-Status "check-hw-env: using $stinfo (STM32_Programmer_CLI not found)"
        try {
            $raw = & $stinfo --probe 2>$null
        } catch {
            $raw = @()
        }
        $boards = Parse-Boards-StInfo -Lines $raw
    } else {
        Write-Status "check-hw-env: no probe CLI found (set `$env:STM32_PROGRAMMER_CLI or install st-info)"
    }
}

Write-Status "check-hw-env: detected $($boards.Count) board(s)"
Write-Output ("BOARDS=" + ($boards -join ','))
exit 0
