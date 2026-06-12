"""CubeIDE hardware tests - headless build against the attached bench.

These run against the F-PROJ-NUCLEO-L476RG-BLINKY project's
GPIO_IOToggle example, building it via CubeIDE's headless-build path.
No probe is strictly required (cubeide builds don't touch the
target), but the tests live under the ``hardware`` marker so they
group with the rest of the F.6 bench suite and gate on the same
``l476rg_ctx`` fixture (which also confirms vendor CLIs are
resolvable on this host).

Prerequisite: HAL / CMSIS-device / BSP populated under the F-PROJ
tree per RES-019 user-provides (see plan-windows.md F-PROJ HAL gap
resolution note). The .gitignore excludes these subtrees from the
repo - each developer's bench populates them locally from a
STM32CubeL4 firmware-bundle extract.

Excluded from the default ``pytest`` run; invoke with
``pytest -m hardware``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from embedagents.stm32.cubeide import CubeIDE
from embedagents.stm32.cubeide.results import BuildResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_workspace_ctx(l476rg_ctx):
    """Per-test fresh CubeIDE workspace + auto-restore of every ``.cproject``.

    Workspace cleanup: Substrate's
    ``cubeide.workspace.cleanup_stale_project`` (workspace.py) only
    removes ``.projects/<name>`` + ``.metadata/.lock``; Eclipse's binary
    ``.projects.workspace.tree`` still tracks the prior import, so a
    second ``-import`` of the same project into the same workspace
    fails with "Project ... already exists in the workspace!". Until
    the substrate's cleanup learns to nuke the deeper state (or to
    skip ``-import`` when the project is already known), each test
    starts from an empty workspace.

    ``.cproject`` snapshot/restore: ``CProjectEditor``'s commit/rollback
    is designed around build-failure semantics — a passing preset edit
    keeps the mutation on disk. Test runs must not leave the user's
    fixture tree mutated for the next session (or the next bench
    operator opening the project in CubeIDE), so we snapshot every
    ``.cproject`` under the F-PROJ tree before the test and restore
    them on teardown regardless of pass/fail. Bytewise comparison
    avoids spurious mtime updates when nothing changed.

    TODO(v1+): once substrate's workspace cleanup is comprehensive,
    drop the workspace-rmtree half; the snapshot/restore half stays.
    """
    project_root = (l476rg_ctx.cwd / "Projects").resolve()
    cproject_snapshots: dict[Path, bytes] = {}
    if project_root.is_dir():
        for cproject in project_root.rglob(".cproject"):
            try:
                cproject_snapshots[cproject] = cproject.read_bytes()
            except OSError:
                continue

    workspace = Path(l476rg_ctx.project.build.workspace)
    if not workspace.is_absolute():
        workspace = l476rg_ctx.cwd / workspace
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)

    try:
        yield l476rg_ctx
    finally:
        for path, original in cproject_snapshots.items():
            try:
                if not path.is_file() or path.read_bytes() != original:
                    path.write_bytes(original)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# TestBuild
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestBuild:
    def test_build_returns_success_with_elf_artifact(
        self, fresh_workspace_ctx
    ) -> None:
        """Descriptor-driven build: no explicit project= kwarg; substrate
        resolves ``ctx.project.build.project_path`` against the F-PROJ
        root to land on the GPIO_IOToggle STM32CubeIDE Eclipse project.

        Asserts the full BuildResult contract: success=True, exit 0,
        artifact_path points at an existing .elf, map_path likewise."""
        result = CubeIDE(fresh_workspace_ctx).build()
        if not result.success:
            pytest.fail(
                f"headless build failed (exit={result.exit_code}); "
                f"see {result.log_path}. Tail of console:\n"
                f"{result.console_output[-1500:]}"
            )
        assert isinstance(result, BuildResult)
        assert result.exit_code == 0
        assert result.project_name == "BLINKY"
        assert result.configuration == "Debug"
        assert result.artifact_path is not None and result.artifact_path.is_file(), (
            f"artifact_path {result.artifact_path} is not a file on disk"
        )
        assert result.artifact_path.stat().st_size > 0, "ELF is empty"
        assert result.map_path is not None and result.map_path.is_file(), (
            f"map_path {result.map_path} is not a file on disk"
        )
        assert result.project_imported is True

    def test_clean_build_succeeds_and_rebuilds_artifact(
        self, fresh_workspace_ctx
    ) -> None:
        """``clean=True`` forces a fresh compile of every object - if the
        Eclipse incremental-build state was lying about completeness,
        this would surface it. Also exercises the cleanup branch in
        CubeIDE.build() where stale Debug/ object files get wiped
        before make-all runs."""
        result = CubeIDE(fresh_workspace_ctx).build(clean=True)
        if not result.success:
            pytest.fail(
                f"clean build failed (exit={result.exit_code}); "
                f"see {result.log_path}. Tail of console:\n"
                f"{result.console_output[-1500:]}"
            )
        assert result.exit_code == 0
        assert result.artifact_path is not None and result.artifact_path.is_file()
        assert result.artifact_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# TestBrokenBuild — compile / link failure paths (backlog #8 + #9)
# ---------------------------------------------------------------------------
#
# Builds the dedicated F-PROJ-NUCLEO-L476RG BROKEN-COMPILE / BROKEN-LINK
# sub-projects directly. The fixtures inject canonical broken main.c
# content idempotently — no save/restore of BLINKY, no rebuild dance in
# teardown. BLINKY stays untouched so debug / cubeprogrammer tests run
# concurrently without interference.
#
# The dedicated trees live under Projects/NUCLEO-L476RG/Examples/FLASH/
# (gitignored per the F-PROJ-* user-provides convention); the test code
# carries the canonical broken content inline and writes it at fixture
# entry. Re-extracting the user-provides tree restores the original ST
# example source; the next test run overwrites with broken content
# again.


_BROKEN_COMPILE_PROJECT = Path(
    "Projects/NUCLEO-L476RG/Examples/FLASH/BROKEN-COMPILE"
)
_BROKEN_LINK_PROJECT = Path(
    "Projects/NUCLEO-L476RG/Examples/FLASH/BROKEN-LINK"
)


_BROKEN_COMPILE_MAIN_C = """\
/* substrate-test BROKEN-COMPILE firmware: deliberate syntax error
 * (missing semicolon) to exercise cubeide's build-failure path. */
int main(void) {
    int x = 42
    return x;
}
"""


_BROKEN_LINK_MAIN_C = """\
/* substrate-test BROKEN-LINK firmware: extern reference to an
 * undefined symbol; compiles cleanly, fails at the link stage. */
extern void _embedagents.stm32_missing_symbol(void);

int main(void) {
    _embedagents.stm32_missing_symbol();
    return 0;
}
"""


@pytest.fixture
def broken_compile_project(fresh_workspace_ctx) -> Path:
    """Locate the BROKEN-COMPILE sub-project, write the canonical
    broken-compile main.c content, yield the project's STM32CubeIDE
    path. Idempotent — no restore."""
    proj_root = (fresh_workspace_ctx.cwd / _BROKEN_COMPILE_PROJECT).resolve()
    main_c = proj_root / "Src" / "main.c"
    cubeide_dir = proj_root / "STM32CubeIDE"
    if not (proj_root.is_dir() and main_c.is_file() and cubeide_dir.is_dir()):
        pytest.skip(
            f"BROKEN-COMPILE project not populated at {proj_root}; "
            "user-provides per RES-019 (see plan-test.md)."
        )
    main_c.write_text(_BROKEN_COMPILE_MAIN_C, encoding="utf-8", newline="\n")
    return cubeide_dir


@pytest.fixture
def broken_link_project(fresh_workspace_ctx) -> Path:
    """Locate the BROKEN-LINK sub-project, write the canonical
    broken-link main.c content, yield the project's STM32CubeIDE path.
    Idempotent — no restore."""
    proj_root = (fresh_workspace_ctx.cwd / _BROKEN_LINK_PROJECT).resolve()
    main_c = proj_root / "Src" / "main.c"
    cubeide_dir = proj_root / "STM32CubeIDE"
    if not (proj_root.is_dir() and main_c.is_file() and cubeide_dir.is_dir()):
        pytest.skip(
            f"BROKEN-LINK project not populated at {proj_root}; "
            "user-provides per RES-019 (see plan-test.md)."
        )
    main_c.write_text(_BROKEN_LINK_MAIN_C, encoding="utf-8", newline="\n")
    return cubeide_dir


@pytest.mark.hardware
class TestBrokenBuild:
    """Cubeide's commit/rollback rule (RES-010 Q1): protocol-level failures
    (snapshot/parse/validate XML) roll back; **build-level failures**
    (compile/link errors after a valid edit) return BuildResult(success=
    False) without raising. These tests exercise the latter path on
    real hardware — substrate must capture the failure exit code +
    console output, not raise."""

    def test_compile_error_returns_build_failure(
        self, fresh_workspace_ctx, broken_compile_project: Path
    ) -> None:
        """Syntactically invalid C → gcc errors out → make exits non-zero
        → cubeide headless build exits non-zero → substrate returns
        BuildResult(success=False, exit_code != 0). Console output
        captured carries the gcc error message for caller / Claude
        introspection (per ADR-004: substrate captures, doesn't
        interpret)."""
        result = CubeIDE(fresh_workspace_ctx).build(
            project=broken_compile_project, clean=True
        )
        assert isinstance(result, BuildResult)
        assert result.success is False
        assert result.exit_code != 0, (
            f"expected non-zero exit; got {result.exit_code}. "
            f"Console tail: {result.console_output[-500:]}"
        )
        # Captured console should mention the syntax error somewhere;
        # substrate doesn't parse, just captures.
        assert (
            "error" in result.console_output.lower()
            or "expected" in result.console_output.lower()
        ), (
            f"console_output didn't mention a compile error; "
            f"tail: {result.console_output[-500:]}"
        )

    def test_link_error_returns_build_failure(
        self, fresh_workspace_ctx, broken_link_project: Path
    ) -> None:
        """Reference to an undefined extern symbol passes compilation
        but fails at link time. ld errors with 'undefined reference';
        substrate captures + returns BuildResult(success=False)."""
        result = CubeIDE(fresh_workspace_ctx).build(
            project=broken_link_project, clean=True
        )
        assert isinstance(result, BuildResult)
        assert result.success is False
        assert result.exit_code != 0
        assert (
            "undefined reference" in result.console_output.lower()
            or "undefined symbol" in result.console_output.lower()
            or "ld returned" in result.console_output.lower()
        ), (
            f"console_output didn't mention an undefined-reference link "
            f"error; tail: {result.console_output[-500:]}"
        )


# ---------------------------------------------------------------------------
# TestPresetFast — preset="fast" multi-edit against real CubeIDE
# ---------------------------------------------------------------------------
#
# Validates the presets.py table fix: regex patterns match real CubeIDE
# superClasses (debuglevel not debugging.level; managedbuild.option.fpu
# not compiler.option.fpu), and value strings expand "..." into the
# matched option's superClass so the written value lands as a valid
# CDT enum token ("<superClass>.value.<token>"). Pre-fix the build
# would either no-match the regex (CProjectEditError on debuglevel) or
# write a placeholder value that CubeIDE rejected at link time.


@pytest.mark.hardware
class TestPresetFast:
    """preset="fast" applies -O3 + -g1 + -flto + FPU flags to the active
    configuration's .cproject + runs a clean headless build. Substrate
    rolls back the .cproject on protocol failure; on build failure the
    edit is kept (per RES-010 Q1).

    The L476RG F-PROJ-BLINKY descriptor carries
    firmware.device_family="STM32L4" so the FPU branch fires
    (-mfpu=fpv4-sp-d16 -mfloat-abi=hard); without that field the test
    would still build but on the soft-FP fallback with a WARNING.
    """

    def test_preset_fast_builds_clean(
        self, fresh_workspace_ctx
    ) -> None:
        result = CubeIDE(fresh_workspace_ctx).build(
            preset="fast", clean=True
        )
        if not result.success:
            pytest.fail(
                f"preset='fast' build failed (exit={result.exit_code}); "
                f"see {result.log_path}. Tail of console:\n"
                f"{result.console_output[-1500:]}"
            )
        assert result.exit_code == 0
        assert result.artifact_path is not None and result.artifact_path.is_file()
        assert result.artifact_path.stat().st_size > 0
        # Preset bundles 4 set/append edits + 2 FPU edits for the
        # STM32L4 family on the active configuration only.
        assert result.settings_modification is not None
        assert len(result.settings_modification.changes) >= 4
        # No protocol-level rollback occurred (preset is exclusive with
        # the rollback-on-build-failure rule per RES-010 Q1, but we
        # asserted success above so this is just a sanity check).
        assert result.settings_modification.rolled_back is False


# ---------------------------------------------------------------------------
# TestFindProjectNamed — B-019 select named project from multiple
# ---------------------------------------------------------------------------


_FLASH_EXAMPLES = Path("Projects/NUCLEO-L476RG/Examples/FLASH")


@pytest.mark.hardware
class TestFindProjectNamed:
    """B-019 — `find_project(folder, name=...)` selects a named project
    from a folder holding multiple `.cproject` files. Exercises the real
    F-PROJ FLASH Examples folder, which has four projects within depth 2
    (BROKEN-COMPILE / BROKEN-LINK / FLASH_DualBoot / FLASH_WriteProtection).
    Filesystem-only resolution (the board isn't touched) — gated on
    `l476rg_ctx` for F-PROJ-tree presence + suite consistency."""

    def test_select_named_project_from_multiple(self, l476rg_ctx) -> None:
        """name= picks exactly one of several candidates. Asserts the
        FoundProject names the requested project, points at a real
        `.cproject` under its STM32CubeIDE dir, and that the pick was made
        from a multi-candidate set (candidates_considered carries them
        all)."""
        folder = (l476rg_ctx.cwd / _FLASH_EXAMPLES).resolve()
        if not folder.is_dir():
            pytest.skip(
                f"FLASH Examples folder not populated at {folder}; "
                "user-provides per RES-019"
            )
        found = CubeIDE(l476rg_ctx).find_project(folder, name="BROKEN-LINK")
        assert found.name == "BROKEN-LINK"
        assert found.cproject_path.is_file()
        assert found.cproject_path.parent.name == "STM32CubeIDE"
        # The selection was made from several candidates, not a lone match.
        assert len(found.candidates_considered) >= 2
        assert found.cproject_path in found.candidates_considered
