"""Headless build script resolution + invocation.

Per ``v1/cubeide-api.md`` § "Headless build invocation". Single shared
entry point — ``CubeIDE.build()`` is responsible for assembling the
``HeadlessInvocation`` shape and then routing through here.

Per ADR-007: Linux uses ``headless-build.sh`` next to the CubeIDE
binary; Windows uses ``headless-build.bat``. CubeIDE installers ship
both as siblings of the launcher executable; substrate dispatches by
``sys.platform`` at resolution time.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from stm32_substrate.cubeide.results import HeadlessInvocation
from stm32_substrate.cubeide.workspace import headless_log_path
from stm32_substrate.errors import CubeIDEError
from stm32_substrate.subprocess_runner import ToolRunResult, run_tool

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.progress import ProgressCallback


def _headless_script_name() -> str:
    """Per-OS canonical filename of the CubeIDE headless build wrapper."""
    return "headless-build.bat" if sys.platform == "win32" else "headless-build.sh"


def resolve_headless_build(*, ctx: "SubstrateContext") -> Path:
    """Resolve the per-OS headless build script.

    Order:

    1. ``ctx.tools.cubeide_headless_build`` (explicit override, any extension).
    2. ``<cubeide_path>/headless-build.sh`` on Linux, ``…\\headless-build.bat``
       on Windows.
    3. Loud ``CubeIDEError(cubeide_marker="headless-script-missing")``.

    TODO(v1+): direct ``-application`` invocation if wrapper missing on
    Snap / Flatpak installs.
    """
    explicit = ctx.tools.cubeide_headless_build
    if explicit is not None and explicit.is_file():
        return explicit

    cubeide_bin = ctx.tools.cubeide_path
    if cubeide_bin is None:
        raise CubeIDEError(
            message="STM32CubeIDE path not configured",
            cubeide_marker="headless-script-missing",
            hint=(
                "set cubeide.path in .claude/stm32-tools.local.jsonc or "
                "STM32CUBEIDE env var; the headless build script "
                f"({_headless_script_name()}) is then auto-resolved next to "
                "the binary"
            ),
        )
    script_name = _headless_script_name()
    candidate = cubeide_bin.parent / script_name
    if candidate.is_file():
        return candidate

    raise CubeIDEError(
        message=f"{script_name} not found next to {cubeide_bin}",
        cubeide_marker="headless-script-missing",
        hint=(
            "set cubeide.cubeide_headless_build in "
            ".claude/stm32-tools.local.jsonc to override; expected default is "
            f"<cubeide_install>/{script_name}"
        ),
    )


def run_headless_build(
    inv: HeadlessInvocation,
    *,
    ctx: "SubstrateContext",
    on_progress: "ProgressCallback | None" = None,
    headless_path: Path | None = None,
) -> tuple[ToolRunResult, Path]:
    """Single subprocess invocation of ``headless-build.sh``.

    Returns the ``ToolRunResult`` and the captured ``log_path``. Substrate
    does NOT parse the captured output; ``CubeIDE.build()`` wraps the
    result into a ``BuildResult`` with ``success = (exit_code == 0)``.

    ``on_progress`` is accepted but not yet wired — header parsing of
    Eclipse build phases is fragile (per spec TODO). v1 falls back to
    INFO-level start/end milestones only.
    """
    log = ctx.logger.getChild("cubeide.headless")
    headless = headless_path if headless_path is not None else resolve_headless_build(ctx=ctx)

    args: list[str] = [
        "-data",
        str(inv.workspace),
    ]
    if inv.project_path is not None:
        # Eclipse's HeadlessBuilder.importProject calls EFS.getStore on
        # the URI form of the path. On Windows, a raw "C:/foo/bar"
        # string is parsed as a URI with scheme "C" (the drive letter
        # masquerades as URI scheme), which has no registered file
        # system and raises:
        #
        #   org.eclipse.core.runtime.CoreException:
        #     No file system is defined for scheme: C
        #
        # The fix is to pass the canonical RFC-8089 file URI form
        # ("file:///C:/foo/bar"). pathlib's Path.as_uri() handles both
        # OSes correctly: Windows yields "file:///C:/..." and Linux
        # yields "file:///path/...".
        #
        # Caught bench-driven 2026-05-20 against H747I-DISCO nested
        # dual-core project (FPU_Fractal CM7); the L476 BLINKY case
        # had been silently masked by the "retry without -import"
        # workaround in client.py.
        args.extend(["-import", inv.project_path.as_uri()])
    args.append("-cleanBuild" if inv.clean else "-build")
    args.append(f"{inv.project_name}/{inv.configuration}")
    args.extend(inv.extra_args)

    log_path = headless_log_path(ctx)
    timeout_s = _headless_timeout_s(ctx)

    log.info(
        "headless-build start project=%s configuration=%s clean=%s",
        inv.project_name,
        inv.configuration,
        inv.clean,
    )

    # raise_on_nonzero=False — the caller (build()) decides whether a
    # non-zero exit code is a hard failure (substrate-side) or a
    # successful capture of a build-level failure (compile errors).
    result = run_tool(
        headless,
        args,
        ctx=ctx,
        timeout_s=timeout_s,
        log_path=log_path,
        raise_on_nonzero=False,
    )
    log.info(
        "headless-build end exit_code=%s duration_s=%.2f",
        result.exit_code,
        result.duration_s,
    )
    return result, log_path


def _headless_timeout_s(ctx: "SubstrateContext") -> float:
    cubeide_defaults = getattr(ctx.defaults, "cubeide", None)
    if cubeide_defaults is None:
        return 600.0
    return float(getattr(cubeide_defaults, "headless_timeout_s", 600))
