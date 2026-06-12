#Requires -Version 5
<#
.SYNOPSIS
    Validate a published embedagents-stm32 release on Windows (no hardware).

.DESCRIPTION
    Runs the two no-hardware tiers of the Windows release test and prints a
    PASS/FAIL summary:

      Tier 1 - Published install (as a brand-new user would experience it):
        * create a throwaway venv
        * pip install from the published GitHub repo (git+https; pinned to
          -ReleaseTag when given)
        * verify the `stm32` console script + version
        * verify the package-bundled JSON schemas load via importlib.resources

      Tier 2 - This checkout:
        * editable install with [dev] extras into a second throwaway venv
        * run the unit test suite (smoke/hardware/eval are deselected by default)
        * validate the plugin/marketplace manifest with `claude plugin validate`

    The interactive check - that the five /stm32* slash commands register in a
    live Claude Code session - cannot be automated; the script prints the manual
    steps for it (plus the ST-tools/hardware Tier 3) at the end.

    Exit code is 0 only if every executed check passed.

.PARAMETER RepoUrl
    Git URL used for the Tier 1 fresh install. Defaults to the public repo.

.PARAMETER ReleaseTag
    Tag under test (e.g. v0.1.1). When given, Tier 1 installs git+RepoUrl@ReleaseTag
    and asserts `stm32 --version` matches the tag (minus the leading v). When
    omitted, Tier 1 installs the default branch and the expected version is read
    from this checkout's pyproject.toml (dev-run mode). Run from a checkout of the
    tag so Tier 2 tests the same code.

.PARAMETER WorkDir
    Scratch directory for the throwaway venvs. Defaults to a temp folder; removed
    on exit unless -KeepWorkDir is given.

.PARAMETER KeepWorkDir
    Keep the scratch venvs after the run (useful for poking at a failure).

.EXAMPLE
    pwsh -File tools\test-release-windows.ps1 -ReleaseTag v0.1.1
#>
[CmdletBinding()]
param(
    [string]$RepoUrl    = "https://github.com/EmbedAgents/stm32-substrate.git",
    [string]$ReleaseTag = "",
    [string]$WorkDir    = (Join-Path $env:TEMP "stm32-release-test"),
    [switch]$KeepWorkDir
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot   # tools\ -> repo root

# What to install in Tier 1, and which version string `stm32 --version` must show.
if ($ReleaseTag) {
    $PipSpec     = "git+$RepoUrl@$ReleaseTag"
    $ExpectedVer = $ReleaseTag -replace '^v', ''
} else {
    $PipSpec     = "git+$RepoUrl"
    $ExpectedVer = ""
    $pyproject = Join-Path $RepoRoot "pyproject.toml"
    if (Test-Path $pyproject) {
        $m = Select-String -Path $pyproject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { $ExpectedVer = $m.Matches[0].Groups[1].Value }
    }
}

# --- result tracking ---------------------------------------------------------
$results = New-Object System.Collections.Generic.List[object]
function Record([string]$name, [bool]$ok, [string]$detail = "") {
    $status = if ($ok) { "PASS" } else { "FAIL" }
    $results.Add([pscustomobject]@{ Check = $name; Status = $status; Detail = $detail })
    Write-Host ("  [{0}] {1}{2}" -f $status, $name, $(if ($detail) { " - $detail" } else { "" })) `
        -ForegroundColor $(if ($ok) { "Green" } else { "Red" })
}
function Skip([string]$name, [string]$why) {
    $results.Add([pscustomobject]@{ Check = $name; Status = "SKIP"; Detail = $why })
    Write-Host ("  [SKIP] {0} - {1}" -f $name, $why) -ForegroundColor Yellow
}
function Section([string]$t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

# --- prerequisites -----------------------------------------------------------
Section "Prerequisites"

# Resolve a Python: prefer the py launcher (3.12 -> 3.11 -> default), else python.
$PyExe = $null; $PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("-3.12", "-3.11", "")) {
        if ($v) { & py $v --version *> $null } else { & py --version *> $null }
        if ($LASTEXITCODE -eq 0) { $PyExe = "py"; $PyArgs = $(if ($v) { @($v) } else { @() }); break }
    }
}
if (-not $PyExe -and (Get-Command python -ErrorAction SilentlyContinue)) { $PyExe = "python"; $PyArgs = @() }

if (-not $PyExe) {
    Record "python available" $false "no 'py' or 'python' on PATH"
    Write-Host "`nCannot continue without Python 3.11+." -ForegroundColor Red
    exit 1
}
$pyVer = (& $PyExe @PyArgs --version 2>&1)
Record "python available" $true "$PyExe $($PyArgs -join ' ') ($pyVer)"

$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
Record "git available" $hasGit
$claude = Get-Command claude -ErrorAction SilentlyContinue

# scratch dir
if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

# --- Tier 1: published install ----------------------------------------------
Section ("Tier 1 - published install (git+https{0})" -f $(if ($ReleaseTag) { ", tag $ReleaseTag" } else { "" }))

if (-not $hasGit) {
    Skip "Tier 1 (all)" "git is required for a git+https install"
} else {
    $t1venv = Join-Path $WorkDir "venv-install"
    & $PyExe @PyArgs -m venv $t1venv
    $t1py = Join-Path $t1venv "Scripts\python.exe"

    if (-not (Test-Path $t1py)) {
        Record "create install venv" $false "venv python not found at $t1py"
    } else {
        Record "create install venv" $true
        & $t1py -m pip install --quiet --upgrade pip *> $null

        $log = & $t1py -m pip install --quiet $PipSpec 2>&1
        $ok = ($LASTEXITCODE -eq 0)
        Record "pip install $PipSpec" $ok $(if (-not $ok) { ($log | Select-Object -Last 3) -join " | " })

        if ($ok) {
            $stm32 = Join-Path $t1venv "Scripts\stm32.exe"
            $ver = (& $stm32 --version 2>&1) -join " "
            if ($ExpectedVer) {
                Record "stm32 --version = $ExpectedVer" ($ver -match [regex]::Escape($ExpectedVer)) $ver
            } else {
                # No tag given and no readable pyproject - at least require the
                # console script to run and print something version-shaped.
                Record "stm32 --version runs" ($ver -match "\d+\.\d+") $ver
            }

            # bundled schemas load via importlib.resources
            $chk = Join-Path $WorkDir "schema_check.py"
            @'
import json, importlib.resources as r
p = r.files("embedagents.stm32.schemas").joinpath("stm32-project.schema.json")
d = json.loads(p.read_text(encoding="utf-8"))
assert str(d.get("$id", "")).endswith("stm32-project.schema.json"), "unexpected $id"
print("SCHEMA_OK")
'@ | Set-Content -Path $chk -Encoding ASCII
            $schemaOut = (& $t1py $chk 2>&1) -join " "
            Record "bundled schemas load" ($schemaOut -match "SCHEMA_OK") $(if ($schemaOut -notmatch "SCHEMA_OK") { $schemaOut })
        }
    }
}

# --- Tier 2: this checkout ---------------------------------------------------
Section "Tier 2 - this checkout (unit suite + plugin validate)"

if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    Skip "Tier 2 (all)" "run this script from a clone (no pyproject.toml at $RepoRoot)"
} else {
    $t2venv = Join-Path $WorkDir "venv-dev"
    & $PyExe @PyArgs -m venv $t2venv
    $t2py = Join-Path $t2venv "Scripts\python.exe"

    if (-not (Test-Path $t2py)) {
        Record "create dev venv" $false
    } else {
        Record "create dev venv" $true
        & $t2py -m pip install --quiet --upgrade pip *> $null
        $log = & $t2py -m pip install --quiet -e "$RepoRoot[dev]" 2>&1
        $ok = ($LASTEXITCODE -eq 0)
        Record "pip install -e .[dev]" $ok $(if (-not $ok) { ($log | Select-Object -Last 3) -join " | " })

        if ($ok) {
            Push-Location $RepoRoot
            $pytestOut = & $t2py -m pytest -q 2>&1
            $pytestCode = $LASTEXITCODE
            Pop-Location
            $summary = ($pytestOut | Select-String -Pattern "passed|failed|error" | Select-Object -Last 1)
            Record "unit suite (pytest)" ($pytestCode -eq 0) "$summary"
            if ($pytestCode -ne 0) { $pytestOut | Select-Object -Last 15 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray } }
        }
    }

    # Plugin/marketplace manifest validation (read-only; does not install anything)
    if ($claude) {
        $valOut = & claude plugin validate --strict $RepoRoot 2>&1
        Record "claude plugin validate --strict" ($LASTEXITCODE -eq 0) (($valOut | Select-String "Validation").Line)
    } else {
        Skip "claude plugin validate" "claude CLI not on PATH"
    }
}

# --- summary -----------------------------------------------------------------
Section "Summary"
$results | Format-Table -AutoSize Check, Status, Detail | Out-Host
$fails = ($results | Where-Object Status -eq "FAIL").Count
$skips = ($results | Where-Object Status -eq "SKIP").Count

if (-not $KeepWorkDir) { Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue }
else { Write-Host "Scratch venvs kept at: $WorkDir" -ForegroundColor DarkGray }

Write-Host ""
if ($fails -eq 0) {
    Write-Host "RESULT: PASS ($($results.Count - $skips) checks, $skips skipped)" -ForegroundColor Green
} else {
    Write-Host "RESULT: FAIL ($fails failed, $skips skipped)" -ForegroundColor Red
}

# --- manual steps the script can't automate ----------------------------------
Section "Manual steps (not automated)"
Write-Host @"
These need a human / hardware and aren't covered above:

  Plugin registration (interactive):
    claude plugin marketplace add EmbedAgents/stm32-substrate
    claude plugin install embedagents-stm32@embedagents
    # restart Claude Code, type '/', confirm /stm32prog /stm32build /stm32debug
    # /stm32project /stm32agent all appear.

  Tier 3 - ST tools + board (real end-to-end):
    1. copy .claude\stm32-tools.local.jsonc.example -> .claude\stm32-tools.local.jsonc
       and fill your Windows tool paths (Program Files\STMicroelectronics\..., .exe).
    2. pytest -m smoke            # vendor CLIs reachable, no board needed
    3. attach a NUCLEO + ST-LINK, then in Claude (in a folder with a
       stm32-project.jsonc):  "list the connected probe"  then  "build it and
       flash my Nucleo."
"@ -ForegroundColor Gray

exit $(if ($fails -eq 0) { 0 } else { 1 })
