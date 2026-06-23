"""CubeIDE workspace-state detection + lock acquisition.

Per ``v1/cubeide-api.md`` § "Workspace state detection". All file locking
goes through ``embedagents.stm32.platform`` (ADR-005 — no inline ``fcntl``
in business logic).

Public surface:

- ``detect_workspace_lock(workspace)`` — ``True`` when CubeIDE GUI is
  holding the workspace.
- ``detect_project_imported(workspace, project_name)`` — returns the
  decoded ``.location`` path or ``None``.
- ``cleanup_stale_project(workspace, project_name, *, logger)`` —
  broader purge: project tree + every ``.projects/<name>/`` entry + stale
  ``.lock``; logs WARNING enumerating deletions.
- ``reset_workspace_for_import(workspace, *, logger)`` — wipe a
  substrate-owned workspace for a guaranteed-fresh CDT ``-import``,
  preserving only the held substrate lock (RES-054). Default path only.
- ``missing_linked_folders(project_path)`` — top-level in-tree virtual
  folders referenced by ``.project`` ``<link>``s that are absent on disk
  (the explicit-workspace staleness signal — RES-054).
- ``acquire_workspace_lock(workspace)`` — context manager; raises
  ``WorkspaceLockedError`` immediately on contention (HIL-mode M-019).
- ``default_workspace_root(project_path)`` — deterministic per-project
  Eclipse workspace under the persistent user-cache, out of the project
  tree (CDT's headless ``-import`` rejects an in-tree ``-data`` dir).
- ``workspace_nested_in_project(workspace, project_path)`` — ``True`` when
  the workspace is the project dir or a descendant (case-insensitive).
- ``headless_log_path(ctx, *, workspace=)`` — generates a timestamped log
  path under ``cubeide.log_dir`` (else ``<workspace>/logs``) for
  ``run_headless_build`` capture.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator
from urllib.parse import unquote

from embedagents.stm32.errors import WorkspaceLockedError
from embedagents.stm32.platform import (
    acquire_exclusive_lock,
    is_lock_held,
    user_cache_root,
)

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext


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
    if not path_str:
        return None
    # Eclipse writes file URIs as ``file:/C:/...`` (sometimes
    # ``file:///C:/...``) on Windows. The URI leading slash(es) before a
    # drive letter are not part of the filesystem path — without
    # stripping them, ``Path('/C:/...')`` never equals the descriptor's
    # ``Path('C:/...')`` and every build spuriously ran
    # ``cleanup_stale_project`` (IMP-06).
    m = re.match(r"^/+([A-Za-z]:[/\\].*)$", path_str)
    if m:
        path_str = m.group(1)
    return Path(path_str)


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
    log = logger or logging.getLogger("embedagents.stm32.cubeide.workspace")
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


def _remove_path(path: Path) -> None:
    """Best-effort recursive remove of a single entry (dir or file)."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def reset_workspace_for_import(
    workspace: Path, *, logger: logging.Logger | None = None
) -> None:
    """Wipe a substrate-owned workspace for a guaranteed-fresh CDT ``-import``.

    Removes every entry under ``workspace`` EXCEPT the held substrate lock
    file (``.metadata/.substrate-lock``) — so the caller may keep holding
    ``acquire_workspace_lock`` across the reset (on Windows that file is
    locked open and cannot be deleted anyway). This clears Eclipse's binary
    project-tree snapshot under ``.metadata/.plugins/`` — state that
    ``cleanup_stale_project`` cannot reach, which otherwise (a) makes
    ``-import`` fail with "already exists in the workspace" and (b) lets CDT
    re-save a project's ``.project`` dropping linked resources whose in-tree
    virtual folders were deleted (RES-054, amending RES-050).

    Called ONLY on the DEFAULT substrate-managed workspace, which holds
    exactly one project (RES-050). NEVER on a user-configured
    ``build.workspace`` — the substrate must not delete a dir the user owns.
    """
    if not workspace.is_dir():
        return
    log = logger or logging.getLogger("embedagents.stm32.cubeide.workspace")
    meta_name = _SUBSTRATE_LOCK_REL.parent.name  # ".metadata"
    keep_name = _SUBSTRATE_LOCK_REL.name  # ".substrate-lock"
    removed = 0
    for entry in workspace.iterdir():
        if entry.name == meta_name and entry.is_dir():
            for sub in entry.iterdir():
                if sub.name == keep_name:
                    continue
                _remove_path(sub)
                removed += 1
        else:
            _remove_path(entry)
            removed += 1
    if removed:
        log.info(
            "reset_workspace_for_import: cleared %d entr%s under %s for a "
            "fresh import (substrate lock preserved)",
            removed,
            "y" if removed == 1 else "ies",
            workspace,
        )


def missing_linked_folders(project_path: Path) -> list[str]:
    """Top-level in-tree virtual folders referenced by ``.project`` links
    that are absent on disk.

    STM32Cube example projects host their HAL/CMSIS/BSP sources as Eclipse
    *linked resources*: each ``<link>`` carries a ``<name>`` such as
    ``Drivers/STM32L4xx_HAL_Driver/stm32l4xx_hal.c`` whose first path
    segment (``Drivers``) is a *virtual folder* CDT materialises as a real
    (initially empty) directory in the project tree on ``-import``. If the
    user deletes that in-tree folder and a later build reuses a cached
    workspace (skips re-import), CDT re-saves ``.project`` dropping every
    ``<link>`` under the missing folder — silently stripping the project
    (RES-054). This returns the referenced top-level folders that are
    currently missing on disk (empty list = nothing to worry about).

    Reads only structured ``.project`` XML (ADR-004 — no interpretive
    parsing). Returns ``[]`` if ``.project`` is absent or unparseable.
    """
    project_xml = project_path / ".project"
    if not project_xml.is_file():
        return []
    try:
        tree = ET.parse(project_xml)
    except ET.ParseError:
        return []
    roots: list[str] = []
    seen: set[str] = set()
    for link in tree.getroot().iter("link"):
        name_el = link.find("name")
        if name_el is None or not name_el.text:
            continue
        first = name_el.text.strip().split("/", 1)[0].strip()
        if first and first not in seen:
            seen.add(first)
            roots.append(first)
    return [r for r in roots if not (project_path / r).exists()]


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


def default_workspace_root(project_path: Path) -> Path:
    """Deterministic per-project Eclipse workspace, out of the project tree.

    ``<user-cache>/stm32-substrate/workspaces/<basename>-<8 hex>`` keyed on
    the resolved (case-normalised) project path. Never inside the project
    tree, so CDT's headless ``-import`` is never asked to import a project
    whose location contains its own ``-data`` dir. Deterministic → the
    workspace's plugin state (and thus incremental-build speed) persists
    across runs; the short ``-<hash>`` suffix keeps the segment compact
    (Windows MAX_PATH) and neutralises reserved device names (CON/NUL/…).
    """
    resolved = project_path.resolve()
    key = os.path.normcase(str(resolved))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", resolved.name) or "project"
    return user_cache_root() / "workspaces" / f"{base}-{digest}"


def workspace_nested_in_project(workspace_path: Path, project_path: Path) -> bool:
    """``True`` when the workspace is the project dir or a descendant.

    Compared on ``os.path.normcase`` of the resolved paths so the check is
    case-insensitive and separator-normalised on Windows (``Path`` /
    ``is_relative_to`` compare case-sensitively even there). CDT rejects a
    headless ``-import`` whose project location encloses the ``-data`` dir.
    """
    ws = os.path.normcase(str(workspace_path.resolve()))
    proj = os.path.normcase(str(project_path.resolve()))
    return ws == proj or ws.startswith(proj + os.sep)


def headless_log_path(
    ctx: "SubstrateContext", *, workspace: "Path | None" = None
) -> Path:
    """Build a fresh timestamped log path under ``cubeide.log_dir``.

    When ``ctx.defaults.cubeide.log_dir`` is unset the logs follow the
    resolved Eclipse ``workspace`` (``<workspace>/logs/``); if no workspace
    is given they fall back to ``<ctx.cwd>/.stm32-substrate-workspace/logs/``.
    Directory is created if missing.
    """
    cubeide_defaults = getattr(ctx.defaults, "cubeide", None)
    raw_dir = (
        getattr(cubeide_defaults, "log_dir", None)
        if cubeide_defaults is not None
        else None
    )
    if raw_dir:
        log_dir = Path(raw_dir)
    elif workspace is not None:
        log_dir = workspace / "logs"
    else:
        log_dir = ctx.cwd / ".stm32-substrate-workspace" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return log_dir / f"build-{timestamp}.log"
