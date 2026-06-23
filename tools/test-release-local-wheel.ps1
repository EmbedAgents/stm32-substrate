#Requires -Version 5
<#
.SYNOPSIS
    PyPI-fidelity LOCAL release test for embedagents-stm32 on Windows. PowerShell
    mirror of tools/test-release-local-wheel.sh.

.DESCRIPTION
    WHY THIS EXISTS (vs. tools/test-release-windows.ps1):
        test-release-windows.ps1 installs from a git+https spec (needs a published
        tag) or editable -e (runs from source). Neither tests the actual built
        artifact. Editable installs in particular HIDE packaging bugs: pip install -e
        does not regenerate the console-script shim or dist metadata, so a renamed
        package can leave a stale stm32 shim importing a module that no longer exists
        (ModuleNotFoundError) - exactly what bit us after stm32_substrate ->
        embedagents.stm32. This script builds the REAL wheel from this dev tree
        (byte-identical to what PyPI would serve) and installs/runs/uninstalls it as an
        end user would - WITHOUT publishing to PyPI. Validate a staged version (e.g.
        0.3.1) over as many rounds as you like before the one-shot, immutable upload.

    INSTALL METHODS (-Method, all install the SAME built wheel; they differ in HOW the
    wheel is fetched, i.e. how close to a real 'pip install name==version'):

      A. pipx   (DEFAULT)  pipx install .\dist\*.whl
           + Real on-PATH user UX; matches the README's recommended Windows install
             (bare 'pip install' drops stm32.exe in a non-PATH Scripts dir). 'pipx
             uninstall' guarantees a clean removal - prevents the stale-shim bug above.
           - Runtime deps still come from real PyPI; does not exercise index resolution.

      B. venv   python -m venv + pip install --no-index --find-links dist name==ver
           + Resolves our package by name+version; fully offline/hermetic (this script
             pre-downloads the wheel's deps into the dist dir so --no-index works).
           - stm32 on PATH only while the venv is active; not the global UX.

      C. index  pypiserver over dist\ + pip install -i http://localhost name==ver
           + Highest fidelity: a true 'pip install embedagents-stm32==<ver>' against an
             index URL - exercises name+version resolution like PyPI.
           - Most setup (server lifecycle, port); deps still proxied from real PyPI.

    SCOPE: full user experience = pip package (the wheel) + Claude plugin channel.
        Plugin half needs the external 'claude' CLI (skipped if absent) and
        'claude plugin marketplace add' mutates GLOBAL plugin state (always torn down).
        Only install -> registration-at-<ver> -> uninstall is scriptable.

    Exit code is 0 only if every executed check passed.

.PARAMETER Method
    Install mechanism: pipx (default), venv, or index.

.PARAMETER NoPlugin
    Skip the Claude plugin channel (on by default).

.PARAMETER Project
    CubeIDE project dir for the -RunBuild smoke.

.PARAMETER RunBuild
    Run 'stm32 build <Project>' end-to-end (needs -Project + ST tools configured; also
    asserts the RES-050 workspace lands OUTSIDE the project tree).

.PARAMETER Keep
    Skip teardown - leave a usable installed CLI + registered plugin.

.EXAMPLE
    pwsh -File tools\test-release-local-wheel.ps1
.EXAMPLE
    pwsh -File tools\test-release-local-wheel.ps1 -Method venv -RunBuild -Project C:\path\to\STM32CubeIDE
#>
[CmdletBinding()]
param(
    [ValidateSet("pipx", "venv", "index")]
    [string]$Method = "pipx",
    [switch]$NoPlugin,
    [string]$Project = "",
    [switch]$RunBuild,
    [switch]$Keep
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot   # tools\ -> repo root
$WorkDir  = Join-Path $env:TEMP ("stm32-localwheel-" + [System.IO.Path]::GetRandomFileName().Substring(0, 8))
$DistDir  = Join-Path $WorkDir "dist"

# Staged version under test = this checkout's pyproject version.
$ExpectedVer = ""
$pyproject = Join-Path $RepoRoot "pyproject.toml"
if (Test-Path $pyproject) {
    $m = Select-String -Path $pyproject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $ExpectedVer = $m.Matches[0].Groups[1].Value }
}
$PluginVer = ""
$pluginJson = Join-Path $RepoRoot ".claude-plugin\plugin.json"
if (Test-Path $pluginJson) {
    $m = Select-String -Path $pluginJson -Pattern '"version"\s*:\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $PluginVer = $m.Matches[0].Groups[1].Value }
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

# --- teardown state ----------------------------------------------------------
$PipxInstalled = $false; $PluginInstalled = $false; $MarketplaceAdded = $false; $ServerProc = $null
$Stm32 = $null; $InstallPy = $null

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

try {
    # --- 1. Preflight --------------------------------------------------------
    Section "Preflight"
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
    Record "method = $Method" $true
    $claude = Get-Command claude -ErrorAction SilentlyContinue

    if ($Method -eq "pipx") {
        $hasPipx = [bool](Get-Command pipx -ErrorAction SilentlyContinue)
        Record "pipx available" $hasPipx
        if (-not $hasPipx) {
            Write-Host "`npipx not found; install it or rerun with -Method venv." -ForegroundColor Red
            exit 1
        }
    }

    # Build-tooling venv (isolated build + twine)
    $buildVenv = Join-Path $WorkDir "venv-build"
    & $PyExe @PyArgs -m venv $buildVenv
    $BPy = Join-Path $buildVenv "Scripts\python.exe"
    $log = & $BPy -m pip install --quiet --upgrade pip build twine 2>&1
    if ($LASTEXITCODE -eq 0) { Record "build tooling (build+twine)" $true }
    else { Record "build tooling (build+twine)" $false (($log | Select-Object -Last 3) -join " | "); exit 1 }

    # --- 2. Version gate -----------------------------------------------------
    Section "Version gate"
    if (-not $ExpectedVer) {
        Record "read pyproject version" $false "no version in pyproject.toml"
    } else {
        Record "pyproject version" $true $ExpectedVer
        Record "plugin.json == pyproject" ($PluginVer -eq $ExpectedVer) `
            $(if ($PluginVer -ne $ExpectedVer) { "plugin.json=$PluginVer vs pyproject=$ExpectedVer (version skew)" } else { $PluginVer })
    }

    # --- 3. Clean build ------------------------------------------------------
    Section "Clean build (python -m build)"
    Remove-Item -Recurse -Force (Join-Path $RepoRoot "build") -ErrorAction SilentlyContinue
    Push-Location $RepoRoot
    $log = & $BPy -m build --outdir $DistDir 2>&1
    $buildCode = $LASTEXITCODE
    Pop-Location
    $Wheel = (Get-ChildItem -Path $DistDir -Filter *.whl -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($buildCode -eq 0 -and $Wheel) {
        Record "python -m build" $true $Wheel.Name
    } else {
        Record "python -m build" $false (($log | Select-Object -Last 3) -join " | "); exit 1
    }

    # --- 4. Wheel gate (reuse the CI artifact assertions) --------------------
    Section "Wheel gate"
    $tw = & $BPy -m twine check (Join-Path $DistDir "*") 2>&1
    Record "twine check" ($LASTEXITCODE -eq 0) $(if ($LASTEXITCODE -ne 0) { ($tw | Select-Object -Last 3) -join " | " })

    $gate = Join-Path $WorkDir "wheel_gate.py"
    @'
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
'@ | Set-Content -Path $gate -Encoding ASCII
    $env:DISTDIR = $DistDir; $env:EXPECTED_VER = $ExpectedVer
    $gateOut = (& $BPy $gate 2>&1) -join " "
    Record "wheel contents (schemas/namespace/version)" ($gateOut -match "WHEEL_GATE_OK") `
        $(if ($gateOut -match "WHEEL_GATE_OK") { ($gateOut -replace ".*WHEEL_GATE_OK", "").Trim() } else { ($gateOut | Select-Object -Last 1) })

    # --- 5. Install (method-dependent) ---------------------------------------
    Section "Install (-Method $Method)"
    switch ($Method) {
        "pipx" {
            $log = & pipx install --force $Wheel.FullName 2>&1
            if ($LASTEXITCODE -eq 0) {
                $PipxInstalled = $true
                $binDir = (& pipx environment --value PIPX_BIN_DIR 2>$null)
                if (-not $binDir) { $binDir = Join-Path $env:USERPROFILE ".local\bin" }
                $pipxHome = (& pipx environment --value PIPX_HOME 2>$null)
                if (-not $pipxHome) { $pipxHome = Join-Path $env:USERPROFILE "pipx" }
                $Stm32 = Join-Path $binDir "stm32.exe"
                $InstallPy = Join-Path $pipxHome "venvs\embedagents-stm32\Scripts\python.exe"
                Record "pipx install wheel" $true $Wheel.Name
            } else {
                Record "pipx install wheel" $false (($log | Select-Object -Last 3) -join " | ")
            }
        }
        "venv" {
            $runVenv = Join-Path $WorkDir "venv-run"; & $PyExe @PyArgs -m venv $runVenv
            $InstallPy = Join-Path $runVenv "Scripts\python.exe"; $Stm32 = Join-Path $runVenv "Scripts\stm32.exe"
            & $InstallPy -m pip install --quiet --upgrade pip *> $null
            & $InstallPy -m pip download --quiet --dest $DistDir --find-links $DistDir "embedagents-stm32==$ExpectedVer" *> $null
            $log = & $InstallPy -m pip install --quiet --no-index --find-links $DistDir "embedagents-stm32==$ExpectedVer" 2>&1
            Record "venv install (--no-index --find-links)" ($LASTEXITCODE -eq 0) $(if ($LASTEXITCODE -ne 0) { ($log | Select-Object -Last 3) -join " | " })
        }
        "index" {
            if (-not (Get-Command pypi-server -ErrorAction SilentlyContinue)) {
                Skip "index install" "pypi-server not installed (pip install pypiserver)"
            } else {
                $port = if ($env:PORT) { $env:PORT } else { "8765" }
                $ServerProc = Start-Process -FilePath "pypi-server" `
                    -ArgumentList @("run", "-p", $port, "-i", "127.0.0.1", $DistDir) `
                    -PassThru -WindowStyle Hidden
                $probe = Join-Path $WorkDir "probe.py"
                "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$port/simple/',timeout=1)" |
                    Set-Content -Path $probe -Encoding ASCII
                $ready = $false
                for ($i = 0; $i -lt 30; $i++) {
                    & $PyExe @PyArgs $probe *> $null
                    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
                    Start-Sleep -Milliseconds 300
                }
                if (-not $ready) {
                    Record "local index server up" $false "server did not become ready"
                } else {
                    Record "local index server up" $true "127.0.0.1:$port"
                    $runVenv = Join-Path $WorkDir "venv-run"; & $PyExe @PyArgs -m venv $runVenv
                    $InstallPy = Join-Path $runVenv "Scripts\python.exe"; $Stm32 = Join-Path $runVenv "Scripts\stm32.exe"
                    & $InstallPy -m pip install --quiet --upgrade pip *> $null
                    $log = & $InstallPy -m pip install --quiet -i "http://127.0.0.1:$port/simple/" `
                        --extra-index-url https://pypi.org/simple --trusted-host 127.0.0.1 `
                        "embedagents-stm32==$ExpectedVer" 2>&1
                    Record "index install (pip -i localhost name==ver)" ($LASTEXITCODE -eq 0) $(if ($LASTEXITCODE -ne 0) { ($log | Select-Object -Last 3) -join " | " })
                }
            }
        }
    }

    # --- 6. CLI smoke --------------------------------------------------------
    Section "CLI smoke"
    if (-not $Stm32 -or -not (Test-Path $Stm32)) {
        Skip "stm32 --version" "no installed stm32 binary (install phase failed)"
    } else {
        $ver = (& $Stm32 --version 2>&1) -join " "
        Record "stm32 --version = $ExpectedVer" ($ver -match [regex]::Escape($ExpectedVer)) $ver
        if ($InstallPy -and (Test-Path $InstallPy)) {
            $chk = Join-Path $WorkDir "schema_check.py"
            @'
import json, importlib.resources as r
p = r.files("embedagents.stm32.schemas").joinpath("stm32-project.schema.json")
d = json.loads(p.read_text(encoding="utf-8"))
assert str(d.get("$id", "")).endswith("stm32-project.schema.json"), "unexpected $id"
print("SCHEMA_OK")
'@ | Set-Content -Path $chk -Encoding ASCII
            $schemaOut = (& $InstallPy $chk 2>&1) -join " "
            Record "bundled schemas load" ($schemaOut -match "SCHEMA_OK") $(if ($schemaOut -notmatch "SCHEMA_OK") { $schemaOut })
        }
        if ($RunBuild) {
            if (-not $Project) {
                Skip "stm32 build (end-to-end)" "-RunBuild needs -Project <dir>"
            } elseif (-not (Test-Path $Project)) {
                Skip "stm32 build (end-to-end)" "project dir not found: $Project"
            } else {
                # Run from the PROJECT dir: the real "build my project" scenario, and
                # the exact cwd==project-root case RES-050 was made for. Lets the
                # substrate's cwd-upward search find a .claude\stm32-tools.local.jsonc
                # (tools may also come from env/PATH); the in-tree-workspace assertion
                # below still proves RES-050 keeps the Eclipse workspace OUT of the tree.
                Push-Location $Project
                $buildOut = & $Stm32 build $Project 2>&1
                $bc = $LASTEXITCODE
                Pop-Location
                $bt = ($buildOut -join "`n")
                $errDetail = ([regex]::Matches($bt, '"(?:message|hint)": "[^"]*"') | ForEach-Object { $_.Value }) -join " "
                if (-not $errDetail) { $errDetail = ($buildOut | Select-Object -Last 3) -join " | " }
                Record "stm32 build (end-to-end)" ($bc -eq 0) $(if ($bc -ne 0) { $errDetail } else { ($buildOut | Select-String -Pattern "errors|warnings|Build of" | Select-Object -Last 1) })
                $inTree = Join-Path $Project ".stm32-substrate-workspace"
                Record "workspace kept out of project tree (RES-050)" (-not (Test-Path $inTree)) `
                    $(if (Test-Path $inTree) { "found $inTree" } else { "no in-tree .stm32-substrate-workspace" })
            }
        }
    }

    # --- 7. Plugin channel ---------------------------------------------------
    Section "Plugin channel"
    if ($NoPlugin) {
        Skip "plugin channel" "-NoPlugin"
    } elseif (-not $claude) {
        Skip "plugin channel" "claude CLI not on PATH"
    } else {
        $mkt = & claude plugin marketplace add $RepoRoot 2>&1
        if ($LASTEXITCODE -eq 0) {
            $MarketplaceAdded = $true; Record "marketplace add (local source)" $true
            $pin = & claude plugin install embedagents-stm32@embedagents 2>&1
            if ($LASTEXITCODE -eq 0) {
                $PluginInstalled = $true; Record "plugin install" $true
                $list = (& claude plugin list 2>&1) -join "`n"
                $nameOk = ($list -match "embedagents-stm32")
                # `claude plugin list` prints the plugin name but NOT its version, so
                # verify the version via the plugin cache path, which encodes
                # <marketplace>\<plugin>\<version>. Fall back to a version string in the
                # listing in case a future claude release starts printing one.
                $claudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE ".claude" }
                $verDir = Join-Path $claudeHome ("plugins\cache\embedagents\embedagents-stm32\" + $ExpectedVer)
                $verOk = (Test-Path $verDir) -or ($list -match [regex]::Escape($ExpectedVer))
                $regOk = $nameOk -and $verOk
                Record "plugin registered at $ExpectedVer" $regOk $(if (-not $regOk) { "name in list=$nameOk; cache '$verDir' exists=$([bool](Test-Path $verDir))" })
            } else {
                Record "plugin install" $false (($pin | Select-Object -Last 3) -join " | ")
            }
        } else {
            Record "marketplace add (local source)" $false (($mkt | Select-Object -Last 3) -join " | ")
        }
    }

    # --- 8. Summary ----------------------------------------------------------
    Section "Summary"
    $results | Format-Table -AutoSize Check, Status, Detail | Out-Host
    $script:Fails = ($results | Where-Object Status -eq "FAIL").Count
    $skips = ($results | Where-Object Status -eq "SKIP").Count
    Write-Host ""
    if ($script:Fails -eq 0) {
        Write-Host "RESULT: PASS ($($results.Count - $skips) checks, $skips skipped) - wheel $ExpectedVer" -ForegroundColor Green
    } else {
        Write-Host "RESULT: FAIL ($($script:Fails) failed, $skips skipped)" -ForegroundColor Red
    }
}
finally {
    if ($ServerProc) { try { Stop-Process -Id $ServerProc.Id -Force -ErrorAction SilentlyContinue } catch {} }
    if ($Keep) {
        Write-Host "`n--keep: leaving install + workdir in place" -ForegroundColor DarkGray
        Write-Host "  workdir: $WorkDir" -ForegroundColor DarkGray
        if ($PipxInstalled)  { Write-Host "  pipx app: embedagents-stm32 (stm32 on PATH)" -ForegroundColor DarkGray }
        if ($PluginInstalled) { Write-Host "  plugin: embedagents-stm32@embedagents registered" -ForegroundColor DarkGray }
    } else {
        Section "Teardown"
        if ($PluginInstalled)  { & claude plugin uninstall embedagents-stm32 *> $null; Write-Host "  removed plugin" }
        if ($MarketplaceAdded) { & claude plugin marketplace remove embedagents *> $null; Write-Host "  removed marketplace" }
        if ($PipxInstalled)    { & pipx uninstall embedagents-stm32 *> $null; Write-Host "  pipx uninstalled" }
        Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue
    }
}

exit $(if ($script:Fails -eq 0) { 0 } else { 1 })
