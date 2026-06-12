"""``CubeIDE`` — STM32CubeIDE headless-build wrapper.

Skeleton (C1a). Both public methods declare their signatures + docstrings
and raise ``NotImplementedError``. Subsequent C1 sub-phases fill in the
bodies (workspace state, ``.cproject`` editor, headless invocation,
build orchestration, find_project discovery, CLI).

Two public methods only — every B-* prompt is a kwargs combination on
``build()`` per the single-verb consolidation rule.
"""

from __future__ import annotations

import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Literal, TYPE_CHECKING

from stm32_substrate.cubeide import headless, presets, workspace
from stm32_substrate.resolution import coerce_path
from stm32_substrate.cubeide.cproject import CProjectEditor
from stm32_substrate.cubeide.results import (
    BuildResult,
    FoundProject,
    HeadlessInvocation,
)
from stm32_substrate.errors import (
    ConfigurationError,
    CProjectEditError,
    CubeIDEError,
    ProjectAmbiguityError,
    WorkspaceLockedError,
)

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.progress import ProgressCallback


# Public callable type aliases — mirrored on the package ``__init__``.
ConflictCallback = Callable[[str, str, str], Literal["replace", "skip", "abort"]]
"""``(target_field, existing_value, new_value) -> "replace" | "skip" | "abort"``."""

ExistingCallback = Callable[[Path], Literal["replace", "skip", "rename"]]
"""``(existing_path) -> "replace" | "skip" | "rename"`` — fires when
``add_libraries`` / ``add_sources`` would overwrite an existing file."""

AmbiguousCallback = Callable[[list[Path]], Path]
"""``(candidates) -> picked_path`` — fires from ``find_project`` (B-018 /
B-019) when multiple ``.cproject`` paths match."""


def _unique_destination(path: Path) -> Path:
    """First non-existing ``<stem>-<n><suffix>`` sibling (on_existing='rename')."""
    for i in range(1, 1000):
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise CProjectEditError(
        message=f"could not find a free rename target near {path}",
        hint="clean up the numbered copies or pass an explicit (src, dest)",
    )


def _lib_ref(lib_path: Path) -> str:
    """Render a ``.a`` archive path as a CubeIDE ``Libraries (-l)`` entry.

    A standard ``lib<name>.a`` becomes ``<name>`` (so the linker emits
    ``-l<name>``). An archive without the ``lib`` prefix (common for ST's
    bundled runtimes, e.g. ``NetworkRuntime1100_CM55_GCC.a``) becomes
    ``:<filename>`` — GNU ld's literal-name form (``-l:<filename>``), which
    resolves the exact file against the ``-L`` search paths.
    """
    name = lib_path.name
    m = re.fullmatch(r"lib(.+)\.a", name)
    return m.group(1) if m else f":{name}"


class CubeIDE:
    """Wrapper around STM32CubeIDE headless build (Eclipse CDT).

    One instance per ``SubstrateContext``. Workspace lifecycle is
    implicit: each ``build()`` call ensures the configured workspace is
    lock-free and import-clean before invoking ``headless-build.sh``.

    Two-method public surface:

    - ``build(**kwargs)`` — every B-* prompt (B-001..B-014).
    - ``find_project(folder, name=None, on_ambiguous=None)`` — B-018 /
      B-019 project discovery.
    """

    def __init__(self, ctx: "SubstrateContext") -> None:
        self.ctx = ctx
        self._cubeide: Path | None = ctx.tools.cubeide_path
        self._headless: Path | None = ctx.tools.cubeide_headless_build
        self._log = ctx.logger.getChild("cubeide")

    # ------------------------------------------------------------------
    # helpers (used by per-method bodies in C1b+)
    # ------------------------------------------------------------------

    def _require_cubeide(self) -> Path:
        """Return the validated CubeIDE binary path or raise loudly."""
        if self._cubeide is None:
            raise ConfigurationError(
                message="STM32CubeIDE path not configured",
                hint=(
                    "Set cubeide.path in .claude/stm32-tools.local.jsonc, "
                    "or set the STM32CUBEIDE environment variable. "
                    "Auto-discovery on PATH attempted; binary not found."
                ),
            )
        return self._cubeide

    # ------------------------------------------------------------------
    # build() — every B-* prompt as kwargs
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        # project / configuration resolution
        project: Path | None = None,
        configuration: str | None = None,
        clean: bool = False,
        # single-option compiler-flag edits (B-005 / B-006)
        debug_level: Literal["none", "-g1", "-g", "-g3"] | None = None,
        optimization: Literal[
            "-O0", "-O1", "-O2", "-O3", "-Og", "-Os", "-Ofast", "-Oz"
        ]
        | None = None,
        # multi-option presets (B-007 / B-008 / B-009)
        preset: Literal["fast", "size", "balanced"] | None = None,
        # structural list edits (B-011..B-014)
        add_symbols: list[str | tuple[str, str]] | None = None,
        add_libraries: list[Path] | None = None,
        add_sources: list[Path | tuple[Path, Path]] | None = None,
        add_include_paths: list[str] | None = None,
        # configuration scoping
        modify_all_configurations: bool | None = None,
        # callbacks
        on_conflict: ConflictCallback | None = None,
        on_existing: ExistingCallback | None = None,
        on_progress: "ProgressCallback | None" = None,
    ) -> BuildResult:
        """Run a headless CubeIDE build, optionally applying ``.cproject``
        edits first.

        See ``v1/cubeide-api.md`` § "Prompt → kwargs mapping" for the
        14-prompt grid. ``preset`` is exclusive with ``debug_level`` /
        ``optimization`` — passing both raises ``ValueError``. Multiple
        edit kwargs can combine in one call; they land in one
        ``.cproject`` snapshot, one validation, one build.

        Commit / rollback rule:

        - Protocol-level failure (snapshot / parse / modify / validate XML)
          → substrate rolls back the ``.cproject`` from the backup and
          raises ``CProjectEditError``.
        - Build-level failure (compile / link errors after a valid edit)
          → substrate keeps the ``.cproject`` change and returns a
          ``BuildResult(success=False, ...)``.

        Substrate does NOT parse the build output (ADR-004 — substrate
        captures, doesn't interpret). The returned ``BuildResult`` carries
        ``log_path`` / ``console_output`` / ``artifact_path`` / ``map_path``
        — callers / Claude read the raw text and decide.
        """
        # ---- validate kwargs ----
        if preset is not None and (debug_level is not None or optimization is not None):
            raise ValueError(
                "preset is exclusive with debug_level/optimization"
            )
        # A fully-formed CDT enum value (".value." in it) passes through
        # verbatim — the documented escape hatch in presets.*_ALIASES.
        # Anything else must be a known alias: an unmapped value would be
        # written into .cproject verbatim and the build would report
        # success while CubeIDE ignores or chokes on the option.
        if (
            debug_level is not None
            and debug_level not in presets.DEBUG_LEVEL_ALIASES
            and ".value." not in debug_level
        ):
            raise ValueError(
                f"invalid debug_level {debug_level!r}: expected one of "
                f"{sorted(presets.DEBUG_LEVEL_ALIASES)} "
                "or a fully-formed CDT enum value (…debuglevel.value.<token>)"
            )
        if (
            optimization is not None
            and optimization not in presets.OPTIMIZATION_ALIASES
            and ".value." not in optimization
        ):
            raise ValueError(
                f"invalid optimization {optimization!r}: expected one of "
                f"{sorted(presets.OPTIMIZATION_ALIASES)} "
                "or a fully-formed CDT enum value (…optimization.level.value.<token>)"
            )

        # ---- resolve project + configuration ----
        project_path = self._resolve_project_path(project)
        config_name = self._resolve_configuration(configuration)
        workspace_path = self._resolve_workspace()
        project_name = self._resolve_project_name(project_path)
        all_configs = self._resolve_all_configurations(modify_all_configurations)

        # ---- decide whether any settings edits land in this build ----
        edits_requested = (
            debug_level is not None
            or optimization is not None
            or preset is not None
            or add_symbols
            or add_libraries
            or add_sources
            or add_include_paths
        )

        # ---- workspace lifecycle ----
        # Everything that mutates the workspace — stale-project cleanup,
        # directory creation, .cproject edits, the build itself — runs
        # under the substrate lock. Cleanup used to run BEFORE the lock,
        # so a concurrent invocation could purge metadata out from under
        # a running build and only then raise WorkspaceLockedError
        # (IMP-10). The GUI-lock pre-check lives inside too: it must be
        # serialized against the cleanup that may delete a stale .lock.
        settings_modification = None
        with workspace.acquire_workspace_lock(workspace_path):
            if workspace.detect_workspace_lock(workspace_path):
                raise WorkspaceLockedError(
                    message="CubeIDE GUI is holding this workspace",
                    cubeide_marker="workspace-locked",
                    workspace_path=workspace_path,
                    project_name=project_name,
                    configuration=config_name,
                    hint="close STM32CubeIDE GUI on this workspace, then re-run",
                )

            imported_loc = workspace.detect_project_imported(
                workspace_path, project_name
            )
            if imported_loc is not None and imported_loc != project_path:
                workspace.cleanup_stale_project(
                    workspace_path, project_name, logger=self._log
                )
                imported_loc = None
            needs_import = imported_loc is None
            if needs_import:
                workspace_path.mkdir(parents=True, exist_ok=True)

            # ---- apply .cproject edits inside the substrate-lock ----
            if edits_requested:
                settings_modification = self._apply_settings(
                    project_path=project_path,
                    configuration=config_name,
                    all_configurations=all_configs,
                    debug_level=debug_level,
                    optimization=optimization,
                    preset=preset,
                    add_symbols=add_symbols,
                    add_libraries=add_libraries,
                    add_sources=add_sources,
                    add_include_paths=add_include_paths,
                    on_conflict=on_conflict,
                    on_existing=on_existing,
                )

            # ---- invoke headless build ----
            inv = HeadlessInvocation(
                project_name=project_name,
                configuration=config_name,
                workspace=workspace_path,
                project_path=project_path if needs_import else None,
                clean=clean,
            )
            start = time.monotonic()
            result, log_path = headless.run_headless_build(
                inv, ctx=self.ctx, on_progress=on_progress
            )

            # ---- retry without -import on the "already exists" path ----
            # Eclipse's binary `.projects.workspace.tree` state survives
            # substrate's cleanup_stale_project pass (which only nukes
            # per-project plugin state). When detect_project_imported
            # said "not imported" but Eclipse's hidden tree says
            # otherwise, the -import fails with "Project ... already
            # exists in the workspace!" and the build aborts. The fix:
            # detect that specific exit + retry without -import. Single
            # retry only — no loop, no escalation.
            if (
                result.exit_code != 0
                and needs_import
                and "already exists in the workspace" in (result.stdout + result.stderr)
            ):
                self._log.info(
                    "build: project %r already known to workspace; retrying "
                    "without -import",
                    project_name,
                )
                retry_inv = HeadlessInvocation(
                    project_name=project_name,
                    configuration=config_name,
                    workspace=workspace_path,
                    project_path=None,
                    clean=clean,
                )
                result, log_path = headless.run_headless_build(
                    retry_inv, ctx=self.ctx, on_progress=on_progress
                )
                # Reflect the actual import state on the result below.
                needs_import = False

            duration_s = time.monotonic() - start

        # ---- build result + locate artifacts ----
        artifact_path, map_path = self._locate_artifacts(
            project_path=project_path,
            configuration=config_name,
            project_name=project_name,
        )
        console_output = result.stdout + (
            "\n--- stderr ---\n" + result.stderr if result.stderr else ""
        )
        # A headless run can exit 0 yet build nothing: Eclipse CDT prints
        # "Nothing to build for project" both on an up-to-date incremental
        # rebuild (artifact already present → genuine success) AND when it
        # imports a project whose managed-build config it can't drive — e.g.
        # a legacy SW4STM32/AC6 toolchain CubeIDE lacks the plugin for, which
        # produces no artifact. So a zero-exit "Nothing to build" run that
        # located no artifact is a failure, not a silent success. (Caught
        # 2026-05-25 bringing up the SW4STM32 L0/L1 boards.)
        nothing_built = "Nothing to build" in console_output
        success = result.exit_code == 0 and not (nothing_built and artifact_path is None)
        self._log.info(
            "build %s project=%s configuration=%s duration=%.2fs log=%s",
            "succeeded" if success else "failed",
            project_name,
            config_name,
            duration_s,
            log_path,
        )
        return BuildResult(
            success=success,
            exit_code=result.exit_code,
            duration_s=duration_s,
            log_path=log_path,
            console_output=console_output,
            artifact_path=artifact_path,
            map_path=map_path,
            project_name=project_name,
            configuration=config_name,
            workspace_path=workspace_path,
            settings_modification=settings_modification,
            project_imported=needs_import,
        )

    def find_project(
        self,
        folder: Path | None = None,
        *,
        name: str | None = None,
        on_ambiguous: AmbiguousCallback | None = None,
    ) -> FoundProject:
        """Search 0..2 levels under ``folder`` for ``.cproject`` files."""
        search_root = coerce_path(folder) if folder is not None else self.ctx.cwd.resolve()
        cproject_paths = sorted(_search_cproject(search_root, max_depth=2))

        if not cproject_paths:
            raise CubeIDEError(
                message=f"no .cproject found under {search_root}",
                cubeide_marker="no-project-found",
                hint=(
                    "check the folder path and that the project directory "
                    "contains a .cproject file at depth <= 2"
                ),
            )

        candidates = tuple(cproject_paths)

        if name is not None:
            return self._pick_named(
                name=name,
                candidates=candidates,
                on_ambiguous=on_ambiguous,
                search_root=search_root,
            )

        if len(candidates) == 1:
            return _build_found_project(candidates[0], candidates)

        # B-018 multi-match path.
        if on_ambiguous is not None:
            picked = on_ambiguous([Path(p) for p in candidates])
            if picked not in candidates:
                raise ProjectAmbiguityError(
                    message=(
                        f"on_ambiguous returned {picked!r}, which is not "
                        f"one of the discovered candidates"
                    ),
                    candidates=candidates,
                )
            return _build_found_project(picked, candidates)

        raise ProjectAmbiguityError(
            message=(
                f"{len(candidates)} .cproject files found under {search_root}; "
                "pass name= or on_ambiguous= to disambiguate"
            ),
            candidates=candidates,
        )

    # ------------------------------------------------------------------
    # build() helpers
    # ------------------------------------------------------------------

    def _resolve_project_path(self, project: Path | None) -> Path:
        if project is not None:
            return self._resolve_explicit_project(project)
        descriptor = self.ctx.project
        if descriptor is None:
            raise ConfigurationError(
                message="no project descriptor found; pass project= explicitly",
                hint="set build.project_path in stm32-project.jsonc",
            )
        configured = self._descriptor_project_path()
        if configured is None:
            raise ConfigurationError(
                message="project= not given and build.project_path is unset",
                hint=(
                    "pass project=Path('...') to build(), or set "
                    "build.project_path in stm32-project.jsonc"
                ),
            )
        return configured

    def _resolve_explicit_project(self, project: Path) -> Path:
        """Resolve an explicit ``project=`` path to an importable project.

        Eclipse's headless ``-import`` requires a ``.project`` file
        directly in the imported directory. A path that has one is used
        as-is. A path that doesn't (typically the repo root of an
        ST-example-shaped tree, where the Eclipse project nests several
        levels down) resolves through the descriptor: if
        ``build.project_path`` lands strictly under the given path and is
        itself importable, the intent is unambiguous — build that
        (logged at INFO). Anything else raises loud with a hint
        (HIL rule: no guessing).
        """
        explicit = coerce_path(project)  # str|Path tolerated (IMP-22)
        if (explicit / ".project").is_file():
            return explicit
        if not explicit.exists():
            raise ConfigurationError(
                message=f"project path {explicit} does not exist",
                hint="pass the directory that contains the project's .project file",
            )
        configured = self._descriptor_project_path()
        if (
            configured is not None
            and configured != explicit
            and configured.is_relative_to(explicit)
            and (configured / ".project").is_file()
        ):
            self._log.info(
                "build: %s has no .project; descriptor resolves project to %s",
                explicit,
                configured,
            )
            return configured
        if configured is not None:
            hint = (
                f"the project descriptor resolves build.project_path to "
                f"{configured} — omit project= to use it, or pass the "
                "directory containing the .project file"
            )
        else:
            hint = (
                "pass the directory that contains the project's .project "
                "file, or set build.project_path in stm32-project.jsonc "
                "and omit project="
            )
        raise ConfigurationError(
            message=(
                f"{explicit} contains no .project — not an importable "
                "CubeIDE project"
            ),
            hint=hint,
        )

    def _descriptor_project_path(self) -> Path | None:
        """``build.project_path`` from the descriptor, or ``None``.

        Relative paths in the descriptor anchor to the project root
        (ctx.cwd), NOT the process CWD. The descriptor is part of the
        project tree, so its paths are most naturally read as "relative
        to where this descriptor lives". Without this, running pytest
        from a different directory than the project root produces a
        broken path.
        """
        descriptor = self.ctx.project
        build_block = getattr(descriptor, "build", None) if descriptor else None
        configured = getattr(build_block, "project_path", None) if build_block else None
        if not configured:
            return None
        configured_path = Path(configured)
        if not configured_path.is_absolute():
            configured_path = self.ctx.cwd / configured_path
        return configured_path.resolve()

    def _resolve_configuration(self, configuration: str | None) -> str:
        if configuration is not None:
            return configuration
        descriptor = self.ctx.project
        if descriptor is None:
            return "Debug"
        build_block = getattr(descriptor, "build", None)
        default = (
            getattr(build_block, "default_configuration", None) if build_block else None
        )
        return default or "Debug"

    def _resolve_workspace(self) -> Path:
        descriptor = self.ctx.project
        build_block = getattr(descriptor, "build", None) if descriptor else None
        configured = (
            getattr(build_block, "workspace", None) if build_block else None
        )
        if configured:
            # Same anchor-to-ctx.cwd rule as _resolve_project_path: a
            # relative workspace in the descriptor reads against the
            # project root, not the process CWD.
            workspace_path = Path(configured)
            if not workspace_path.is_absolute():
                workspace_path = self.ctx.cwd / workspace_path
            return workspace_path.resolve()
        return (self.ctx.cwd / ".stm32-substrate-workspace").resolve()

    def _resolve_project_name(self, project_path: Path) -> Path:
        """Extract ``<name>`` from the project's ``.project`` XML file."""
        project_xml = project_path / ".project"
        if not project_xml.is_file():
            # No .project file → derive from directory name as fallback.
            return project_path.name
        try:
            tree = ET.parse(project_xml)
        except ET.ParseError:
            return project_path.name
        name_el = tree.getroot().find("name")
        if name_el is not None and name_el.text:
            return name_el.text.strip()
        return project_path.name

    def _resolve_all_configurations(self, kwarg: bool | None) -> bool:
        if kwarg is not None:
            return kwarg
        descriptor = self.ctx.project
        build_block = getattr(descriptor, "build", None) if descriptor else None
        return bool(getattr(build_block, "modify_all_configurations", False)) if build_block else False

    def _apply_settings(
        self,
        *,
        project_path: Path,
        configuration: str,
        all_configurations: bool,
        debug_level: str | None,
        optimization: str | None,
        preset: str | None,
        add_symbols: list | None,
        add_libraries: list | None,
        add_sources: list | None,
        add_include_paths: list | None,
        on_conflict: ConflictCallback | None = None,
        on_existing: ExistingCallback | None = None,
    ):
        """Run the CProjectEditor protocol for one ``build()`` call."""
        editor = CProjectEditor(project_path, logger=self._log)
        editor.snapshot()
        try:
            if preset is not None:
                self._apply_preset(
                    editor, preset, configuration, all_configurations
                )
            if debug_level is not None:
                editor.set_option(
                    superclass=r".*\.compiler\.option\.debuglevel",
                    value=presets.DEBUG_LEVEL_ALIASES.get(
                        debug_level, debug_level
                    ),
                    configuration=configuration if not all_configurations else None,
                    all_configurations=all_configurations,
                )
            if optimization is not None:
                editor.set_option(
                    superclass=r".*\.compiler\.option\.optimization\.level",
                    value=presets.OPTIMIZATION_ALIASES.get(
                        optimization, optimization
                    ),
                    configuration=configuration if not all_configurations else None,
                    all_configurations=all_configurations,
                )
            if add_symbols:
                symbols_superclass = r".*\.compiler\.option\.definedsymbols"
                for sym in add_symbols:
                    rendered = sym if isinstance(sym, str) else f"{sym[0]}={sym[1]}"
                    change = editor.append_list_value(
                        # Real CubeIDE: ...c.compiler.option.definedsymbols.
                        # (The old ...preprocessor.def.symbols regex never
                        # matched real ST output — see RES note in plan.)
                        superclass=symbols_superclass,
                        value=rendered,
                        configuration=configuration if not all_configurations else None,
                        all_configurations=all_configurations,
                    )
                    # ARC-02: same symbol already defined with a DIFFERENT
                    # value → on_conflict decides; no callback → raise
                    # (ambiguity raises, per HIL).
                    self._resolve_symbol_conflict(
                        editor,
                        change,
                        rendered,
                        superclass=symbols_superclass,
                        on_conflict=on_conflict,
                        configuration=configuration,
                        all_configurations=all_configurations,
                    )
            if add_include_paths:
                for inc in add_include_paths:
                    editor.append_list_value(
                        # Real CubeIDE: ...c.compiler.option.includepaths
                        # (plural). The singular ``includepath`` regex
                        # failed re.fullmatch against real ST output.
                        superclass=r".*\.compiler\.option\.includepaths",
                        value=str(inc),
                        configuration=configuration if not all_configurations else None,
                        all_configurations=all_configurations,
                    )
            if add_libraries:
                for lib in add_libraries:
                    lib_path = Path(lib)
                    # Append the search dir (-L) and the lib reference (-l)
                    # per spec ("append paths+libs lists"). Real CubeIDE
                    # linker options: ...linker.option.libraries (the -l
                    # list) + ...linker.option.directories (the -L list).
                    parent = lib_path.parent
                    if str(parent) not in ("", "."):
                        editor.append_list_value(
                            superclass=r".*\.linker\.option\.directories",
                            value=str(parent),
                            configuration=configuration if not all_configurations else None,
                            all_configurations=all_configurations,
                        )
                    editor.append_list_value(
                        superclass=r".*\.linker\.option\.(?:libs|libraries)",
                        value=_lib_ref(lib_path),
                        configuration=configuration if not all_configurations else None,
                        all_configurations=all_configurations,
                    )
                    editor.track_aux(lib_path)
            # add_sources: copy each source into the project's scanned
            # source tree so the managed build's catch-all sourcePath picks
            # it up (per spec: "copy → CDT-mode-aware optional sourceEntries
            # edit"). A bare Path copies to ``<project>/<name>``; a
            # ``(src, dest)`` tuple copies to ``<project>/<dest>`` (dest
            # relative to the project dir, or absolute). The copy is the
            # load-bearing step; we also un-exclude the basename from any
            # sourceEntries ``excluding`` list so a previously-excluded
            # file builds.
            if add_sources:
                for source in add_sources:
                    if isinstance(source, tuple):
                        src_path, dest = Path(source[0]), Path(source[1])
                    else:
                        src_path, dest = Path(source), Path(Path(source).name)
                    dest_abs = dest if dest.is_absolute() else project_path / dest
                    # ARC-02: an existing destination must not be silently
                    # overwritten — on_existing decides; no callback → raise.
                    if dest_abs.exists():
                        decision = (
                            on_existing(dest_abs)
                            if on_existing is not None
                            else None
                        )
                        if decision is None:
                            raise CProjectEditError(
                                message=(
                                    f"add_sources destination already "
                                    f"exists: {dest_abs}"
                                ),
                                hint=(
                                    "pass on_existing=... returning "
                                    "'replace' / 'skip' / 'rename', or "
                                    "pick a different (src, dest) target"
                                ),
                                recoverable=True,
                            )
                        if decision == "skip":
                            continue
                        if decision == "rename":
                            dest_abs = _unique_destination(dest_abs)
                        elif decision != "replace":
                            raise ValueError(
                                f"on_existing returned invalid decision "
                                f"{decision!r}; expected 'replace' / "
                                "'skip' / 'rename'"
                            )
                    # IMP-09: a missing source / unwritable tree raised a
                    # raw FileNotFoundError/OSError straight through the
                    # protocol try-block (which catches CProjectEditError
                    # only — so it also skipped rollback).
                    try:
                        dest_abs.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_path, dest_abs)
                    except OSError as ex:
                        raise CProjectEditError(
                            message=(
                                f"add_sources copy failed: {src_path} → "
                                f"{dest_abs}: {ex}"
                            ),
                            hint=(
                                "check that the source file exists and "
                                "the project tree is writable"
                            ),
                        ) from ex
                    editor.track_aux(dest_abs)
                    editor.unexclude_source(dest_abs.name)
            editor.write_and_validate()
            editor.commit()
        except CProjectEditError:
            editor.rollback()
            raise
        return editor.snapshot_record()

    def _resolve_symbol_conflict(
        self,
        editor: CProjectEditor,
        change,
        rendered: str,
        *,
        superclass: str,
        on_conflict: ConflictCallback | None,
        configuration: str,
        all_configurations: bool,
    ) -> None:
        """ARC-02 — gate ``add_symbols`` against an already-defined symbol.

        ``change.old_value`` carries the pre-append list. A pre-existing
        entry with the same symbol name but a different value is a
        conflict: ``on_conflict(name, existing, requested)`` decides
        'replace' (drop the old entry) / 'skip' (drop the new one) /
        'abort'; no callback raises (ambiguity raises, per HIL). All
        edits are in-memory — a raise rolls the whole protocol back.
        """
        name = rendered.split("=", 1)[0]
        old_values = (
            change.old_value if isinstance(change.old_value, tuple) else ()
        )
        config_kwarg = None if all_configurations else configuration
        for existing in old_values:
            if existing == rendered or existing.split("=", 1)[0] != name:
                continue
            decision = (
                on_conflict(name, existing, rendered)
                if on_conflict is not None
                else None
            )
            if decision == "replace":
                editor.remove_list_value(
                    superclass=superclass,
                    value=existing,
                    configuration=config_kwarg,
                    all_configurations=all_configurations,
                )
            elif decision == "skip":
                editor.remove_list_value(
                    superclass=superclass,
                    value=rendered,
                    configuration=config_kwarg,
                    all_configurations=all_configurations,
                )
            elif decision is None or decision == "abort":
                raise CProjectEditError(
                    message=(
                        f"symbol {name!r} already defined as {existing!r}; "
                        f"requested {rendered!r}"
                        + (
                            " (on_conflict returned 'abort')"
                            if decision == "abort"
                            else ""
                        )
                    ),
                    hint=(
                        "pass on_conflict=... returning 'replace' or "
                        "'skip', or remove the conflicting symbol first"
                    ),
                    recoverable=True,
                )
            else:
                raise ValueError(
                    f"on_conflict returned invalid decision {decision!r}; "
                    "expected 'replace' / 'skip' / 'abort'"
                )

    def _apply_preset(
        self,
        editor: CProjectEditor,
        preset: str,
        configuration: str,
        all_configurations: bool,
    ) -> None:
        ops = presets.PRESETS.get(preset)
        if ops is None:
            raise ValueError(f"unknown preset {preset!r}")
        config_kwarg = None if all_configurations else configuration
        for kind, superclass, value in ops:
            if kind in ("set_value", "set_value_soft"):
                editor.set_option(
                    superclass=superclass,
                    value=value,
                    configuration=config_kwarg,
                    all_configurations=all_configurations,
                    required=(kind == "set_value"),
                )
            elif kind == "append_list":
                editor.append_list_value(
                    superclass=superclass,
                    value=value,
                    configuration=config_kwarg,
                    all_configurations=all_configurations,
                )
            else:  # remove_list
                editor.remove_list_value(
                    superclass=superclass,
                    value=value,
                    configuration=config_kwarg,
                    all_configurations=all_configurations,
                )
        # Preset "fast" appends FPU flags per family table (MR-1).
        if preset == "fast":
            self._apply_fpu_flags(editor, configuration, all_configurations)

    def _apply_fpu_flags(
        self,
        editor: CProjectEditor,
        configuration: str,
        all_configurations: bool,
    ) -> None:
        descriptor = self.ctx.project
        firmware = getattr(descriptor, "firmware", None) if descriptor else None
        family = getattr(firmware, "device_family", None) if firmware else None
        fpu = presets.fpu_flags_for_family(family)
        if fpu is None:
            self._log.warning(
                "preset='fast' without a matched firmware.device_family: "
                "assuming soft-FP. Set firmware.device_family in "
                "stm32-project.jsonc for FPU-aware builds (current value: %r)",
                family,
            )
            return
        fpu_value, abi_value = fpu
        config_kwarg = None if all_configurations else configuration
        # Real CubeIDE places fpu/floatabi under
        # ``com.st...managedbuild.option.fpu`` (not under the .compiler.
        # tool subtree). The ``...`` value prefix gets spliced with the
        # matched option's superClass in CProjectEditor._edit_option.
        editor.set_option(
            superclass=r".*\.managedbuild\.option\.fpu",
            value=f"....value.{fpu_value}",
            configuration=config_kwarg,
            all_configurations=all_configurations,
        )
        editor.set_option(
            superclass=r".*\.managedbuild\.option\.floatabi",
            value=f"....value.{abi_value}",
            configuration=config_kwarg,
            all_configurations=all_configurations,
        )

    def _pick_named(
        self,
        *,
        name: str,
        candidates: tuple[Path, ...],
        on_ambiguous: AmbiguousCallback | None,
        search_root: Path,
    ) -> FoundProject:
        # Read each candidate's .project / dir name to disambiguate.
        named: list[tuple[Path, str]] = []
        for cproject in candidates:
            named.append((cproject, _read_project_name(cproject)))

        exact = [path for path, n in named if n == name]
        if exact:
            return _build_found_project(exact[0], candidates)

        substring = [path for path, n in named if name.lower() in n.lower()]
        if not substring:
            raise CubeIDEError(
                message=f"no .cproject matching name {name!r} under {search_root}",
                cubeide_marker="no-project-found",
                hint="check the project name or run discovery without name= to list candidates",
            )
        if len(substring) == 1:
            self._log.warning(
                "find_project: substring match (no exact match) — picked %s for name=%r",
                substring[0],
                name,
            )
            return _build_found_project(substring[0], candidates)
        if on_ambiguous is not None:
            picked = on_ambiguous([Path(p) for p in substring])
            if picked not in substring:
                raise ProjectAmbiguityError(
                    message=f"on_ambiguous returned {picked!r}, not a substring candidate",
                    candidates=tuple(substring),
                )
            return _build_found_project(picked, candidates)
        raise ProjectAmbiguityError(
            message=f"{len(substring)} substring matches for name={name!r}; pass on_ambiguous=",
            candidates=tuple(substring),
        )

    def _locate_artifacts(
        self,
        *,
        project_path: Path,
        configuration: str,
        project_name: str,
    ) -> tuple[Path | None, Path | None]:
        """Locate .elf and .map under ``<project>/<configuration>/``.

        v1 simple-now: typical CubeIDE layout is
        ``<project>/<config>/<project_name>.elf`` +
        ``<project>/<config>/<project_name>.map``. When absent, return
        ``None`` — substrate doesn't synthesize paths the build didn't
        produce.
        """
        config_dir = project_path / configuration
        candidates_elf = [config_dir / f"{project_name}.elf"]
        candidates_map = [config_dir / f"{project_name}.map"]
        # Some projects emit artifacts with the directory name rather than
        # the .project <name>; check both.
        if project_name != project_path.name:
            candidates_elf.append(config_dir / f"{project_path.name}.elf")
            candidates_map.append(config_dir / f"{project_path.name}.map")
        artifact = next((p for p in candidates_elf if p.is_file()), None)
        map_path = next((p for p in candidates_map if p.is_file()), None)
        return artifact, map_path


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _search_cproject(root: Path, *, max_depth: int) -> list[Path]:
    """Return absolute paths to ``.cproject`` files under ``root`` at
    depth 0..``max_depth``."""
    if not root.is_dir():
        return []
    results: list[Path] = []
    _walk(root, depth=0, max_depth=max_depth, out=results)
    return results


def _walk(root: Path, *, depth: int, max_depth: int, out: list[Path]) -> None:
    candidate = root / ".cproject"
    if candidate.is_file():
        out.append(candidate.resolve())
    if depth >= max_depth:
        return
    try:
        children = sorted(root.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir() and not child.name.startswith("."):
            _walk(child, depth=depth + 1, max_depth=max_depth, out=out)


def _read_project_name(cproject_path: Path) -> str:
    """Read ``<name>`` from the sibling ``.project`` file, fall back to dir name."""
    project_dir = cproject_path.parent
    project_xml = project_dir / ".project"
    if not project_xml.is_file():
        return project_dir.name
    try:
        tree = ET.parse(project_xml)
    except ET.ParseError:
        return project_dir.name
    name_el = tree.getroot().find("name")
    if name_el is not None and name_el.text:
        return name_el.text.strip()
    return project_dir.name


def _build_found_project(
    cproject_path: Path, candidates: tuple[Path, ...]
) -> FoundProject:
    project_dir = cproject_path.parent
    name = _read_project_name(cproject_path)
    return FoundProject(
        path=project_dir,
        name=name,
        cproject_path=cproject_path,
        candidates_considered=tuple(candidates),
    )
