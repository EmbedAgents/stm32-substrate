# Verify RES-054 (workspace-reuse .project corruption guard) on Windows.
#
# Runs two layers:
#   1. RES-054 unit tests (fast, CubeIDE-independent) - the mocked-build
#      logic: default resets+imports, explicit raises, the detector + the
#      workspace reset helper.
#   2. The RES-054 smoke test (real CubeIDE -import/-build) - the part mocks
#      cannot cover: a real Eclipse import, a real .location decode, and the
#      actual fix end-to-end on the F-PROJ BLINKY linked-resource example.
#      Skips cleanly if CubeIDE or the F-PROJ fixture is absent.
#
# IMPORTANT: this package is src-layout, so pytest imports the *installed*
# `embedagents`, NOT the repo source. A pulled-but-not-reinstalled checkout
# silently tests a STALE installed package (the failure seen on Windows:
# AttributeError: ... has no attribute 'reset_workspace_for_import'). This
# script guards against that: it verifies the loaded package actually carries
# the RES-054 symbols and, by default, self-heals by creating a repo .venv and
# editable-installing the source into it (Mode A). Pass -NoInstall to opt out.
#
# Usage (from anywhere):
#     powershell -ExecutionPolicy Bypass -File tools\verify-res054.ps1
#     tools\verify-res054.ps1 -CubeIdePath "C:\ST\STM32CubeIDE_2.1.1\STM32CubeIDE\stm32cubeide.exe"
#     tools\verify-res054.ps1 -NoInstall   # never touch the environment
#
# Resolution:
#   - Python: -Python arg, else <repo>\.venv\Scripts\python.exe, else `python` on PATH.
#   - CubeIDE: -CubeIdePath arg, else $env:STM32CUBEIDE, else common install globs.
#     If none is found the smoke layer SKIPS (not fails) - the unit layer still runs.
#
# Exit code: 0 when the unit layer passes and the smoke layer passes-or-skips;
# 1 on any test failure; 2 on environment errors (no python / stale install
# that could not be repaired).

param(
    [string]$CubeIdePath,
    [string]$Python,
    [switch]$NoInstall
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $repo '.venv\Scripts\python.exe'

function Write-Section { param([string]$Message) Write-Host "`n=== $Message ===" -ForegroundColor Cyan }

# Probe the package the given Python actually imports. Returns an object with
# .Ok (bool: RES-054 symbols all present) and .Out (the MODULE=/MISSING= lines
# or the import traceback). Single source of truth for "is this install fresh".
function Test-Res054Install {
    param([string]$Py)
    $code = @'
import sys, importlib.util
try:
    import embedagents.stm32.cubeide.workspace as w
except Exception as exc:
    print("MODULE=<import failed: %s>" % exc)
    print("PYTEST=" + ("yes" if importlib.util.find_spec("pytest") else "no"))
    raise SystemExit(2)
need = ("reset_workspace_for_import", "missing_linked_folders", "default_workspace_root")
missing = [n for n in need if not hasattr(w, n)]
has_pytest = importlib.util.find_spec("pytest") is not None
print("MODULE=" + w.__file__)
print("MISSING=" + (",".join(missing) if missing else "(none)"))
print("PYTEST=" + ("yes" if has_pytest else "no"))
raise SystemExit(0 if (not missing and has_pytest) else 1)
'@
    # Write to a temp .py and run that - passing a multi-line snippet via
    # `python -c` is mangled by PowerShell/Windows native-arg handling.
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("res054_probe_" + [System.IO.Path]::GetRandomFileName() + ".py")
    Set-Content -LiteralPath $tmp -Value $code -Encoding ASCII
    try {
        $out = & $Py $tmp 2>&1
        $ok = ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{ Ok = $ok; Out = $out }
}

function Stop-Stale {
    param([string]$Py, [string[]]$Probe)
    Write-Host "`nERROR: the environment '$Py' uses is not usable for RES-054 tests" -ForegroundColor Red
    Write-Host "(stale/foreign embedagents shadowing the repo source, or pytest missing):" -ForegroundColor Red
    foreach ($line in $Probe) { Write-Host "  $line" -ForegroundColor Red }
    Write-Host "`nFix it (one time), then re-run this script:" -ForegroundColor Yellow
    Write-Host "  cd `"$repo`"" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv" -ForegroundColor Yellow
    Write-Host "  .\.venv\Scripts\python -m pip install -e `".[dev]`"" -ForegroundColor Yellow
    exit 2
}

# --- resolve python ---------------------------------------------------------
if (-not $Python) {
    if (Test-Path -LiteralPath $venvPy) {
        $Python = $venvPy
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source }
    }
}
if (-not $Python) {
    Write-Host "ERROR: no python found (no .venv and none on PATH). Pass -Python <path>." -ForegroundColor Red
    exit 2
}
Write-Host "python : $Python"

# --- integrity pre-flight (catch stale-install shadowing) -------------------
Write-Section "Pre-flight: RES-054 install integrity"
$probe = Test-Res054Install -Py $Python
$probe.Out | ForEach-Object { Write-Host "  $_" }
if (-not $probe.Ok) {
    if ($NoInstall) {
        Stop-Stale -Py $Python -Probe $probe.Out
    }
    Write-Host "Stale/missing install detected - auto-setting up a repo .venv (Mode A)." -ForegroundColor Yellow

    # Bootstrap python for `venv` creation: never the (stale) .venv we are about
    # to (re)build - use -Python, else system `python` on PATH.
    $bootstrap = $null
    if ($PSBoundParameters.ContainsKey('Python') -and $Python) { $bootstrap = $Python }
    if (-not $bootstrap) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $bootstrap = $cmd.Source }
    }
    if (-not $bootstrap) { $bootstrap = $Python }

    if (-not (Test-Path -LiteralPath $venvPy)) {
        Write-Host "  creating venv: $bootstrap -m venv `"$repo\.venv`""
        & $bootstrap -m venv (Join-Path $repo '.venv')
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: venv creation failed." -ForegroundColor Red; exit 2 }
    }
    # Install the package editable WITH the dev extra (pytest + pytest-xdist) -
    # `pip install -e .` alone pulls only runtime deps, leaving "No module named
    # pytest". Run from the repo dir so the `.[dev]` extras spec resolves.
    Write-Host "  editable install (dev extras): $venvPy -m pip install -e `".[dev]`""
    Push-Location $repo
    try { & $venvPy -m pip install -e ".[dev]"; $rc = $LASTEXITCODE } finally { Pop-Location }
    if ($rc -ne 0) { Stop-Stale -Py $venvPy -Probe @('pip install -e .[dev] failed (offline / no build backend?)') }

    $Python = $venvPy
    Write-Host "python : $Python (repo .venv)"
    $probe = Test-Res054Install -Py $Python
    $probe.Out | ForEach-Object { Write-Host "  $_" }
    if (-not $probe.Ok) { Stop-Stale -Py $Python -Probe $probe.Out }
}
Write-Host "install OK - RES-054 symbols present." -ForegroundColor Green

# --- resolve CubeIDE (optional; smoke skips without it) ---------------------
if ($CubeIdePath) { $env:STM32CUBEIDE = $CubeIdePath }
if (-not $env:STM32CUBEIDE) {
    $globs = @(
        'C:\ST\STM32CubeIDE*\STM32CubeIDE\stm32cubeide.exe',
        'C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeIDE*\stm32cubeide.exe',
        'C:\Program Files (x86)\STMicroelectronics\STM32Cube\STM32CubeIDE*\stm32cubeide.exe'
    )
    foreach ($g in $globs) {
        $hit = Get-Item -Path $g -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $env:STM32CUBEIDE = $hit.FullName; break }
    }
}
if ($env:STM32CUBEIDE) {
    Write-Host "cubeide: $env:STM32CUBEIDE"
} else {
    Write-Host "cubeide: NOT FOUND - the smoke layer will SKIP. Pass -CubeIdePath or set `$env:STM32CUBEIDE to exercise the real build." -ForegroundColor Yellow
}

# --- layer 1: unit tests (always) ------------------------------------------
Write-Section "RES-054 unit tests (fast, no CubeIDE)"
& $Python -m pytest `
    (Join-Path $repo 'tests\test_cubeide_workspace.py') `
    (Join-Path $repo 'tests\test_cubeide_build.py') `
    -k 'WorkspaceReuse or ResetWorkspaceForImport or MissingLinkedFolders or MutationOrdering' `
    -q
$unitCode = $LASTEXITCODE

# --- layer 2: smoke test (real CubeIDE, or skip) ---------------------------
Write-Section "RES-054 smoke test (real CubeIDE -import/-build)"
& $Python -m pytest -m smoke (Join-Path $repo 'tests\test_cubeide_build_smoke.py') -v -rs
$smokeCode = $LASTEXITCODE

# --- summary ----------------------------------------------------------------
Write-Section "Summary"
Write-Host ("unit  layer: " + $(if ($unitCode -eq 0) { 'PASS' } else { "FAIL (exit $unitCode)" }))
Write-Host ("smoke layer: " + $(if ($smokeCode -eq 0) { 'PASS or SKIP' } else { "FAIL (exit $smokeCode)" }))

if ($unitCode -ne 0 -or $smokeCode -ne 0) { exit 1 }
exit 0
