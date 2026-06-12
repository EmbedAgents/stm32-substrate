"""STM32CubeIDE headless-build wrapper.

Public surface: ``CubeIDE`` class + result dataclasses + the callable
type aliases. Maps the 14 atomic B-* prompts (B-001 / B-002 / B-004 …
B-014, B-018, B-019) onto two methods — ``build()`` (kwargs distinguish
all 12 build-shaped prompts) and ``find_project()`` (B-018 + B-019).

Out of this module:

- **B-003 / B-016 / CP-004 / CP-005** — multi-tool compounds; live in
  ``compound/``.
- **B-017** — dropped per P-037 (CubeMX scope cut).
- **B-010 / B-015 / B-020 / B-021** — T3 prompts deferred per M-014.

See ``v1/cubeide-api.md`` for the full method signatures + kwargs grid.
"""

from __future__ import annotations

from embedagents.stm32.cubeide.client import (
    AmbiguousCallback,
    ConflictCallback,
    CubeIDE,
    ExistingCallback,
)
from embedagents.stm32.cubeide.results import (
    BuildResult,
    FoundProject,
    HeadlessInvocation,
    SettingChange,
    SettingsModification,
)

__all__ = [
    "CubeIDE",
    "BuildResult",
    "FoundProject",
    "HeadlessInvocation",
    "SettingChange",
    "SettingsModification",
    "AmbiguousCallback",
    "ConflictCallback",
    "ExistingCallback",
]
