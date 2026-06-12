"""Vendor-CLI ``--version`` smoke tests (F.5.1).

One test per vendor CLI: invoke the binary with its version flag (or
the closest "I'm alive" probe when ``--version`` isn't supported),
assert exit 0 (or expected-error code for help-style probes), and
verify a sentinel string appears in stdout/stderr.

These are deliberately lightweight — they prove the CLI is installed,
runnable, and producing recognisable output. They don't exercise any
substrate code path beyond ``ctx.tools.<x>`` path resolution, so
they're cheap to run as a "did my install of the vendor stack
basically work" check.

Coverage matches the substrate's 6 vendor wrappers (per CLAUDE.md):
  - cubeprogrammer: STM32_Programmer_CLI --version
  - signing:        STM32_SigningTool_CLI --version
  - debug (gdb):    arm-none-eabi-gdb --version
  - debug (gdbsrv): ST-LINK_gdbserver --version
  - cubeide:        headless-build.bat (no args → USAGE banner)

CubeMX already has a heavier ``@pytest.mark.smoke`` covered by
``test_cubemx_hardware.py::TestGenerate`` (full project generation —
proves the launcher resolves AND the JVM can run AND the IOC parser
works). No ``--version`` probe needed for CubeMX in this layer.

Excluded from the default ``pytest`` run; invoke with
``pytest -m smoke``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

if False:  # TYPE_CHECKING guard kept terse
    from embedagents.stm32.context import SubstrateContext


_VERSION_TIMEOUT_S = 15.0
_USAGE_TIMEOUT_S = 30.0  # cubeide headless-build cold-starts the JVM


def _run(argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with merged stdout+stderr, text-mode, bounded timeout."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


# ---------------------------------------------------------------------------
# STM32_Programmer_CLI
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCubeProgrammerVersion:
    def test_version_flag_returns_zero_with_version_line(
        self, smoke_ctx: "SubstrateContext"
    ) -> None:
        cli = smoke_ctx.tools.cube_programmer_cli
        if cli is None:
            pytest.skip("STM32_Programmer_CLI not resolvable on this host")
        result = _run([str(cli), "--version"], timeout_s=_VERSION_TIMEOUT_S)
        assert result.returncode == 0, (
            f"--version exit code {result.returncode}; stdout: {result.stdout!r}; "
            f"stderr: {result.stderr!r}"
        )
        combined = (result.stdout or "") + (result.stderr or "")
        assert "STM32CubeProgrammer" in combined, (
            f"expected 'STM32CubeProgrammer' banner; got: {combined[:300]!r}"
        )


# ---------------------------------------------------------------------------
# STM32_SigningTool_CLI
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSigningToolVersion:
    def test_version_flag_returns_zero_with_version_line(
        self, smoke_ctx: "SubstrateContext"
    ) -> None:
        cli = smoke_ctx.tools.stm32_signing_tool_cli
        if cli is None:
            pytest.skip("STM32_SigningTool_CLI not resolvable on this host")
        result = _run([str(cli), "--version"], timeout_s=_VERSION_TIMEOUT_S)
        assert result.returncode == 0, (
            f"--version exit code {result.returncode}; stdout: {result.stdout!r}; "
            f"stderr: {result.stderr!r}"
        )
        combined = (result.stdout or "") + (result.stderr or "")
        assert "STM32 Signing Tool" in combined or "Signing Tool" in combined, (
            f"expected 'Signing Tool' banner; got: {combined[:300]!r}"
        )


# ---------------------------------------------------------------------------
# arm-none-eabi-gdb (standard GNU --version)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestArmGdbVersion:
    def test_version_flag_returns_zero_with_gnu_banner(
        self, smoke_ctx: "SubstrateContext"
    ) -> None:
        cli = smoke_ctx.tools.arm_gdb
        if cli is None:
            pytest.skip("arm-none-eabi-gdb not resolvable on this host")
        result = _run([str(cli), "--version"], timeout_s=_VERSION_TIMEOUT_S)
        assert result.returncode == 0, (
            f"--version exit code {result.returncode}; stdout: {result.stdout!r}; "
            f"stderr: {result.stderr!r}"
        )
        assert "GNU gdb" in result.stdout, (
            f"expected 'GNU gdb' banner; got: {result.stdout[:300]!r}"
        )


# ---------------------------------------------------------------------------
# ST-LINK_gdbserver
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestGdbServerVersion:
    def test_version_flag_returns_zero_with_version_line(
        self, smoke_ctx: "SubstrateContext"
    ) -> None:
        cli = smoke_ctx.tools.stlink_gdbserver
        if cli is None:
            pytest.skip("ST-LINK_gdbserver not resolvable on this host")
        result = _run([str(cli), "--version"], timeout_s=_VERSION_TIMEOUT_S)
        assert result.returncode == 0, (
            f"--version exit code {result.returncode}; stdout: {result.stdout!r}; "
            f"stderr: {result.stderr!r}"
        )
        combined = (result.stdout or "") + (result.stderr or "")
        assert "version" in combined.lower(), (
            f"expected 'version' line; got: {combined[:300]!r}"
        )


# ---------------------------------------------------------------------------
# STM32CubeIDE (headless-build no-args USAGE banner)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCubeIDEHeadlessUsage:
    """STM32CubeIDE has no clean ``--version`` flag (Eclipse-based; the
    launcher just opens the GUI). The closest "I'm alive" probe is
    invoking ``headless-build.bat`` (or ``.sh``) with no arguments,
    which prints the Eclipse headlessbuild USAGE banner and exits
    non-zero. That confirms the launcher script + the JVM + the
    managedbuilder.headlessbuild application all initialise correctly —
    which is the entire substrate dependency for cubeide.build().
    """

    def test_headless_build_no_args_prints_usage(
        self, smoke_ctx: "SubstrateContext", tmp_path: Path
    ) -> None:
        from embedagents.stm32.cubeide.headless import resolve_headless_build
        from embedagents.stm32.errors import CubeIDEError

        try:
            cli = resolve_headless_build(ctx=smoke_ctx)
        except CubeIDEError as exc:
            pytest.skip(
                f"headless-build script not resolvable on this host: {exc.message}"
            )
        # Run from a tmp_path cwd so any Eclipse droppings (workspace
        # state, .metadata, lock files) land in the per-test tmp dir
        # and not in the repo root.
        result = subprocess.run(
            [str(cli)],
            capture_output=True,
            text=True,
            timeout=_USAGE_TIMEOUT_S,
            check=False,
            cwd=tmp_path,
        )
        # Eclipse headlessbuild exits non-zero (typically -1 / 255 on
        # Windows) when invoked with no args. Substrate just needs the
        # launcher to produce the USAGE banner — exit code is
        # incidental as long as the banner appears.
        combined = (result.stdout or "") + (result.stderr or "")
        assert "Usage:" in combined or "headlessbuild" in combined.lower(), (
            f"expected USAGE banner; exit={result.returncode}; "
            f"output: {combined[:500]!r}"
        )
