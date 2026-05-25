"""``CubeMX`` — thin wrapper around STM32CubeMX ``-q <script>``.

Inline-script helpers (``_quote`` + ``EXIT_COMMAND``) live at module
scope so the script-text audit in ``CubeMXResult.script_text`` is
byte-stable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TYPE_CHECKING

from stm32_substrate.cubemx import runner
from stm32_substrate.cubemx.launcher import resolve_cubemx_launcher
from stm32_substrate.cubemx.results import CubeMXResult
from stm32_substrate.errors import CubeMXError

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.progress import ProgressCallback


# ---------------------------------------------------------------------------
# Script-construction helpers (module-level for testability + byte stability)
# ---------------------------------------------------------------------------


_FORBIDDEN_SCRIPT_CHARS: tuple[str, ...] = ('"',)
"""CubeMX's ``-q`` script parser accepts simple double-quoted strings
but has no documented escape syntax for embedded ``"`` — substrate
refuses that character at the boundary rather than guessing at vendor
escaping rules. Backslash (``\\``) is no longer refused: Windows paths
contain backslashes natively, and CubeMX (Java-based) accepts both
``C:\\foo\\bar`` and ``C:/foo/bar`` interchangeably on Windows. Per
RES-020 + ADR-007, ``_quote()`` normalises backslashes to forward
slashes so substrate-generated scripts are platform-uniform."""


EXIT_COMMAND = "exit_mx"
"""Hardcoded permanently per RES-016 / Q14 — no config knob."""


def _quote(value: str) -> str:
    """Wrap ``value`` in double quotes if it contains a space; refuse
    forbidden characters with a loud ``ValueError``.

    Per RES-020: CubeMX's script parser accepts simple double-quoted
    strings but has no escape mechanism. The boundary refusal is the
    safe choice — caller fixes the path / value.

    Windows-side backslashes are normalised to forward slashes (CubeMX's
    Java parser handles both transparently) so the produced script is
    platform-uniform and the substrate's test fixtures stay LF-clean
    without escaping.
    """
    normalised = value.replace("\\", "/")
    for bad in _FORBIDDEN_SCRIPT_CHARS:
        if bad in normalised:
            raise ValueError(
                f"value {value!r} contains unsupported character {bad!r}; "
                f"CubeMX script parser has no escape syntax for it."
            )
    return f'"{normalised}"' if " " in normalised else normalised


# ---------------------------------------------------------------------------
# CubeMX class — one verb (generate)
# ---------------------------------------------------------------------------


class CubeMX:
    """Wrapper around STM32CubeMX ``-q <script>``. One per SubstrateContext.

    Stateless: each ``generate()`` call is a one-shot
    spawn → poll-marker → terminate cycle. No persistent worker, no
    session_state slot.
    """

    def __init__(self, ctx: "SubstrateContext") -> None:
        self.ctx = ctx
        self._launcher: Path | None = None  # resolved lazily on generate()
        self._log = ctx.logger.getChild("cubemx")

    def generate(
        self,
        ioc_path: Path | None = None,
        *,
        output_path: Path | None = None,
        project_name: str | None = None,
        toolchain: Literal["STM32CubeIDE"] = "STM32CubeIDE",
        timeout_s: float | None = None,
        on_progress: "ProgressCallback | None" = None,
    ) -> CubeMXResult:
        """MX-001 / CP-008 — open IOC, generate project code.

        Resolution order for path args:

        - ``ioc_path=None`` falls back to ``ctx.project.cubemx.ioc_path``;
          if neither is set, raises ``ValueError``.
        - ``output_path=None`` falls back to ``ctx.project.cubemx.output_path``;
          if neither, defaults to ``ioc_path.parent``.
        - ``project_name=None`` falls back to ``ctx.project.cubemx.project_name``;
          if neither, defaults to ``ioc_path.stem``.

        Validation runs entirely substrate-side before invoking the
        launcher:

        - ``ioc_path.is_file()`` and suffix ``.ioc`` → else
          ``CubeMXError(cubemx_marker="ioc-missing")``.
        - ``toolchain`` runtime guard (v1 only ``STM32CubeIDE``).
        - Output-path existence is NOT pre-checked (HIL carve-out per
          spec § "Module-specific HIL carve-out") — CubeMX overwrites in
          place; that's the iterative-regen safety net.
        """
        resolved_ioc = self._resolve_ioc_path(ioc_path)
        resolved_output = self._resolve_output_path(output_path, resolved_ioc)
        resolved_name = self._resolve_project_name(project_name, resolved_ioc)
        self._guard_toolchain(toolchain)

        # IOC existence + suffix check.
        if not resolved_ioc.is_file() or resolved_ioc.suffix.lower() != ".ioc":
            raise CubeMXError(
                message=f"IOC file not found or wrong suffix: {resolved_ioc}",
                cubemx_marker="ioc-missing",
                ioc_path=resolved_ioc,
                output_dir=resolved_output,
                hint=(
                    "verify the IOC path exists and ends in .ioc; pass "
                    "ioc_path= explicitly or set cubemx.ioc_path in "
                    "stm32-project.jsonc"
                ),
                recoverable=True,
            )

        resolved_output.mkdir(parents=True, exist_ok=True)

        if self._launcher is None:
            self._launcher = resolve_cubemx_launcher(self.ctx)

        script_text = _build_script(
            ioc_path=resolved_ioc,
            output_path=resolved_output,
            project_name=resolved_name,
            toolchain=toolchain,
        )
        # CubeMX double-nests with the STM32CubeIDE toolchain:
        #   <output>/<name>/                  - project source tree
        #                                       (.mxproject, Drivers/, Inc/, Src/)
        #   <output>/<name>/<toolchain>/      - Eclipse project root
        #                                       (.cproject, .project, .settings/)
        # The marker (.cproject) lives in the toolchain subdir, which
        # is what callers want as ``output_dir`` since downstream tools
        # (CubeIDE.build(project=...)) operate on the Eclipse root.
        project_dir = resolved_output / resolved_name / toolchain
        marker = project_dir / ".cproject"

        self._log.info(
            "generate ioc=%s output=%s name=%s toolchain=%s project_dir=%s",
            resolved_ioc,
            resolved_output,
            resolved_name,
            toolchain,
            project_dir,
        )

        return runner.run_cubemx(
            launcher=self._launcher,
            script_text=script_text,
            expected_marker=marker,
            output_dir=project_dir,
            ctx=self.ctx,
            timeout_s=timeout_s,
            on_progress=on_progress,
        )

    # ------------------------------------------------------------------
    # resolution helpers
    # ------------------------------------------------------------------

    def _resolve_ioc_path(self, ioc_path: Path | None) -> Path:
        if ioc_path is not None:
            return ioc_path.resolve()
        descriptor = self.ctx.project
        cubemx_block = getattr(descriptor, "cubemx", None) if descriptor else None
        configured = (
            getattr(cubemx_block, "ioc_path", None) if cubemx_block else None
        )
        if not configured:
            raise ValueError(
                "ioc_path= not given and cubemx.ioc_path is unset in "
                "stm32-project.jsonc"
            )
        return Path(configured).resolve()

    def _resolve_output_path(
        self, output_path: Path | None, ioc_path: Path
    ) -> Path:
        if output_path is not None:
            return output_path.resolve()
        descriptor = self.ctx.project
        cubemx_block = getattr(descriptor, "cubemx", None) if descriptor else None
        configured = (
            getattr(cubemx_block, "output_path", None) if cubemx_block else None
        )
        if configured:
            return Path(configured).resolve()
        return ioc_path.parent

    def _resolve_project_name(
        self, project_name: str | None, ioc_path: Path
    ) -> str:
        if project_name is not None:
            return project_name
        descriptor = self.ctx.project
        cubemx_block = getattr(descriptor, "cubemx", None) if descriptor else None
        configured = (
            getattr(cubemx_block, "project_name", None) if cubemx_block else None
        )
        return configured or ioc_path.stem

    @staticmethod
    def _guard_toolchain(toolchain: str) -> None:
        if toolchain not in ("STM32CubeIDE",):
            raise ValueError(
                f"v1 supports toolchain='STM32CubeIDE' only; got {toolchain!r}"
            )


# ---------------------------------------------------------------------------
# Inline script builder — module-level for testability
# ---------------------------------------------------------------------------


def _build_script(
    *,
    ioc_path: Path,
    output_path: Path,
    project_name: str,
    toolchain: str,
) -> str:
    """Construct the CubeMX ``-q`` script as a multi-line string.

    Paths are ``.resolve()``-d to absolute by caller. ``_quote()`` is
    applied to every value so spaces don't break the parser and
    forbidden chars trigger the loud ValueError up front.
    """
    return "\n".join(
        [
            f"config load {_quote(str(ioc_path))}",
            f"project name {_quote(project_name)}",
            f"project path {_quote(str(output_path))}",
            f"project toolchain {_quote(toolchain)}",
            "project generate",
            EXIT_COMMAND,
        ]
    ) + "\n"
