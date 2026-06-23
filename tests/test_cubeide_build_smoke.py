"""RES-054 smoke test — real CubeIDE ``-import``/``-build`` must not strip
``.project`` on workspace reuse.

``@pytest.mark.smoke`` (real CLIs, no hardware). This is the real-tool
counterpart to the mocked ``TestWorkspaceReuseSafety`` unit tests in
``test_cubeide_build.py``: it exercises what mocks cannot — a real Eclipse
``-import`` that materialises the linked-resource virtual folders, a real
``.location`` decode, and the actual fix end-to-end. Cross-platform; the
default-workspace path under test has no Linux-only code (RES-054).

Skips cleanly when:
  - CubeIDE is not resolvable on this host (set ``STM32CUBEIDE`` or
    ``.claude/stm32-tools.local.jsonc``), or
  - the F-PROJ BLINKY fixture (a linked-resource STM32Cube example) is not
    present (the F-PROJ trees are synced, not always on a fresh checkout).

Restores the ``.project`` fixture + drops all build byproducts on teardown
(RES-027 discipline — never leak fixture mutations across runs).

Run:  pytest -m smoke tests/test_cubeide_build_smoke.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeide import CubeIDE
from embedagents.stm32.cubeide import workspace as ws_mod
from embedagents.stm32.errors import ConfigurationError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BLINKY = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "projects"
    / "F-PROJ-NUCLEO-L476RG"
    / "Projects"
    / "NUCLEO-L476RG"
    / "Examples"
    / "GPIO"
    / "BLINKY"
    / "STM32CubeIDE"
)
# Pure CDT-materialised virtual folders (deleting these is the trigger).
# Example/ is a REAL source folder (startup .s / Reset_Handler) — never delete it.
_LINK_FOLDERS = ("Drivers", "Doc")
_BYPRODUCTS = ("Debug", ".settings", *_LINK_FOLDERS)


def _link_count(project: Path) -> int:
    return (project / ".project").read_text(encoding="utf-8").count("<link>")


@pytest.fixture()
def blinky_build_env() -> tuple[SubstrateContext, Path, Path]:
    """Resolve CubeIDE + the F-PROJ BLINKY, snapshot/restore ``.project``."""
    if not (_BLINKY / ".project").is_file():
        pytest.skip(
            f"F-PROJ BLINKY fixture absent at {_BLINKY} — sync the F-PROJ trees"
        )
    ctx = SubstrateContext.from_environment(project_path=_BLINKY)
    if ctx.tools.cubeide_path is None:
        pytest.skip("CubeIDE not resolvable on this host (set STM32CUBEIDE)")

    default_ws = ws_mod.default_workspace_root(_BLINKY.resolve())
    project_snapshot = (_BLINKY / ".project").read_bytes()

    def _clean() -> None:
        shutil.rmtree(default_ws, ignore_errors=True)
        for sub in _BYPRODUCTS:
            shutil.rmtree(_BLINKY / sub, ignore_errors=True)

    _clean()
    try:
        yield ctx, _BLINKY, default_ws
    finally:
        (_BLINKY / ".project").write_bytes(project_snapshot)
        _clean()


@pytest.mark.smoke
class TestWorkspaceReuseProjectIntegritySmoke:
    def test_default_workspace_rebuild_preserves_dot_project(
        self, blinky_build_env: tuple[SubstrateContext, Path, Path]
    ) -> None:
        ctx, blinky, default_ws = blinky_build_env
        client = CubeIDE(ctx)

        # Build #1 — fresh import materialises Drivers/ + Doc/; .project intact.
        r1 = client.build(project=blinky)
        assert r1.success, f"build #1 failed:\n{r1.console_output[-1000:]}"
        assert _link_count(blinky) == 12
        for folder in _LINK_FOLDERS:
            assert (blinky / folder).is_dir(), f"CDT did not materialise {folder}/"

        # The real Eclipse .location now records the project — exercise the
        # real decode the unit test mocks.
        assert ws_mod.detect_project_imported(default_ws, "BLINKY") == blinky.resolve()

        # User deletes the materialised virtual folders (they look like build
        # cruft). The real detector must report them missing.
        for folder in _LINK_FOLDERS:
            shutil.rmtree(blinky / folder)
        assert sorted(ws_mod.missing_linked_folders(blinky)) == ["Doc", "Drivers"]

        # Build #2 — RES-054: the substrate-owned default workspace is wiped +
        # re-imported, so CDT re-reads the on-disk .project and never strips it.
        # (Pre-fix this reused the cached workspace, skipped import, and CDT
        # re-saved .project 12 -> 2.)
        r2 = client.build(project=blinky)
        assert r2.success, f"build #2 failed:\n{r2.console_output[-1000:]}"
        assert r2.project_imported is True
        assert _link_count(blinky) == 12, (
            "RES-054 regression: .project was stripped on workspace reuse"
        )

    def test_explicit_workspace_raises_and_is_never_deleted(
        self,
        blinky_build_env: tuple[SubstrateContext, Path, Path],
        tmp_path: Path,
    ) -> None:
        """Explicit user ``build.workspace``: reused as-is, NEVER auto-deleted;
        when reuse would skip ``-import`` and an in-tree linked folder is gone,
        ``build()`` raises off the REAL cached ``.location`` BEFORE launching
        CDT — the part the mocked unit test fakes.

        Uses a temp descriptor (``build.project_path`` -> the F-PROJ BLINKY,
        ``build.workspace`` -> a temp dir) so the fixture tree is untouched
        beyond the ``.project`` snapshot ``blinky_build_env`` already restores.
        """
        _ctx, blinky, _default_ws = blinky_build_env
        ext_ws = tmp_path / "user_ws"
        descriptor_dir = tmp_path / "proj_root"
        descriptor_dir.mkdir()
        (descriptor_dir / "stm32-project.jsonc").write_text(
            json.dumps(
                {
                    "version": 1,
                    "build": {
                        "project_path": str(blinky),
                        "workspace": str(ext_ws),
                    },
                }
            ),
            encoding="utf-8",
        )
        ctx = SubstrateContext.from_environment(project_path=descriptor_dir)
        client = CubeIDE(ctx)

        # Build #1 — real import into the USER-owned workspace; .project intact,
        # the workspace now exists and holds a real Eclipse .location.
        r1 = client.build()
        assert r1.success, f"build #1 failed:\n{r1.console_output[-1000:]}"
        assert _link_count(blinky) == 12
        assert ext_ws.is_dir()
        assert ws_mod.detect_project_imported(ext_ws, "BLINKY") is not None

        # User deletes the materialised virtual folders.
        for folder in _LINK_FOLDERS:
            shutil.rmtree(blinky / folder)

        # Build #2 — reuse would skip -import AND folders are gone: raise BEFORE
        # CDT (off the real cached .location), never corrupting .project and
        # never auto-deleting the user workspace.
        with pytest.raises(ConfigurationError, match="(?i)drivers|missing"):
            client.build()
        assert _link_count(blinky) == 12, (
            "explicit-workspace path must not strip .project"
        )
        assert ext_ws.is_dir(), (
            "a user-configured build.workspace must never be auto-deleted"
        )
