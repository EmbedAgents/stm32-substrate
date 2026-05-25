"""CubeIDE workspace-state detection + lock acquisition.

Per the CubeIDE API spec § "Workspace state detection". All file locking
goes through ``stm32_substrate.platform`` (ADR-005 — no inline ``fcntl``
in business logic).

Public surface:

- ``detect_workspace_lock(workspace)`` — ``True`` when CubeIDE GUI is
  holding the workspace.
- ``detect_project_imported(workspace, project_name)`` — returns the
  decoded ``.location`` path or ``None``.
- ``cleanup_stale_project(workspace, project_name, *, logger)`` —
  broader purge: project tree + every ``.projects/<name>/`` entry + stale
  ``.lock``; logs WARNING enumerating deletions.
- ``acquire_workspace_lock(workspace)`` — context manager; raises
  ``WorkspaceLockedError`` immediately on contention (HIL-mode M-019).
- ``headless_log_path(ctx)`` — generates a timestamped log path under
  ``cubeide.log_dir`` for ``run_headless_build`` capture.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import unquote

from stm32_substrate.errors import WorkspaceLockedError
from stm32_substrate.platform import (
    acquire_exclusive_lock,
    is_lock_held,
)

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext


_GUI_LOCK_REL = Path(".metadata") / ".lock"
_SUBSTRATE_LOCK_REL = Path(".metadata") / ".substrate-lock"


def detect_workspace_lock(workspace: Path) -> bool:
    """``True`` when CubeIDE GUI holds the workspace's ``.metadata/.lock``.

    Eclipse writes a zero-byte ``.metadata/.lock`` file and holds an
    advisory ``fcntl`` lock on it for the lifetime of the GUI session.
    The probe is non-destructive — we just check whether another holder
    has the lock; the file lingering on disk without a holder counts as
    NOT locked (stale `.lock` is cleaned up by ``cleanup_stale_project``).
    """
    lock_path = workspace / _GUI_LOCK_REL
    return is_lock_held(lock_path)


def detect_project_imported(workspace: Path, project_name: str) -> Path | None:
    """Return the decoded ``.location`` URI or ``None`` if not imported.

    Eclipse stores the project's source-tree path at
    ``<ws>/.metadata/.plugins/org.eclipse.core.resources/.projects/<name>/.location``
    as a URI-encoded ``URI//file:<path>`` blob. We only need the path
    component — substrate compares it against the descriptor's
    ``project_path`` to decide whether to re-import.

    For v1 simple-now, the decoder finds the first ``file:`` URI in the
    binary blob and URI-decodes the rest. TODO: full Eclipse Resources
    URI parser if more sophisticated path layouts appear.
    """
    location = (
        workspace
        / ".metadata"
        / ".plugins"
        / "org.eclipse.core.resources"
        / ".projects"
        / project_name
        / ".location"
    )
    if not location.is_file():
        return None
    try:
        raw = location.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    marker = "file:"
    idx = text.find(marker)
    if idx < 0:
        return None
    rest = text[idx + len(marker) :]
    # Take until the first non-path-friendly control byte (NUL / SOH …).
    end = len(rest)
    for i, ch in enumerate(rest):
        if ord(ch) < 0x20:
            end = i
            break
    path_str = unquote(rest[:end]).rstrip()
    return Path(path_str) if path_str else None


def cleanup_stale_project(
    workspace: Path, project_name: str, *, logger: logging.Logger | None = None
) -> None:
    """Broader purge of one project's stale workspace state.

    Per spec § "Workspace strategy" / "Cleanup":

    - ``<ws>/<project_name>/`` directory (project's own tree)
    - Every ``<ws>/.metadata/.plugins/*/.projects/<project_name>/`` entry
    - ``<ws>/.metadata/.lock`` only if NOT held (already verified by
      ``detect_workspace_lock`` in the caller)

    Other projects' state under ``.metadata/`` is untouched.

    Logs a WARNING enumerating every path being removed so the user can
    audit a posteriori.
    """
    log = logger or logging.getLogger("stm32_substrate.cubeide.workspace")
    deletions: list[Path] = []

    project_tree = workspace / project_name
    if project_tree.exists():
        deletions.append(project_tree)

    plugins_root = workspace / ".metadata" / ".plugins"
    if plugins_root.is_dir():
        for plugin_dir in plugins_root.iterdir():
            if not plugin_dir.is_dir():
                continue
            stale = plugin_dir / ".projects" / project_name
            if stale.exists():
                deletions.append(stale)

    gui_lock = workspace / _GUI_LOCK_REL
    if gui_lock.exists() and not is_lock_held(gui_lock):
        deletions.append(gui_lock)

    if not deletions:
        return

    log.warning(
        "cleanup_stale_project: removing %d path(s) for %r:\n  %s",
        len(deletions),
        project_name,
        "\n  ".join(str(p) for p in deletions),
    )
    for path in deletions:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as ex:
            log.warning("cleanup_stale_project: failed to remove %s (%s)", path, ex)


@contextlib.contextmanager
def acquire_workspace_lock(workspace: Path) -> Iterator[None]:
    """Acquire ``<ws>/.metadata/.substrate-lock`` (non-blocking).

    Raises ``WorkspaceLockedError`` immediately on contention — HIL-mode
    M-019 forbids waiting. The contended case typically means another
    substrate build is in-flight; the user waits for it to finish before
    retrying.

    The CubeIDE GUI's own lock (``.metadata/.lock``) is a separate concern
    — callers check ``detect_workspace_lock`` *before* calling this.
    """
    lock_path = workspace / _SUBSTRATE_LOCK_REL
    try:
        with acquire_exclusive_lock(lock_path):
            yield
    except BlockingIOError as ex:
        raise WorkspaceLockedError(
            message=(
                "another substrate build is in progress on this workspace"
            ),
            cubeide_marker="workspace-locked",
            workspace_path=workspace,
            hint=(
                "wait for the in-flight substrate build to finish, then "
                "retry; substrate does not auto-wait for locks (HIL mode)"
            ),
        ) from ex


def headless_log_path(ctx: "SubstrateContext") -> Path:
    """Build a fresh timestamped log path under ``cubeide.log_dir``.

    Default ``log_dir`` (when ``ctx.defaults.cubeide.log_dir`` is unset)
    is ``<ctx.cwd>/.stm32-substrate-workspace/logs/``. Directory is
    created if missing.
    """
    cubeide_defaults = getattr(ctx.defaults, "cubeide", None)
    raw_dir = (
        getattr(cubeide_defaults, "log_dir", None)
        if cubeide_defaults is not None
        else None
    )
    if raw_dir:
        log_dir = Path(raw_dir)
    else:
        log_dir = ctx.cwd / ".stm32-substrate-workspace" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"build-{timestamp}.log"
