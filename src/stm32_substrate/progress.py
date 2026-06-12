"""Progress streaming primitives.

Per ``v1/api-conventions.md`` § "Progress streaming — ``on_progress``
callback". Plain callback shape; no async / observable.

Long-running ops (``read_flash_to_file``, ``tail_swo``, CubeMX generate,
hardware test runs) accept an ``on_progress`` callable. Default ``None``
is silent. Slash commands wire it to user-visible status messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProgressEvent:
    """One progress tick from a long-running substrate operation."""

    stage: str
    """Symbolic stage name (e.g. ``"flash.write"``, ``"cubemx_running"``)."""

    detail: str
    """Human-readable note (e.g. ``"wrote 1024/4096 bytes"``)."""

    fraction: float | None
    """``0.0..1.0`` if known; ``None`` if the op cannot estimate progress."""

    duration_s: float
    """Wall-clock seconds elapsed since the op started."""


ProgressCallback = Callable[[ProgressEvent], None]
