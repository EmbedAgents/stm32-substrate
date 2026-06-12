"""C1d-e tests — CubeIDE.build() end-to-end orchestration.

Tests mock the subprocess layer (headless-build.sh invocation) so we
exercise the kwargs validation + workspace lifecycle + CProject protocol
+ result assembly without spawning real Eclipse builds.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeide import BuildResult, CubeIDE
from embedagents.stm32.errors import (
    CProjectEditError,
    CubeIDEError,
    WorkspaceLockedError,
)
from embedagents.stm32.subprocess_runner import ToolRunResult


# ---------------------------------------------------------------------------
# Test fixtures: synthetic project tree
# ---------------------------------------------------------------------------


def _make_cproject_xml(configurations: list[str] = ["Debug", "Release"]) -> bytes:
    """Build a minimal CDT-shaped .cproject XML blob."""
    root = ET.Element("cproject")
    sm = ET.SubElement(root, "storageModule", moduleId="cdtBuildSystem")
    for cfg in configurations:
        cconfig = ET.SubElement(sm, "cconfiguration", id=f"cfg.{cfg.lower()}.id")
        config = ET.SubElement(cconfig, "configuration", name=cfg)
        toolchain = ET.SubElement(config, "toolChain")
        tool = ET.SubElement(toolchain, "tool")
        # Plant compiler-option elements so set_option / append_list_value
        # have something to find. superClass strings mirror real CubeIDE
        # shape (``debuglevel`` not ``debugging.level``; ``managedbuild.
        # option.fpu`` not ``compiler.option.fpu``) so the same regex
        # patterns work on both synthetic + real .cproject inputs.
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.compiler.option.debuglevel",
            value="gnu.c.compiler.option.debuglevel.value.g3",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.compiler.option.optimization.level",
            value="gnu.c.compiler.option.optimization.level.value.none",
        )
        # Real CubeIDE superClass suffixes (definedsymbols / includepaths /
        # libraries / directories) — the old preprocessor.def.symbols /
        # includepath / libs forms never matched real ST output and let a
        # regex-drift bug hide until real ST example projects hit it.
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.compiler.option.definedsymbols",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.compiler.option.includepaths",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.linker.option.libraries",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.linker.option.directories",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.compiler.option.otherflags",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.linker.option.otherflags",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.c.linker.option.usenewlibnano",
            value="false",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.managedbuild.option.fpu",
            value="",
        )
        ET.SubElement(
            tool, "option",
            superClass="gnu.managedbuild.option.floatabi",
            value="",
        )
    import io

    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / ".cproject").write_bytes(_make_cproject_xml())
    project_xml = ET.Element("projectDescription")
    name_el = ET.SubElement(project_xml, "name")
    name_el.text = "demo"
    ET.ElementTree(project_xml).write(proj / ".project")
    return proj


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    cubeide_bin, headless_script = _make_cubeide_stubs(tmp_path)
    monkeypatch.setenv("STM32CUBEIDE", str(cubeide_bin))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _make_cubeide_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Author per-OS stub CubeIDE binary + headless build script.

    Linux: ``stm32cubeide`` + ``headless-build.sh`` (chmod 0o755).
    Windows: ``stm32cubeide.exe`` + ``headless-build.bat``.
    """
    if sys.platform == "win32":
        cubeide_bin = tmp_path / "stm32cubeide.exe"
        cubeide_bin.write_bytes(b"")
        headless_script = tmp_path / "headless-build.bat"
        headless_script.write_text("@echo off\r\nexit /b 0\r\n")
    else:
        cubeide_bin = tmp_path / "stm32cubeide"
        cubeide_bin.write_text("#!/bin/sh\nexit 0\n")
        cubeide_bin.chmod(0o755)
        headless_script = tmp_path / "headless-build.sh"
        headless_script.write_text("#!/bin/sh\nexit 0\n")
        headless_script.chmod(0o755)
    return cubeide_bin, headless_script


def _build_run_tool_success() -> ToolRunResult:
    return ToolRunResult(
        exit_code=0,
        stdout="Build of configuration Debug for project demo complete\n",
        stderr="",
        duration_s=2.5,
        timed_out=False,
    )


def _build_run_tool_failure() -> ToolRunResult:
    return ToolRunResult(
        exit_code=1,
        stdout="src/main.c:42: error: 'undeclared_var' undeclared\n",
        stderr="",
        duration_s=1.1,
        timed_out=False,
    )


def _build_run_tool_nothing_built() -> ToolRunResult:
    # Eclipse CDT headless exits 0 but builds nothing — emitted both on an
    # up-to-date incremental rebuild AND when it imports a project whose
    # managed-build config it can't drive (a legacy SW4STM32/AC6 toolchain).
    return ToolRunResult(
        exit_code=0,
        stdout=(
            "**** Build of configuration Debug for project demo ****\n"
            "Nothing to build for project demo\n"
        ),
        stderr=(
            "Managed Build system manifest file error: Option "
            "fr.ac6.managedbuild.option.gnu.cross.mcu uses a null category "
            "that is invalid in its context. The option was ignored.\n"
        ),
        duration_s=0.4,
        timed_out=False,
    )


# ---------------------------------------------------------------------------
# kwargs validation
# ---------------------------------------------------------------------------


class TestKwargValidation:
    def test_preset_with_debug_level_rejected(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with pytest.raises(ValueError, match="preset is exclusive"):
            client.build(project=project_dir, preset="fast", debug_level="-g3")

    def test_preset_with_optimization_rejected(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with pytest.raises(ValueError, match="preset is exclusive"):
            client.build(project=project_dir, preset="size", optimization="-Os")

    def test_no_project_no_descriptor_raises(self, ctx: SubstrateContext) -> None:
        client = CubeIDE(ctx)
        # No project= kwarg AND no stm32-project.jsonc → ConfigurationError.
        from embedagents.stm32.errors import ConfigurationError

        with pytest.raises(ConfigurationError):
            client.build()

    # A-001: unmapped option values must raise loud, not get written into
    # .cproject verbatim with a success report.
    def test_bare_debug_level_rejected(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with pytest.raises(ValueError, match=r"invalid debug_level '2'.*-g3"):
            client.build(project=project_dir, debug_level="2")

    def test_bare_optimization_rejected(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        # The old stm32build.md documented bare "O2" — must not silently
        # land in .cproject.
        with pytest.raises(ValueError, match=r"invalid optimization 'O2'.*-Oz"):
            client.build(project=project_dir, optimization="O2")

    def test_unknown_optimization_rejected_before_any_edit(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        original_xml = (project_dir / ".cproject").read_bytes()
        with pytest.raises(ValueError, match="invalid optimization"):
            client.build(project=project_dir, optimization="-Omax")
        # Validation fires before the edit protocol — .cproject untouched.
        assert (project_dir / ".cproject").read_bytes() == original_xml

    def test_fully_formed_cdt_enum_value_passes_through(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        # The documented escape hatch: a fully-formed CDT enum value
        # (contains ".value.") bypasses the alias table verbatim.
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(
                project=project_dir,
                debug_level="gnu.c.compiler.option.debuglevel.value.g3",
            )
        assert result.settings_modification is not None
        tree = ET.parse(project_dir / ".cproject")
        values = [
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass") == "gnu.c.compiler.option.debuglevel"
        ]
        assert "gnu.c.compiler.option.debuglevel.value.g3" in values


# ---------------------------------------------------------------------------
# Happy build path
# ---------------------------------------------------------------------------


class TestHappyBuild:
    def test_returns_build_result_success(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir)
        assert isinstance(result, BuildResult)
        assert result.success is True
        assert result.exit_code == 0
        assert result.project_name == "demo"
        assert result.configuration == "Debug"
        assert result.console_output != ""
        # log_path is allocated by headless_log_path; the real run_tool
        # would write to it, but we mocked run_tool so the file may not
        # exist. We only assert the path was assigned.
        assert result.log_path.name.startswith("build-")
        assert result.log_path.name.endswith(".log")
        assert result.settings_modification is None  # no edits requested
        assert result.project_imported is True  # first build always imports

    def test_clean_build_passes_clean_flag(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ) as mocked:
            client.build(project=project_dir, clean=True)
        argv = mocked.call_args[0][1]
        assert "-cleanBuild" in argv
        assert "-build" not in argv

    def test_configuration_in_argv(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ) as mocked:
            client.build(project=project_dir, configuration="Release")
        argv = mocked.call_args[0][1]
        assert "demo/Release" in argv

    def test_import_uses_file_uri_form(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        """Eclipse's HeadlessBuilder.importProject calls EFS.getStore on
        the URI form of the -import path. On Windows, a raw "C:/foo"
        is parsed as URI scheme=C, failing with "No file system is
        defined for scheme: C". Substrate must pass the canonical
        file:/// URI form (Path.as_uri()) so EFS routes to the file
        scheme on both OSes. Caught bench-driven 2026-05-20 against
        H747I-DISCO FPU_Fractal CM7."""
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ) as mocked:
            client.build(project=project_dir)
        argv = mocked.call_args[0][1]
        # -import must be followed by a file:/// URI, not a bare path.
        idx = argv.index("-import")
        import_arg = argv[idx + 1]
        assert import_arg.startswith("file:///"), (
            f"expected file:/// URI; got {import_arg!r} (Eclipse on "
            "Windows fails with 'No file system is defined for scheme: C' "
            "if the drive letter masquerades as URI scheme)"
        )
        # Project path's tail should still be present in the URI.
        assert "demo" in import_arg


# ---------------------------------------------------------------------------
# Explicit path that is not itself an Eclipse project
# ---------------------------------------------------------------------------


def _make_repo_root(
    tmp_path: Path,
    *,
    nested: str = "Projects/BLINKY/STM32CubeIDE",
    name: str = "blinky",
    descriptor: bool = True,
    project_files: bool = True,
) -> Path:
    """Author an ST-example-shaped repo: descriptor at the root, the
    Eclipse project nested several levels down (deeper than
    find_project's max_depth=2)."""
    root = tmp_path / "repo"
    nested_dir = root / nested
    nested_dir.mkdir(parents=True)
    if project_files:
        (nested_dir / ".cproject").write_bytes(_make_cproject_xml())
        project_xml = ET.Element("projectDescription")
        ET.SubElement(project_xml, "name").text = name
        ET.ElementTree(project_xml).write(nested_dir / ".project")
    if descriptor:
        (root / "stm32-project.jsonc").write_text(
            json.dumps({"version": 1, "build": {"project_path": nested}})
        )
    return root


def _ctx_for(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SubstrateContext:
    cubeide_bin, _ = _make_cubeide_stubs(tmp_path)
    monkeypatch.setenv("STM32CUBEIDE", str(cubeide_bin))
    return SubstrateContext.from_environment(project_path=root)


class TestExplicitPathResolution:
    def test_repo_root_resolves_via_descriptor(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`build(project=<repo-root>)` where the root has no .project but
        the descriptor's build.project_path nests under it → builds the
        descriptor-resolved project (logged at INFO), not the root."""
        import logging

        root = _make_repo_root(tmp_path)
        client = CubeIDE(_ctx_for(root, tmp_path, monkeypatch))
        caplog.set_level(logging.INFO, logger="embedagents.stm32")
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ) as mocked:
            result = client.build(project=root)
        assert result.project_name == "blinky"
        argv = mocked.call_args[0][1]
        idx = argv.index("-import")
        assert argv[idx + 1].endswith("STM32CubeIDE")
        assert "no .project" in caplog.text

    def test_no_descriptor_raises_with_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from embedagents.stm32.errors import ConfigurationError

        root = _make_repo_root(tmp_path, descriptor=False)
        client = CubeIDE(_ctx_for(root, tmp_path, monkeypatch))
        with pytest.raises(ConfigurationError, match=r"no \.project"):
            client.build(project=root)

    def test_descriptor_outside_explicit_path_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Descriptor resolves to a project that is NOT under the explicit
        path → no silent redirect; raise, hint names the descriptor path."""
        from embedagents.stm32.errors import ConfigurationError

        root = _make_repo_root(tmp_path)
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        client = CubeIDE(_ctx_for(root, tmp_path, monkeypatch))
        with pytest.raises(ConfigurationError, match=r"no \.project") as excinfo:
            client.build(project=unrelated)
        assert "build.project_path" in (excinfo.value.hint or "")

    def test_nonexistent_path_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from embedagents.stm32.errors import ConfigurationError

        root = _make_repo_root(tmp_path)
        client = CubeIDE(_ctx_for(root, tmp_path, monkeypatch))
        with pytest.raises(ConfigurationError, match="does not exist"):
            client.build(project=root / "nope")

    def test_descriptor_target_not_importable_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Descriptor nests under the explicit path but its target has no
        .project either → raise (the descriptor itself is wrong); never
        hand Eclipse an unimportable directory."""
        from embedagents.stm32.errors import ConfigurationError

        root = _make_repo_root(tmp_path, project_files=False)
        client = CubeIDE(_ctx_for(root, tmp_path, monkeypatch))
        with pytest.raises(ConfigurationError, match=r"no \.project"):
            client.build(project=root)

    def test_explicit_project_dir_unaffected(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        """A path that IS an importable project builds as before — the
        descriptor never overrides a valid explicit path."""
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir)
        assert result.project_name == "demo"


# ---------------------------------------------------------------------------
# Build-level failure
# ---------------------------------------------------------------------------


class TestBuildFailure:
    def test_non_zero_exit_returns_success_false(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_failure(),
        ):
            result = client.build(project=project_dir)
        assert result.success is False
        assert result.exit_code == 1
        assert "undeclared" in result.console_output

    def test_settings_change_kept_on_build_failure(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        """Build-level failure (compile error) after a valid .cproject
        edit keeps the change — substrate doesn't auto-revert."""
        client = CubeIDE(ctx)
        original_xml = (project_dir / ".cproject").read_bytes()
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_failure(),
        ):
            result = client.build(project=project_dir, debug_level="-g1")
        assert result.success is False
        # .cproject was modified and stays modified after the build failed.
        new_xml = (project_dir / ".cproject").read_bytes()
        assert new_xml != original_xml
        assert result.settings_modification is not None
        assert result.settings_modification.rolled_back is False
        assert len(result.settings_modification.changes) == 1


# ---------------------------------------------------------------------------
# Settings edits
# ---------------------------------------------------------------------------


class TestSettingsEdits:
    def test_debug_level_applied(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir, debug_level="-g3")
        # User-facing "-g3" alias → CDT enum value
        # "<superClass>.value.g3" via the "..." expansion convention.
        tree = ET.parse(project_dir / ".cproject")
        debug_opts = sorted(
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass") == "gnu.c.compiler.option.debuglevel"
        )
        assert any(v.endswith(".value.g3") for v in debug_opts), debug_opts
        assert result.settings_modification is not None
        # Active-only: one SettingChange spanning one configuration.
        change = result.settings_modification.changes[0]
        assert change.kind == "set_value"
        assert "," not in change.configuration  # single config touched

    def test_add_symbols_appends_to_list(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(
                project=project_dir,
                add_symbols=["DEBUG", ("MY_VER", "2")],
            )
        tree = ET.parse(project_dir / ".cproject")
        sym_values = []
        for opt in tree.iter("option"):
            if opt.get("superClass") == "gnu.c.compiler.option.definedsymbols":
                for child in opt.findall("listOptionValue"):
                    sym_values.append(child.get("value"))
        assert "DEBUG" in sym_values
        assert "MY_VER=2" in sym_values

    def test_add_include_paths(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(
                project=project_dir,
                add_include_paths=["./include", "./vendor"],
            )
        tree = ET.parse(project_dir / ".cproject")
        inc_values = []
        for opt in tree.iter("option"):
            if opt.get("superClass") == "gnu.c.compiler.option.includepaths":
                for child in opt.findall("listOptionValue"):
                    inc_values.append(child.get("value"))
        assert "./include" in inc_values
        assert "./vendor" in inc_values

    def test_add_libraries_appends_lib_ref_and_search_dir(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        """add_libraries appends the library reference to
        ...linker.option.libraries and its parent dir to
        ...linker.option.directories. A ``lib<name>.a`` becomes ``<name>``
        (-l<name>); a non-prefixed ``.a`` becomes ``:<file>`` (-l:<file>)."""
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(
                project=project_dir,
                add_libraries=[
                    Path("vendor/gcc/libmylib.a"),
                    Path("vendor/gcc/Runtime_CM55_GCC.a"),
                ],
            )
        tree = ET.parse(project_dir / ".cproject")
        libs, dirs = [], []
        for opt in tree.iter("option"):
            sc = opt.get("superClass")
            if sc == "gnu.c.linker.option.libraries":
                libs += [c.get("value") for c in opt.findall("listOptionValue")]
            elif sc == "gnu.c.linker.option.directories":
                dirs += [c.get("value") for c in opt.findall("listOptionValue")]
        assert "mylib" in libs              # lib<name>.a -> -lmylib
        assert ":Runtime_CM55_GCC.a" in libs  # bare .a -> -l:literal
        assert "vendor/gcc" in dirs or str(Path("vendor/gcc")) in dirs

    def test_add_sources_copies_file_into_project(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        """add_sources copies the source into the project's scanned tree
        (default dest = project/<name>) so the catch-all sourcePath builds
        it. Bare Path form."""
        src = tmp_path / "external" / "helper.c"
        src.parent.mkdir(parents=True)
        src.write_text("int helper(void){return 1;}\n", encoding="utf-8")
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(project=project_dir, add_sources=[src])
        copied = project_dir / "helper.c"
        assert copied.is_file()
        assert copied.read_text(encoding="utf-8") == "int helper(void){return 1;}\n"

    def test_modify_all_configurations(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(
                project=project_dir,
                optimization="-Os",
                modify_all_configurations=True,
            )
        tree = ET.parse(project_dir / ".cproject")
        opt_values = sorted(
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass") == "gnu.c.compiler.option.optimization.level"
        )
        # Both configurations updated to the size enum value
        # ("<superClass>.value.size") via the "..." expansion.
        assert len(opt_values) == 2
        assert all(v.endswith(".value.size") for v in opt_values), opt_values


# ---------------------------------------------------------------------------
# Preset path
# ---------------------------------------------------------------------------


class TestPreset:
    def test_preset_size_applies_multi_edit(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir, preset="size")
        # Multiple SettingChange records from one preset application.
        assert result.settings_modification is not None
        assert len(result.settings_modification.changes) >= 3

    def test_preset_fast_without_family_warns_soft_fp(
        self,
        ctx: SubstrateContext,
        project_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeIDE(ctx)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.cubeide"):
            with patch(
                "embedagents.stm32.cubeide.headless.run_tool",
                return_value=_build_run_tool_success(),
            ):
                client.build(project=project_dir, preset="fast")
        # No firmware.device_family in descriptor → soft-FP fallback warns.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("soft-FP" in r.message or "device_family" in r.message for r in warnings)

    def test_preset_fast_with_family_writes_fpu_flags(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """firmware.device_family=STM32L4 → -mfpu=fpv4-sp-d16 -mfloat-abi=hard."""
        import json

        cubeide_bin, _ = _make_cubeide_stubs(tmp_path)
        monkeypatch.setenv("STM32CUBEIDE", str(cubeide_bin))

        descriptor = {
            "version": 1,
            "firmware": {"device_family": "STM32L4"},
        }
        (tmp_path / "stm32-project.jsonc").write_text(json.dumps(descriptor))

        proj = tmp_path / "demo"
        proj.mkdir()
        (proj / ".cproject").write_bytes(_make_cproject_xml())
        project_xml = ET.Element("projectDescription")
        name_el = ET.SubElement(project_xml, "name")
        name_el.text = "demo"
        ET.ElementTree(project_xml).write(proj / ".project")

        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeIDE(ctx2)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(project=proj, preset="fast")

        tree = ET.parse(proj / ".cproject")
        fpu_values = sorted(
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass") == "gnu.managedbuild.option.fpu"
        )
        abi_values = sorted(
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass") == "gnu.managedbuild.option.floatabi"
        )
        # Value strings are ST-prefix-expanded enum tokens
        # ("<superClass>.value.<token>") — verify the token suffix lands.
        assert any(v.endswith(".value.fpv4-sp-d16") for v in fpu_values), fpu_values
        assert any(v.endswith(".value.hard") for v in abi_values), abi_values


# ---------------------------------------------------------------------------
# Protocol-level failure path → rollback
# ---------------------------------------------------------------------------


class TestProtocolFailureRollback:
    def test_modify_with_no_match_raises_and_rolls_back(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        # Strip the .cproject's debugging option so set_option finds none → modify failure.
        tree = ET.parse(project_dir / ".cproject")
        for opt in list(tree.iter("option")):
            if opt.get("superClass") == "gnu.c.compiler.option.debugging.level":
                # Find parent + remove
                pass  # ElementTree doesn't have parent refs by default

        # Instead, point at an option that doesn't exist at all.
        client = CubeIDE(ctx)
        original_xml = (project_dir / ".cproject").read_bytes()

        # We need a way to force a protocol-level failure. The cleanest
        # is to provide an unknown superclass via a custom hook. Since
        # build() doesn't expose this directly, we test via the
        # CProjectEditor public surface in test_cubeide_cproject.py.
        # Here we verify build() forwards the error if it bubbles:
        from embedagents.stm32.cubeide import cproject as cproject_module

        def boom_snapshot(self):  # noqa: ARG001
            raise CProjectEditError(
                message="forced protocol failure",
                failed_step="parse",
                file=Path("/tmp/.cproject"),
            )

        with patch.object(
            cproject_module.CProjectEditor, "snapshot", boom_snapshot
        ):
            with patch(
                "embedagents.stm32.cubeide.headless.run_tool",
                return_value=_build_run_tool_success(),
            ):
                with pytest.raises(CProjectEditError):
                    client.build(project=project_dir, debug_level="-g3")

        # .cproject untouched (snapshot raised before any modify).
        assert (project_dir / ".cproject").read_bytes() == original_xml


# ---------------------------------------------------------------------------
# Workspace lock detection
# ---------------------------------------------------------------------------


class TestWorkspaceLock:
    def test_gui_lock_raises_workspace_locked(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        # Patch detect_workspace_lock to return True (GUI holds lock).
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.client.workspace.detect_workspace_lock",
            return_value=True,
        ):
            with patch(
                "embedagents.stm32.cubeide.headless.run_tool",
                return_value=_build_run_tool_success(),
            ) as mocked:
                with pytest.raises(WorkspaceLockedError) as excinfo:
                    client.build(project=project_dir)
        # Build subprocess never invoked.
        assert mocked.call_count == 0
        err = excinfo.value
        assert err.cubeide_marker == "workspace-locked"
        assert "GUI" in err.message


# ---------------------------------------------------------------------------
# Artifact/map detection
# ---------------------------------------------------------------------------


class TestArtifactDetection:
    def test_artifact_and_map_paths(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        # Create fake build output before the build runs.
        debug_dir = project_dir / "Debug"
        debug_dir.mkdir()
        elf = debug_dir / "demo.elf"
        elf.write_bytes(b"\x7fELF")
        map_path = debug_dir / "demo.map"
        map_path.write_text("ld map output")

        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir)
        assert result.artifact_path == elf
        assert result.map_path == map_path

    def test_missing_artifact_returns_none(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir)
        assert result.artifact_path is None
        assert result.map_path is None


class TestNothingToBuild:
    """A zero-exit 'Nothing to build' headless run is only a success when an
    artifact is present (up-to-date incremental rebuild); with no artifact
    it's a failure (e.g. a legacy SW4STM32/AC6 project CubeIDE can't drive),
    not a silent success. Regression for the L0/L1 bring-up finding."""

    def test_nothing_built_no_artifact_is_failure(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_nothing_built(),
        ):
            result = client.build(project=project_dir)
        assert result.exit_code == 0
        assert result.artifact_path is None
        assert result.success is False  # not a silent success
        assert "Nothing to build" in result.console_output

    def test_nothing_built_with_artifact_is_success(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        # Up-to-date incremental rebuild: "Nothing to build" but the ELF is
        # already there → genuine success, must not be flipped to failure.
        debug_dir = project_dir / "Debug"
        debug_dir.mkdir()
        (debug_dir / "demo.elf").write_bytes(b"\x7fELF")
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_nothing_built(),
        ):
            result = client.build(project=project_dir)
        assert result.success is True
        assert result.artifact_path == debug_dir / "demo.elf"



# ---------------------------------------------------------------------------
# F.2.2: per-OS headless script resolution
# ---------------------------------------------------------------------------


class TestResolveHeadlessBuild:
    """Direct tests for resolve_headless_build per-OS dispatch.

    Linux: probes ``headless-build.sh`` next to the CubeIDE binary.
    Windows: probes ``headless-build.bat``.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="Linux-only filename")
    def test_resolves_sh_on_linux(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from embedagents.stm32.cubeide.headless import resolve_headless_build

        cubeide_bin = tmp_path / "stm32cubeide"
        cubeide_bin.write_text("#!/bin/sh\nexit 0\n")
        cubeide_bin.chmod(0o755)
        script = tmp_path / "headless-build.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        monkeypatch.setenv("STM32CUBEIDE", str(cubeide_bin))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)

        assert resolve_headless_build(ctx=ctx) == script

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only filename")
    def test_resolves_bat_on_windows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from embedagents.stm32.cubeide.headless import resolve_headless_build

        cubeide_bin = tmp_path / "stm32cubeide.exe"
        cubeide_bin.write_bytes(b"")
        script = tmp_path / "headless-build.bat"
        script.write_text("@echo off\r\nexit /b 0\r\n")
        monkeypatch.setenv("STM32CUBEIDE", str(cubeide_bin))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)

        assert resolve_headless_build(ctx=ctx) == script

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only filename")
    def test_missing_bat_raises_with_filename_in_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows, the error message + hint name headless-build.bat
        (not headless-build.sh) so the user knows what to set."""
        from embedagents.stm32.cubeide.headless import resolve_headless_build

        cubeide_bin = tmp_path / "stm32cubeide.exe"
        cubeide_bin.write_bytes(b"")
        # NO headless-build.bat created.
        monkeypatch.setenv("STM32CUBEIDE", str(cubeide_bin))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)

        with pytest.raises(CubeIDEError) as excinfo:
            resolve_headless_build(ctx=ctx)
        assert excinfo.value.cubeide_marker == "headless-script-missing"
        assert "headless-build.bat" in excinfo.value.message
        assert "headless-build.bat" in (excinfo.value.hint or "")

    def test_explicit_override_honoured_via_direct_assignment(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """ctx.tools.cubeide_headless_build wins over the per-OS probe.

        ``ToolPaths`` is frozen, and the schema does not currently expose
        ``cubeide_headless_build`` as a settable key (see TODO(v1+) in
        stm32-tools.local.schema.json). The first branch of
        ``resolve_headless_build`` is reachable today only via programmatic
        injection — this test confirms it works when the field is set.
        """
        from embedagents.stm32.cubeide.headless import resolve_headless_build

        override = tmp_path / "my-headless.whatever"
        override.write_text("dummy\n")
        # ToolPaths is frozen; use object.__setattr__ to inject for the test.
        object.__setattr__(ctx.tools, "cubeide_headless_build", override)
        assert resolve_headless_build(ctx=ctx) == override


# ---------------------------------------------------------------------------
# IMP-10 — workspace mutations serialized under the substrate lock
# ---------------------------------------------------------------------------


class TestWorkspaceMutationOrdering:
    def test_cleanup_runs_under_substrate_lock(
        self,
        ctx: SubstrateContext,
        project_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """IMP-10: cleanup_stale_project (and the workspace mkdir) used
        to run BEFORE acquire_workspace_lock — a concurrent invocation
        could purge metadata out from under a running build, then raise
        WorkspaceLockedError. All workspace mutations now happen inside
        the lock."""
        import contextlib as _ctxlib

        from embedagents.stm32.cubeide import workspace as ws_mod

        events: list[str] = []
        real_lock = ws_mod.acquire_workspace_lock

        @_ctxlib.contextmanager
        def recording_lock(path: Path):
            events.append("lock-enter")
            with real_lock(path):
                yield
            events.append("lock-exit")

        monkeypatch.setattr(ws_mod, "acquire_workspace_lock", recording_lock)
        # Simulate a stale import: workspace says the project lives
        # somewhere else → cleanup path fires.
        monkeypatch.setattr(
            ws_mod,
            "detect_project_imported",
            lambda w, n: Path("/somewhere/else"),
        )
        monkeypatch.setattr(
            ws_mod,
            "cleanup_stale_project",
            lambda w, n, logger=None: events.append("cleanup"),
        )

        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(project=project_dir)
        assert result.success is True
        assert "cleanup" in events
        assert (
            events.index("lock-enter")
            < events.index("cleanup")
            < events.index("lock-exit")
        )


# ---------------------------------------------------------------------------
# A-009 — the "already exists in the workspace" single bounded retry
# (ratified RES-040; previously shipped unratified + untested)
# ---------------------------------------------------------------------------


def _already_exists_failure() -> ToolRunResult:
    return ToolRunResult(
        exit_code=1,
        stdout="",
        stderr='Project "demo" already exists in the workspace!\n',
        duration_s=0.3,
        timed_out=False,
    )


class TestAlreadyExistsRetry:
    def test_retries_once_without_import_then_succeeds(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        calls: list[list[str]] = []

        def record(binary, args, **kwargs):
            calls.append([str(a) for a in args])
            if len(calls) == 1:
                return _already_exists_failure()
            return _build_run_tool_success()

        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool", side_effect=record
        ):
            result = client.build(project=project_dir)
        assert result.success is True
        assert len(calls) == 2
        assert any("-import" in a for a in calls[0])
        assert not any("-import" in a for a in calls[1])

    def test_other_failures_do_not_retry(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        """M-017: the retry fires ONLY on the specific Eclipse
        hidden-tree marker text — any other failure stays one-shot."""
        calls: list[int] = []

        def record(binary, args, **kwargs):
            calls.append(1)
            return _build_run_tool_failure()

        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool", side_effect=record
        ):
            result = client.build(project=project_dir)
        assert result.success is False
        assert len(calls) == 1

    def test_retry_is_single_never_a_loop(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        calls: list[int] = []

        def record(binary, args, **kwargs):
            calls.append(1)
            return _already_exists_failure()

        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool", side_effect=record
        ):
            result = client.build(project=project_dir)
        assert result.success is False
        assert len(calls) == 2  # initial + exactly one retry


# ---------------------------------------------------------------------------
# ARC-02 + IMP-09 — add_sources/add_symbols gates and typed copy errors
# ---------------------------------------------------------------------------


class TestAddSourcesExistingGate:
    def _src(self, tmp_path: Path, content: str = "int helper(void){return 1;}\n") -> Path:
        src = tmp_path / "external" / "helper.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(content, encoding="utf-8")
        return src

    def test_existing_destination_without_callback_raises(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        src = self._src(tmp_path)
        (project_dir / "helper.c").write_text("ORIGINAL\n", encoding="utf-8")
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            with pytest.raises(CProjectEditError, match="already exists"):
                client.build(project=project_dir, add_sources=[src])
        # Pre-existing file untouched.
        assert (project_dir / "helper.c").read_text(encoding="utf-8") == "ORIGINAL\n"

    def test_on_existing_replace_overwrites(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        src = self._src(tmp_path)
        (project_dir / "helper.c").write_text("ORIGINAL\n", encoding="utf-8")
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(
                project=project_dir,
                add_sources=[src],
                on_existing=lambda p: "replace",
            )
        assert "helper(void)" in (project_dir / "helper.c").read_text(
            encoding="utf-8"
        )

    def test_on_existing_skip_leaves_destination(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        src = self._src(tmp_path)
        (project_dir / "helper.c").write_text("ORIGINAL\n", encoding="utf-8")
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            result = client.build(
                project=project_dir,
                add_sources=[src],
                on_existing=lambda p: "skip",
            )
        assert (project_dir / "helper.c").read_text(encoding="utf-8") == "ORIGINAL\n"
        assert isinstance(result, BuildResult)

    def test_on_existing_rename_copies_under_new_name(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        src = self._src(tmp_path)
        (project_dir / "helper.c").write_text("ORIGINAL\n", encoding="utf-8")
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            client.build(
                project=project_dir,
                add_sources=[src],
                on_existing=lambda p: "rename",
            )
        assert (project_dir / "helper.c").read_text(encoding="utf-8") == "ORIGINAL\n"
        assert (project_dir / "helper-1.c").is_file()

    def test_missing_source_raises_typed_error(
        self, ctx: SubstrateContext, project_dir: Path, tmp_path: Path
    ) -> None:
        # IMP-09: was a raw FileNotFoundError straight through build().
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            with pytest.raises(CProjectEditError, match="copy failed"):
                client.build(
                    project=project_dir,
                    add_sources=[tmp_path / "no-such-file.c"],
                )


class TestAddSymbolsConflictGate:
    def _build(self, ctx: SubstrateContext, project_dir: Path, **kwargs):
        client = CubeIDE(ctx)
        with patch(
            "embedagents.stm32.cubeide.headless.run_tool",
            return_value=_build_run_tool_success(),
        ):
            return client.build(project=project_dir, **kwargs)

    def _symbols(self, project_dir: Path) -> list[str]:
        tree = ET.parse(project_dir / ".cproject")
        out = []
        for opt in tree.iter("option"):
            if opt.get("superClass") == "gnu.c.compiler.option.definedsymbols":
                out += [c.get("value") for c in opt.findall("listOptionValue")]
        return out

    def test_conflicting_value_without_callback_raises(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        self._build(ctx, project_dir, add_symbols=[("MY_VER", "1")])
        with pytest.raises(CProjectEditError, match="already defined"):
            self._build(ctx, project_dir, add_symbols=[("MY_VER", "2")])
        # Rollback kept the original definition.
        assert "MY_VER=1" in self._symbols(project_dir)
        assert "MY_VER=2" not in self._symbols(project_dir)

    def test_on_conflict_replace_swaps_value(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        self._build(ctx, project_dir, add_symbols=[("MY_VER", "1")])
        seen = []
        self._build(
            ctx,
            project_dir,
            add_symbols=[("MY_VER", "2")],
            on_conflict=lambda name, old, new: (seen.append((name, old, new)), "replace")[1],
        )
        assert seen == [("MY_VER", "MY_VER=1", "MY_VER=2")]
        symbols = self._symbols(project_dir)
        assert "MY_VER=2" in symbols
        assert "MY_VER=1" not in symbols

    def test_on_conflict_skip_keeps_existing(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        self._build(ctx, project_dir, add_symbols=[("MY_VER", "1")])
        self._build(
            ctx,
            project_dir,
            add_symbols=[("MY_VER", "2")],
            on_conflict=lambda name, old, new: "skip",
        )
        symbols = self._symbols(project_dir)
        assert "MY_VER=1" in symbols
        assert "MY_VER=2" not in symbols

    def test_on_conflict_abort_raises(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        self._build(ctx, project_dir, add_symbols=[("MY_VER", "1")])
        with pytest.raises(CProjectEditError, match="abort"):
            self._build(
                ctx,
                project_dir,
                add_symbols=[("MY_VER", "2")],
                on_conflict=lambda name, old, new: "abort",
            )

    def test_same_value_is_not_a_conflict(
        self, ctx: SubstrateContext, project_dir: Path
    ) -> None:
        self._build(ctx, project_dir, add_symbols=[("MY_VER", "1")])
        # Re-adding the identical definition dedupes silently; the
        # callback must NOT fire.
        def _no_call(name, old, new):
            raise AssertionError("on_conflict fired for an identical value")

        self._build(
            ctx,
            project_dir,
            add_symbols=[("MY_VER", "1")],
            on_conflict=_no_call,
        )
        assert self._symbols(project_dir).count("MY_VER=1") == 1
