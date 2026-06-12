"""C3c tests — CubeMX.generate() end-to-end + inline script construction.

The runner-loop itself is exercised in test_cubemx_runner.py with
mocked subprocess + clock. Here we test the orchestration on top of
the runner: kwargs/descriptor resolution, ioc-missing precheck,
toolchain runtime guard, script-builder output, and the runner-glue
contract."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubemx import CubeMX, CubeMXResult
from stm32_substrate.cubemx.client import _build_script
from stm32_substrate.errors import CubeMXError


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake = tmp_path / "STM32CubeMX"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("STM32CUBEMX_PATH", str(fake))
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def ioc(tmp_path: Path) -> Path:
    p = tmp_path / "demo.ioc"
    p.write_text("# ioc placeholder")
    return p


def _fake_result(output_dir: Path, log_path: Path) -> CubeMXResult:
    return CubeMXResult(
        success=True,
        exit_code=0,
        duration_s=1.0,
        timed_out=False,
        extensions_used=0,
        output_dir=output_dir,
        log_path=log_path,
        cubemx_log_path=None,
        script_text="",
    )


# ---------------------------------------------------------------------------
# Inline script construction (RES-020 — three fixture scenarios)
# ---------------------------------------------------------------------------


class TestBuildScript:
    def test_canonical_script(self, tmp_path: Path) -> None:
        ioc = tmp_path / "demo.ioc"
        output = tmp_path / "out"
        text = _build_script(
            ioc_path=ioc,
            output_path=output,
            project_name="demo",
            toolchain="STM32CubeIDE",
        )
        # Order + content per spec § "Inline script construction". Paths
        # are emitted with forward slashes regardless of host OS — CubeMX's
        # Java parser accepts both transparently, and substrate normalises
        # to forward slashes per _quote().
        lines = text.rstrip("\n").splitlines()
        ioc_norm = str(ioc).replace("\\", "/")
        output_norm = str(output).replace("\\", "/")
        assert lines[0] == f"config load {ioc_norm}"
        assert lines[1] == "project name demo"
        assert lines[2] == f"project path {output_norm}"
        assert lines[3] == "project toolchain STM32CubeIDE"
        assert lines[4] == "project generate"
        assert lines[5] == "exit_mx"

    def test_spaced_paths_quoted(self, tmp_path: Path) -> None:
        ioc = tmp_path / "project with space" / "demo.ioc"
        output = tmp_path / "out dir with space"
        text = _build_script(
            ioc_path=ioc,
            output_path=output,
            project_name="my demo",
            toolchain="STM32CubeIDE",
        )
        ioc_norm = str(ioc).replace("\\", "/")
        output_norm = str(output).replace("\\", "/")
        assert f'config load "{ioc_norm}"' in text
        assert f'project path "{output_norm}"' in text
        assert 'project name "my demo"' in text
        # Plain values stay unquoted.
        assert "project toolchain STM32CubeIDE" in text

    def test_quoted_chars_refused_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unsupported character"):
            _build_script(
                ioc_path=tmp_path / 'bad"path.ioc',
                output_path=tmp_path / "out",
                project_name="demo",
                toolchain="STM32CubeIDE",
            )

    def test_backslashes_normalised_to_forward_slashes(self, tmp_path: Path) -> None:
        """Backslashes are no longer refused — they're normalised to ``/``
        so the substrate emits platform-uniform scripts. CubeMX's Java
        parser accepts both forms on Windows."""
        text = _build_script(
            ioc_path=Path(r"C:\some\path\demo.ioc"),
            output_path=Path(r"C:\some\out"),
            project_name="my\\subname",
            toolchain="STM32CubeIDE",
        )
        # Forward slashes in the emitted script; no raw backslashes.
        assert "\\" not in text
        assert "C:/some/path/demo.ioc" in text
        assert "C:/some/out" in text
        assert "my/subname" in text

    def test_ends_with_newline(self, tmp_path: Path) -> None:
        text = _build_script(
            ioc_path=tmp_path / "demo.ioc",
            output_path=tmp_path / "out",
            project_name="demo",
            toolchain="STM32CubeIDE",
        )
        assert text.endswith("\n")


# ---------------------------------------------------------------------------
# ioc-missing precheck
# ---------------------------------------------------------------------------


class TestIocMissing:
    def test_nonexistent_ioc_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        client = CubeMX(ctx)
        with pytest.raises(CubeMXError) as excinfo:
            client.generate(tmp_path / "missing.ioc")
        err = excinfo.value
        assert err.cubemx_marker == "ioc-missing"
        assert err.recoverable is True

    def test_wrong_suffix_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        bogus = tmp_path / "wrong.txt"
        bogus.write_text("")
        client = CubeMX(ctx)
        with pytest.raises(CubeMXError) as excinfo:
            client.generate(bogus)
        assert excinfo.value.cubemx_marker == "ioc-missing"

    def test_ioc_suffix_case_insensitive(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        upper = tmp_path / "demo.IOC"
        upper.write_text("")
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(
                tmp_path, tmp_path / "log"
            )
            client.generate(upper)
        runner_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Toolchain runtime guard
# ---------------------------------------------------------------------------


class TestToolchainGuard:
    def test_ewarm_rejected(self, ctx: SubstrateContext, ioc: Path) -> None:
        client = CubeMX(ctx)
        with pytest.raises(ValueError, match="STM32CubeIDE"):
            client.generate(ioc, toolchain="EWARM")  # type: ignore[arg-type]

    def test_cubeide_accepted(self, ctx: SubstrateContext, ioc: Path) -> None:
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(ioc.parent, Path("/tmp/log"))
            client.generate(ioc, toolchain="STM32CubeIDE")
        runner_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Path resolution (kwargs + descriptor + defaults)
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_explicit_args_win(
        self, ctx: SubstrateContext, ioc: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "explicit_out"
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(output, Path("/tmp/log"))
            client.generate(ioc, output_path=output, project_name="custom")
        call = runner_mock.call_args
        # CubeMX writes the source tree at <output>/<name>/ and the
        # Eclipse project at <output>/<name>/<toolchain>/ for the
        # STM32CubeIDE toolchain. output_dir = Eclipse project root.
        assert call.kwargs["output_dir"] == output.resolve() / "custom" / "STM32CubeIDE"
        # Script text contains the explicit name.
        assert "project name custom" in call.kwargs["script_text"]

    def test_output_defaults_to_ioc_parent(
        self, ctx: SubstrateContext, ioc: Path
    ) -> None:
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(
                ioc.parent, Path("/tmp/log")
            )
            client.generate(ioc)
        call = runner_mock.call_args
        # output_path defaults to ioc.parent; project_name defaults to
        # ioc.stem ("demo"); CubeMX double-nests at
        # <output>/<name>/<toolchain>/.
        assert (
            call.kwargs["output_dir"]
            == ioc.parent / ioc.stem / "STM32CubeIDE"
        )

    def test_name_defaults_to_ioc_stem(
        self, ctx: SubstrateContext, ioc: Path
    ) -> None:
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(
                ioc.parent, Path("/tmp/log")
            )
            client.generate(ioc)
        call = runner_mock.call_args
        assert f"project name {ioc.stem}" in call.kwargs["script_text"]

    def test_descriptor_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No kwarg → ctx.project.cubemx.ioc_path."""
        ioc = tmp_path / "fromdesc.ioc"
        ioc.write_text("")
        fake = tmp_path / "STM32CubeMX"
        fake.write_text("")
        fake.chmod(0o755)
        monkeypatch.setenv("STM32CUBEMX_PATH", str(fake))

        descriptor = {
            "version": 1,
            "cubemx": {"ioc_path": str(ioc), "project_name": "fromdesc"},
        }
        (tmp_path / "stm32-project.jsonc").write_text(json.dumps(descriptor))
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)

        client = CubeMX(ctx2)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(tmp_path, Path("/tmp/log"))
            client.generate()  # no kwargs at all
        call = runner_mock.call_args
        assert "project name fromdesc" in call.kwargs["script_text"]
        assert "config load" in call.kwargs["script_text"]
        # Script emits forward-slashed paths (per _quote normalisation);
        # raw Path str on Windows contains backslashes, so compare against
        # the normalised form.
        assert str(ioc).replace("\\", "/") in call.kwargs["script_text"]

    def test_no_ioc_anywhere_raises(self, ctx: SubstrateContext) -> None:
        # IMP-41: ConfigurationError with a hint, consistent with every
        # other resolver (was a bare ValueError).
        from stm32_substrate.errors import ConfigurationError

        client = CubeMX(ctx)
        with pytest.raises(ConfigurationError, match="cubemx.ioc_path"):
            client.generate()  # no kwarg + no descriptor field


# ---------------------------------------------------------------------------
# generate() routes correctly through runner
# ---------------------------------------------------------------------------


class TestGenerateContract:
    def test_returns_runner_result(
        self, ctx: SubstrateContext, ioc: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out"
        expected = _fake_result(output, tmp_path / "log")
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx",
            return_value=expected,
        ):
            result = client.generate(ioc, output_path=output)
        assert result is expected

    def test_timeout_passed_to_runner(
        self, ctx: SubstrateContext, ioc: Path
    ) -> None:
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(
                ioc.parent, Path("/tmp/log")
            )
            client.generate(ioc, timeout_s=600.0)
        assert runner_mock.call_args.kwargs["timeout_s"] == 600.0

    def test_marker_path_under_output(
        self, ctx: SubstrateContext, ioc: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "out"
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(output, Path("/tmp/log"))
            client.generate(ioc, output_path=output)
        call = runner_mock.call_args
        # Marker lives inside the CubeMX-created Eclipse project subdir
        # (<output>/<project_name>/<toolchain>/.cproject); project_name
        # defaults to ioc.stem ("demo") here.
        assert (
            call.kwargs["expected_marker"]
            == output.resolve() / ioc.stem / "STM32CubeIDE" / ".cproject"
        )

    def test_output_directory_created(
        self, ctx: SubstrateContext, ioc: Path, tmp_path: Path
    ) -> None:
        nested = tmp_path / "nested" / "out"
        assert not nested.exists()
        client = CubeMX(ctx)
        with patch(
            "stm32_substrate.cubemx.client.runner.run_cubemx"
        ) as runner_mock:
            runner_mock.return_value = _fake_result(nested, Path("/tmp/log"))
            client.generate(ioc, output_path=nested)
        assert nested.is_dir()


# ---------------------------------------------------------------------------
# CLI: stm32 mx generate
# ---------------------------------------------------------------------------


class TestCLIMxGenerate:
    def test_basic_invocation(
        self,
        ctx: SubstrateContext,
        ioc: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from stm32_substrate.cli import main

        output = tmp_path / "out"
        fake_result = _fake_result(output, tmp_path / "log")

        from unittest.mock import MagicMock

        client_mock = MagicMock()
        client_mock.generate.return_value = fake_result
        factory = MagicMock(return_value=client_mock)
        monkeypatch.setattr("stm32_substrate.cli._mx.CubeMX", factory)

        code = main(
            [
                "mx", "generate",
                str(ioc),
                "--output", str(output),
                "--name", "demo",
                "--timeout", "120",
            ]
        )
        captured = capsys.readouterr()
        assert code == 0
        client_mock.generate.assert_called_once_with(
            ioc, output_path=output, project_name="demo", timeout_s=120.0
        )
        payload = json.loads(captured.out)
        assert payload["success"] is True

    def test_help_lists_generate(
        self, ctx: SubstrateContext, capsys: pytest.CaptureFixture
    ) -> None:
        from stm32_substrate.cli import main

        with pytest.raises(SystemExit):
            main(["mx", "--help"])
        out = capsys.readouterr().out
        assert "generate" in out

    def test_cubemx_error_exits_one(
        self,
        ctx: SubstrateContext,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from stm32_substrate.cli import main
        from unittest.mock import MagicMock

        client_mock = MagicMock()
        client_mock.generate.side_effect = CubeMXError(
            message="missing IOC",
            cubemx_marker="ioc-missing",
            ioc_path=tmp_path / "nope.ioc",
        )
        factory = MagicMock(return_value=client_mock)
        monkeypatch.setattr("stm32_substrate.cli._mx.CubeMX", factory)

        code = main(["mx", "generate", str(tmp_path / "nope.ioc")])
        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        parsed = json.loads(captured.err.strip())
        assert parsed["error_type"] == "CubeMXError"
        assert parsed["cubemx_marker"] == "ioc-missing"

    def test_success_false_still_exits_zero(
        self,
        ctx: SubstrateContext,
        ioc: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from stm32_substrate.cli import main
        from unittest.mock import MagicMock

        client_mock = MagicMock()
        client_mock.generate.return_value = CubeMXResult(
            success=False,
            exit_code=1,
            duration_s=10.0,
            timed_out=False,
            extensions_used=0,
            output_dir=tmp_path,
            log_path=tmp_path / "log",
            cubemx_log_path=tmp_path / "cubemx.log",
            script_text="",
        )
        factory = MagicMock(return_value=client_mock)
        monkeypatch.setattr("stm32_substrate.cli._mx.CubeMX", factory)

        code = main(["mx", "generate", str(ioc)])
        captured = capsys.readouterr()
        assert code == 0  # failure is a result, not an error
        payload = json.loads(captured.out)
        assert payload["success"] is False
