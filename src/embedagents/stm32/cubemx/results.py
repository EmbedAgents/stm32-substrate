"""``CubeMXResult`` + cubemx-local ``ProgressEvent``.

Per ``v1/cubemx-api.md`` § "Result type". Substrate captures, doesn't
interpret: ``log_path`` / ``cubemx_log_path`` are handed to the caller
verbatim; ``script_text`` is the audit copy of what substrate sent.

Cubemx defines its own ``ProgressEvent`` (different shape from
``embedagents.stm32.progress.ProgressEvent``) because the running-loop
emits substrate-clock-only fields — deadline + extensions_used — not
the generic ``fraction`` / ``detail`` pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProgressEvent:
    """Heartbeat emitted by the running-loop, once per poll-tick.

    Substrate clock only — no log content, no byte counts.
    """

    stage: str
    duration_s: float
    deadline_s: float
    extensions_used: int


@dataclass(frozen=True)
class CubeMXResult:
    """Outcome of ``CubeMX.generate()``.

    ``success`` is the canonical signal — derived from the marker-file
    + subprocess-settled observation, not from ``exit_code`` (which may
    be ``None`` when substrate terminated the subprocess after the
    marker appeared, per RES-020).
    """

    success: bool
    exit_code: int | None
    duration_s: float
    timed_out: bool
    extensions_used: int
    output_dir: Path | None
    log_path: Path
    cubemx_log_path: Path | None
    script_text: str
    terminated_after_marker: bool = False
