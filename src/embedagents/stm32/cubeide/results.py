"""CubeIDE-specific result dataclasses.

Per ``v1/cubeide-api.md`` § "Result types". Every shape is
``@dataclass(frozen=True)`` to match ADR-006.

Substrate captures, doesn't interpret (ADR-004): ``BuildResult`` carries
``log_path`` + ``console_output`` + ``artifact_path`` + ``map_path`` —
not a typed parse of the build output. Callers (Claude, T3 wrappers,
slash commands) read the raw text and decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SettingChange:
    """One ``<option>`` edit applied to a ``.cproject`` file.

    ``kind`` distinguishes the three primitive edit shapes the protocol
    supports. ``old_value`` is ``None`` when the option didn't previously
    exist (e.g. a new ``-D`` symbol being appended onto an empty list).
    """

    superclass_id: str
    configuration: str
    kind: Literal["set_value", "append_list", "remove_list"]
    old_value: str | tuple[str, ...] | None
    new_value: str | tuple[str, ...]


@dataclass(frozen=True)
class SettingsModification:
    """Audit trail for one ``CProjectEditor`` invocation.

    Returned on every ``BuildResult`` that applied a settings edit.
    ``rolled_back=True`` only when a protocol-level failure triggered
    rollback; build-level failure (compile / link errors after a valid
    protocol edit) keeps the change and leaves ``rolled_back=False``.
    """

    file: Path
    backup_path: Path | None
    changes: list[SettingChange]
    rolled_back: bool = False


@dataclass(frozen=True)
class BuildResult:
    """Returned by every ``CubeIDE.build()`` call.

    ``success`` is ``exit_code == 0`` EXCEPT a zero-exit "Nothing to
    build for project" run that produced no artifact, which is a failure
    (RES-034); an up-to-date incremental rebuild also prints "Nothing to
    build" but keeps its artifact, so it stays a success. Build-level
    failures (compile / link errors) surface here as ``success=False``
    with the error text in ``console_output`` / ``log_path``;
    substrate-side failures (workspace locked, headless script missing,
    etc.) raise ``CubeIDEError`` instead.
    """

    success: bool
    exit_code: int
    duration_s: float
    log_path: Path
    console_output: str
    artifact_path: Path | None
    map_path: Path | None
    project_name: str
    configuration: str
    workspace_path: Path
    settings_modification: SettingsModification | None = None
    project_imported: bool = False


@dataclass(frozen=True)
class FoundProject:
    """Result of ``find_project()``.

    ``candidates_considered`` carries every match the search saw — useful
    for surfacing "we picked X but also saw Y and Z" in caller / log
    output without forcing the caller to re-walk the filesystem.
    """

    path: Path
    name: str
    cproject_path: Path
    candidates_considered: tuple[Path, ...] = ()


@dataclass(frozen=True)
class HeadlessInvocation:
    """Arguments for one ``headless-build.sh`` invocation.

    Built by ``CubeIDE.build()`` and consumed by
    ``headless.run_headless_build``. Separating the data shape from the
    execution helper makes the build flow testable without spawning
    subprocesses.
    """

    project_name: str
    configuration: str
    workspace: Path
    project_path: Path | None = None
    clean: bool = False
    extra_args: tuple[str, ...] = ()
