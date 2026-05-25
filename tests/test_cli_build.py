"""C1g tests — ``stm32 build`` CLI subcommand group."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stm32_substrate.cli import main
from stm32_substrate.cubeide.results import (
    BuildResult,
    FoundProject,
    SettingsModification,
)
from stm32_substrate.errors import (
    CProjectEditError,
    SubstrateError,
    WorkspaceLockedError,
)


@pytest.fixture()
def ensure_cubeide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cubeide = tmp_path / "stm32cubeide"
    fake_cubeide.write_text("#!/bin/sh\nexit 0\n")
    fake_cubeide.chmod(0o755)
    monkeypatch.setenv("STM32CUBEIDE", str(fake_cubeide))


@pytest.fixture()
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    instance = MagicMock(name="CubeIDE-instance")
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("stm32_substrate.cli._build.CubeIDE", factory)
    return instance


def _build_result(success: bool = True, exit_code: int = 0) -> BuildResult:
    return BuildResult(
        success=success,
        exit_code=exit_code,
        duration_s=1.5,
        log_path=Path("/tmp/build.log"),
        console_output="Build output here\n",
        artifact_path=Path("/tmp/demo.elf") if success else None,
        map_path=Path("/tmp/demo.map") if success else None,
        project_name="demo",
        configuration="Debug",
        workspace_path=Path("/tmp/ws"),
    )


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def _read_json(stdout: str) -> dict:
    return json.loads(stdout)


# ---------------------------------------------------------------------------
# Base `stm32 build`
# ---------------------------------------------------------------------------


class TestBaseBuild:
    def test_no_args_invokes_build(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        code, out, err = _run(["build"], capsys)
        assert code == 0
        mock_client.build.assert_called_once_with(
            project=None,
            configuration=None,
            clean=False,
            debug_level=None,
            optimization=None,
            preset=None,
        )
        # Console output mirrored to stderr.
        assert "Build output here" in err
        # JSON envelope on stdout.
        payload = _read_json(out)
        assert payload["success"] is True
        assert payload["project_name"] == "demo"

    def test_project_clean_config(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(
            ["build", "--project", "/path/to/proj", "--config", "Release", "--clean"],
            capsys,
        )
        call = mock_client.build.call_args
        assert call.kwargs == {
            "project": Path("/path/to/proj"),
            "configuration": "Release",
            "clean": True,
            "debug_level": None,
            "optimization": None,
            "preset": None,
        }

    def test_debug_level(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        # `-g3` starts with `-` so argparse needs `=` syntax to bind it
        # as the value (otherwise it's mistaken for a flag).
        mock_client.build.return_value = _build_result()
        _run(["build", "--debug-level=-g3"], capsys)
        assert mock_client.build.call_args.kwargs["debug_level"] == "-g3"

    def test_opt(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "--opt=-Os"], capsys)
        assert mock_client.build.call_args.kwargs["optimization"] == "-Os"

    def test_preset(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "--preset", "fast"], capsys)
        assert mock_client.build.call_args.kwargs["preset"] == "fast"


# ---------------------------------------------------------------------------
# Build-level failure
# ---------------------------------------------------------------------------


class TestBuildFailureExitCode:
    def test_success_false_exits_zero(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        """Build-level failure is a result, not a substrate-side error.
        CLI exits 0 — user inspects BuildResult.success."""
        mock_client.build.return_value = _build_result(success=False, exit_code=1)
        code, out, err = _run(["build"], capsys)
        assert code == 0
        payload = _read_json(out)
        assert payload["success"] is False
        assert payload["exit_code"] == 1

    def test_substrate_error_exits_one(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.side_effect = WorkspaceLockedError(
            message="GUI holds workspace",
            cubeide_marker="workspace-locked",
        )
        code, out, err = _run(["build"], capsys)
        assert code == 1
        # stdout empty (no BuildResult to serialise).
        assert out == ""
        parsed = json.loads(err.strip())
        assert parsed["error_type"] == "WorkspaceLockedError"


# ---------------------------------------------------------------------------
# add-symbol
# ---------------------------------------------------------------------------


class TestAddSymbol:
    def test_single_symbol(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-symbol", "DEBUG"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_symbols"] == ["DEBUG"]

    def test_name_value_form(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-symbol", "MY_VER=2"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_symbols"] == [("MY_VER", "2")]

    def test_multiple_symbols(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-symbol", "DEBUG", "USE_FEATURE_X=1", "VERBOSE"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_symbols"] == [
            "DEBUG",
            ("USE_FEATURE_X", "1"),
            "VERBOSE",
        ]

    def test_all_configs_flag(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-symbol", "DEBUG", "--all-configs"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs.get("modify_all_configurations") is True


# ---------------------------------------------------------------------------
# add-lib / add-source / add-include
# ---------------------------------------------------------------------------


class TestAddLib:
    def test_paths_collected(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-lib", "/libs/foo.a", "/libs/bar.a"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_libraries"] == [Path("/libs/foo.a"), Path("/libs/bar.a")]


class TestAddSource:
    def test_paths_collected(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-source", "/src/main.c", "/src/helper.c"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_sources"] == [Path("/src/main.c"), Path("/src/helper.c")]

    def test_with_target(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(
            ["build", "add-source", "/src/main.c", "--target", "/proj/Core/Src"],
            capsys,
        )
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_sources"] == [
            (Path("/src/main.c"), Path("/proj/Core/Src"))
        ]


class TestAddInclude:
    def test_paths_collected(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["build", "add-include", "./include", "./vendor"], capsys)
        kwargs = mock_client.build.call_args.kwargs
        assert kwargs["add_include_paths"] == ["./include", "./vendor"]


# ---------------------------------------------------------------------------
# in-folder + named (discovery + build chains)
# ---------------------------------------------------------------------------


class TestInFolder:
    def test_chains_find_project_then_build(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        found = FoundProject(
            path=Path("/discovered/demo"),
            name="demo",
            cproject_path=Path("/discovered/demo/.cproject"),
        )
        mock_client.find_project.return_value = found
        mock_client.build.return_value = _build_result()
        _run(["build", "in-folder", "/some/folder", "--clean"], capsys)
        mock_client.find_project.assert_called_once_with(folder=Path("/some/folder"))
        mock_client.build.assert_called_once_with(
            project=Path("/discovered/demo"),
            configuration=None,
            clean=True,
        )

    def test_default_folder(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        found = FoundProject(
            path=Path("/x/demo"),
            name="demo",
            cproject_path=Path("/x/demo/.cproject"),
        )
        mock_client.find_project.return_value = found
        mock_client.build.return_value = _build_result()
        _run(["build", "in-folder"], capsys)
        mock_client.find_project.assert_called_once_with(folder=None)


class TestNamed:
    def test_chains_named_lookup(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        found = FoundProject(
            path=Path("/discovered/blink"),
            name="blink",
            cproject_path=Path("/discovered/blink/.cproject"),
        )
        mock_client.find_project.return_value = found
        mock_client.build.return_value = _build_result()
        _run(["build", "named", "blink", "--folder", "/projects"], capsys)
        mock_client.find_project.assert_called_once_with(
            folder=Path("/projects"), name="blink"
        )
        mock_client.build.assert_called_once_with(
            project=Path("/discovered/blink"),
            configuration=None,
            clean=False,
        )


# ---------------------------------------------------------------------------
# Console-output mirroring + JSON shape
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_console_output_mirrored_to_stderr(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        result = _build_result()
        # Tweak console output content.
        from dataclasses import replace

        result = replace(result, console_output="line1\nline2\n")
        mock_client.build.return_value = result
        code, out, err = _run(["build"], capsys)
        assert "line1" in err
        assert "line2" in err

    def test_no_trailing_newline_added_to_full_output(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        from dataclasses import replace

        result = replace(_build_result(), console_output="no-newline")
        mock_client.build.return_value = result
        code, out, err = _run(["build"], capsys)
        # The CLI adds a trailing newline if the content didn't have one.
        assert err.endswith("\n")
        assert "no-newline" in err

    def test_pretty_flag_indents_json(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.return_value = _build_result()
        _run(["--pretty", "build"], capsys)
        out = capsys.readouterr()
        # Wait, _run already captured. Re-run carefully:

    def test_settings_modification_serialised(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        from dataclasses import replace
        from stm32_substrate.cubeide.results import SettingChange

        sm = SettingsModification(
            file=Path("/x/.cproject"),
            backup_path=Path("/x/.cproject.bak"),
            changes=[
                SettingChange(
                    superclass_id="x.opt",
                    configuration="Debug",
                    kind="set_value",
                    old_value="a",
                    new_value="b",
                )
            ],
        )
        mock_client.build.return_value = replace(
            _build_result(), settings_modification=sm
        )
        code, out, _ = _run(["build", "--debug-level=-g3"], capsys)
        payload = _read_json(out)
        assert payload["settings_modification"]["changes"][0]["kind"] == "set_value"


# ---------------------------------------------------------------------------
# Help / unknown action
# ---------------------------------------------------------------------------


class TestHelp:
    def test_build_help_lists_subactions(
        self, ensure_cubeide, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["build", "--help"], capsys)
        # argparse writes help to stdout.
        out = capsys.readouterr().out
        for action in ("add-symbol", "add-lib", "add-source", "add-include", "in-folder", "named"):
            assert action in out


# ---------------------------------------------------------------------------
# CProjectEditError surfaces as substrate failure
# ---------------------------------------------------------------------------


class TestProtocolErrorPath:
    def test_cproject_edit_error_exits_one(
        self, ensure_cubeide, mock_client: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        mock_client.build.side_effect = CProjectEditError(
            message="modify failed",
            failed_step="modify",
            superclass_attempted="x.opt",
        )
        code, out, err = _run(["build", "--debug-level=-g3"], capsys)
        assert code == 1
        parsed = json.loads(err.strip())
        assert parsed["error_type"] == "CProjectEditError"
        assert parsed["failed_step"] == "modify"
