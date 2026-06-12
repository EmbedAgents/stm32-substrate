"""``STM32CubeMX`` launcher resolution.

v1 is launcher-only — no jar fallback. Resolution order:

1. ``ctx.tools.cubemx_executable`` (explicit config / env override). The
   per-OS ``candidates.windows`` / ``candidates.linux`` block in the
   user's ``.claude/stm32-tools.local.jsonc`` is consumed during the
   ``SubstrateContext.from_environment`` load, so this field already
   reflects per-OS resolution per ADR-007.
2. ``shutil.which("STM32CubeMX")`` on PATH. On Windows, ``shutil.which``
   automatically resolves PATHEXT so ``STM32CubeMX.exe`` /
   ``.bat`` / ``.cmd`` are found without special-casing.
3. ``CubeMXLauncherError`` listing every attempt.

TODO(v1+): CubeIDE-bundled jar fallback via ``ctx.tools.cubemx_jar`` +
``ctx.tools.java_executable``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from embedagents.stm32.errors import CubeMXLauncherError

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext


def resolve_cubemx_launcher(ctx: "SubstrateContext") -> Path:
    """Return the absolute ``Path`` to the ``STM32CubeMX`` launcher.

    Raises ``CubeMXLauncherError`` listing every candidate examined when
    no launcher is resolvable.
    """
    checked: list[str] = []

    explicit = ctx.tools.cubemx_executable
    if explicit is not None:
        checked.append(str(explicit))
        if explicit.is_file():
            return explicit

    via_path = shutil.which("STM32CubeMX")
    if via_path:
        return Path(via_path)
    checked.append("$(which STM32CubeMX)")

    raise CubeMXLauncherError(
        message="STM32CubeMX launcher not resolvable",
        cubemx_marker=None,
        checked_candidates=tuple(checked),
        hint=(
            "Set cubemx.executable in .claude/stm32-tools.local.jsonc or "
            "STM32CUBEMX_PATH env var, or install STM32CubeMX so its "
            "launcher is on PATH. v1 does not fall back to the "
            "CubeIDE-bundled jar (TODO v1+)."
        ),
    )
