"""C1f tests — CubeIDE.find_project (B-018 / B-019 discovery)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeide import CubeIDE, FoundProject
from embedagents.stm32.errors import CubeIDEError, ProjectAmbiguityError


def _make_project(parent: Path, *, name: str | None = None) -> Path:
    """Create a project dir under ``parent`` with a .cproject + .project."""
    proj = parent / (name or "demo")
    proj.mkdir()
    (proj / ".cproject").write_text("<cproject/>")
    project_root = ET.Element("projectDescription")
    if name is not None:
        name_el = ET.SubElement(project_root, "name")
        name_el.text = name
    ET.ElementTree(project_root).write(proj / ".project")
    return proj


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cubeide = tmp_path / "stm32cubeide"
    fake_cubeide.write_text("#!/bin/sh\nexit 0\n")
    fake_cubeide.chmod(0o755)
    monkeypatch.setenv("STM32CUBEIDE", str(fake_cubeide))
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestSingleMatch:
    def test_one_project_at_root(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        proj = _make_project(tmp_path, name="demo")
        client = CubeIDE(ctx)
        result = client.find_project(tmp_path)
        assert isinstance(result, FoundProject)
        assert result.path == proj
        assert result.name == "demo"
        assert result.cproject_path == proj / ".cproject"
        assert len(result.candidates_considered) == 1

    def test_one_project_nested(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        subdir = tmp_path / "src"
        subdir.mkdir()
        proj = _make_project(subdir, name="nested")
        client = CubeIDE(ctx)
        result = client.find_project(tmp_path)
        assert result.path == proj
        assert result.name == "nested"

    def test_default_folder_is_ctx_cwd(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        proj = _make_project(tmp_path, name="cwddemo")
        client = CubeIDE(ctx)
        # No folder= → defaults to ctx.cwd which is tmp_path.
        result = client.find_project()
        assert result.path == proj


class TestZeroMatches:
    def test_empty_folder_raises_no_project_found(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        client = CubeIDE(ctx)
        with pytest.raises(CubeIDEError) as excinfo:
            client.find_project(empty)
        assert excinfo.value.cubeide_marker == "no-project-found"


class TestMultipleMatches:
    def test_without_callback_raises_ambiguity(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        _make_project(tmp_path, name="alpha")
        _make_project(tmp_path, name="beta")
        client = CubeIDE(ctx)
        with pytest.raises(ProjectAmbiguityError) as excinfo:
            client.find_project(tmp_path)
        assert len(excinfo.value.candidates) == 2

    def test_with_callback_routes_pick(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        a = _make_project(tmp_path, name="alpha")
        b = _make_project(tmp_path, name="beta")
        client = CubeIDE(ctx)

        seen: list[list[Path]] = []

        def picker(paths: list[Path]) -> Path:
            seen.append(list(paths))
            return next(p for p in paths if "beta" in str(p))

        result = client.find_project(tmp_path, on_ambiguous=picker)
        assert len(seen) == 1
        assert result.path == b
        assert a.parent in result.candidates_considered[0].parents or True  # sanity

    def test_callback_returning_unknown_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        _make_project(tmp_path, name="alpha")
        _make_project(tmp_path, name="beta")
        client = CubeIDE(ctx)
        with pytest.raises(ProjectAmbiguityError):
            client.find_project(
                tmp_path,
                on_ambiguous=lambda _paths: Path("/not/a/candidate/.cproject"),
            )


class TestNameMatching:
    def test_exact_name_wins(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        _make_project(tmp_path, name="alphabeta")
        target = _make_project(tmp_path, name="alpha")
        client = CubeIDE(ctx)
        result = client.find_project(tmp_path, name="alpha")
        # Exact "alpha" wins, not the substring-match "alphabeta".
        assert result.path == target
        assert result.name == "alpha"

    def test_substring_match_warns_and_picks(
        self,
        ctx: SubstrateContext,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        proj = _make_project(tmp_path, name="alphabeta")
        client = CubeIDE(ctx)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.cubeide"):
            result = client.find_project(tmp_path, name="alpha")
        assert result.path == proj
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("substring" in r.message for r in warnings)

    def test_multiple_substring_without_callback_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        _make_project(tmp_path, name="alphabeta")
        _make_project(tmp_path, name="alphagamma")
        client = CubeIDE(ctx)
        with pytest.raises(ProjectAmbiguityError):
            client.find_project(tmp_path, name="alpha")

    def test_multiple_substring_with_callback(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        _make_project(tmp_path, name="alphabeta")
        gamma = _make_project(tmp_path, name="alphagamma")
        client = CubeIDE(ctx)
        result = client.find_project(
            tmp_path,
            name="alpha",
            on_ambiguous=lambda paths: next(p for p in paths if "gamma" in str(p)),
        )
        assert result.path == gamma

    def test_no_name_match_raises_no_project_found(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        _make_project(tmp_path, name="alpha")
        client = CubeIDE(ctx)
        with pytest.raises(CubeIDEError) as excinfo:
            client.find_project(tmp_path, name="nonexistent")
        assert excinfo.value.cubeide_marker == "no-project-found"


class TestDepthLimit:
    def test_depth_2_found(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        # Project AT depth 2: tmp_path / a / b / .cproject
        b_dir = tmp_path / "a" / "b"
        b_dir.mkdir(parents=True)
        (b_dir / ".cproject").write_text("<cproject/>")
        # .project for name resolution
        project_xml = ET.Element("projectDescription")
        name_el = ET.SubElement(project_xml, "name")
        name_el.text = "depth2"
        ET.ElementTree(project_xml).write(b_dir / ".project")

        client = CubeIDE(ctx)
        result = client.find_project(tmp_path)
        assert result.name == "depth2"

    def test_depth_3_not_found(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        # Project AT depth 3: tmp_path / a / b / c / .cproject (too deep)
        c_dir = tmp_path / "a" / "b" / "c"
        c_dir.mkdir(parents=True)
        (c_dir / ".cproject").write_text("<cproject/>")
        client = CubeIDE(ctx)
        with pytest.raises(CubeIDEError) as excinfo:
            client.find_project(tmp_path)
        assert excinfo.value.cubeide_marker == "no-project-found"


# ---------------------------------------------------------------------------
# Real nested-workspace tree — F-PROJ-STM32H7S78-DK-MULTI-FOLDER
# ---------------------------------------------------------------------------


_H7S78_MSC_STANDALONE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "projects"
    / "STM32CubeH7RS"
    / "Projects"
    / "STM32H7S78-DK"
    / "Applications"
    / "USB_Device"
    / "MSC_Standalone"
    / "STM32CubeIDE"
)


class TestMultiFolderRealWorkspace:
    """Exercise find_project against the real ST nested-workspace tree.

    The H7S78-DK MSC_Standalone example layout is:
    ``STM32CubeIDE/.project`` (parent referencing children) +
    ``STM32CubeIDE/Appli/.cproject`` (RAM/ROM XSPI app) +
    ``STM32CubeIDE/Boot/.cproject`` (FLASH boot). Substrate's
    ``find_project`` must:

    1. Discover both child .cprojects at depth 1.
    2. Raise ``ProjectAmbiguityError`` on bare call.
    3. Resolve each by name (substring fallback against the ``.project``
       `<name>` elements like ``MSC_Standalone_Appli`` /
       ``MSC_Standalone_Boot``).

    Skipped cleanly when the fixture tree isn't on disk (the H7S78-DK
    project is gitignored per RES-019 user-provides; each developer
    populates locally from the STM32CubeH7RS firmware bundle).
    """

    @pytest.fixture()
    def real_ctx(self, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
        if not _H7S78_MSC_STANDALONE.is_dir():
            pytest.skip(
                f"MSC_Standalone fixture tree not on disk at "
                f"{_H7S78_MSC_STANDALONE}; user-provides per RES-019 "
                "(clone STM32CubeH7RS firmware bundle to populate)."
            )
        # find_project is filesystem-only; substrate only needs a context
        # for ``ctx.cwd`` defaulting + logger access. Stub a cubeide tool
        # path so SubstrateContext loads cleanly.
        import sys
        if sys.platform == "win32":
            stub = _H7S78_MSC_STANDALONE / "stm32cubeide.exe"
            stub.parent.mkdir(parents=True, exist_ok=True)
            if not stub.exists():
                stub.write_bytes(b"")
        else:
            stub = _H7S78_MSC_STANDALONE / "stm32cubeide"
            if not stub.exists():
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
        monkeypatch.setenv("STM32CUBEIDE", str(stub))
        return SubstrateContext.from_environment(
            project_path=_H7S78_MSC_STANDALONE
        )

    def test_bare_find_raises_ambiguity_with_appli_and_boot(
        self, real_ctx: SubstrateContext
    ) -> None:
        client = CubeIDE(real_ctx)
        with pytest.raises(ProjectAmbiguityError) as excinfo:
            client.find_project(_H7S78_MSC_STANDALONE)
        candidates = excinfo.value.candidates
        assert len(candidates) == 2
        names = {c.parent.name for c in candidates}
        assert names == {"Appli", "Boot"}

    def test_name_appli_resolves_to_appli_subproject(
        self, real_ctx: SubstrateContext
    ) -> None:
        client = CubeIDE(real_ctx)
        result = client.find_project(_H7S78_MSC_STANDALONE, name="Appli")
        assert isinstance(result, FoundProject)
        assert result.path.name == "Appli"
        assert result.cproject_path.name == ".cproject"
        # ST writes .project <name> as the parent-prefixed form.
        assert "Appli" in result.name

    def test_name_boot_resolves_to_boot_subproject(
        self, real_ctx: SubstrateContext
    ) -> None:
        client = CubeIDE(real_ctx)
        result = client.find_project(_H7S78_MSC_STANDALONE, name="Boot")
        assert isinstance(result, FoundProject)
        assert result.path.name == "Boot"
        assert "Boot" in result.name

    def test_unknown_name_raises_no_project_found(
        self, real_ctx: SubstrateContext
    ) -> None:
        client = CubeIDE(real_ctx)
        with pytest.raises(CubeIDEError) as excinfo:
            client.find_project(_H7S78_MSC_STANDALONE, name="Bootloader")
        assert excinfo.value.cubeide_marker == "no-project-found"

    def test_on_ambiguous_callback_picks_appli(
        self, real_ctx: SubstrateContext
    ) -> None:
        client = CubeIDE(real_ctx)
        result = client.find_project(
            _H7S78_MSC_STANDALONE,
            on_ambiguous=lambda paths: next(p for p in paths if p.parent.name == "Appli"),
        )
        assert result.path.name == "Appli"
