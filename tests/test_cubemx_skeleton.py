"""C3a skeleton tests — cubemx package imports, dataclasses frozen,
launcher resolution, script-construction helpers."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubemx import CubeMX, CubeMXResult, ProgressEvent
from embedagents.stm32.cubemx.client import (
    EXIT_COMMAND,
    _FORBIDDEN_SCRIPT_CHARS,
    _quote,
)
from embedagents.stm32.cubemx.launcher import resolve_cubemx_launcher
from embedagents.stm32.errors import CubeMXError, CubeMXLauncherError


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


class TestResultDataclasses:
    def test_cubemx_result_is_frozen(self, tmp_path: Path) -> None:
        result = CubeMXResult(
            success=True,
            exit_code=0,
            duration_s=1.0,
            timed_out=False,
            extensions_used=0,
            output_dir=tmp_path,
            log_path=tmp_path / "log",
            cubemx_log_path=None,
            script_text="x",
        )
        assert is_dataclass(result)
        with pytest.raises(FrozenInstanceError):
            result.success = False  # type: ignore[misc]
        assert result.terminated_after_marker is False

    def test_progress_event_shape(self) -> None:
        e = ProgressEvent(
            stage="cubemx_running",
            duration_s=12.0,
            deadline_s=300.0,
            extensions_used=0,
        )
        assert is_dataclass(e)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_launcher_error_is_cubemx_error(self) -> None:
        assert issubclass(CubeMXLauncherError, CubeMXError)

    def test_launcher_error_carries_candidates(self) -> None:
        err = CubeMXLauncherError(
            message="not resolvable",
            checked_candidates=("/nope", "$(which STM32CubeMX)"),
        )
        assert err.checked_candidates == ("/nope", "$(which STM32CubeMX)")

    def test_cubemx_error_extra_fields(self, tmp_path: Path) -> None:
        err = CubeMXError(
            message="missing IOC",
            cubemx_marker="ioc-missing",
            ioc_path=tmp_path / "nope.ioc",
            output_dir=tmp_path,
        )
        assert err.cubemx_marker == "ioc-missing"
        assert err.ioc_path == tmp_path / "nope.ioc"
        assert err.output_dir == tmp_path


# ---------------------------------------------------------------------------
# Launcher resolution
# ---------------------------------------------------------------------------


class TestResolveLauncher:
    def test_explicit_executable_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "STM32CubeMX"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setenv("STM32CUBEMX_PATH", str(fake))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert resolve_cubemx_launcher(ctx) == fake

    def test_path_lookup_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No explicit override; STM32CubeMX on a temp PATH. Per-OS file
        # naming because shutil.which on Windows resolves PATHEXT
        # extensions (.exe / .bat / .cmd) while POSIX uses bare names +
        # +x mode.
        import sys

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        if sys.platform == "win32":
            fake = bin_dir / "STM32CubeMX.exe"
            fake.write_bytes(b"")
        else:
            fake = bin_dir / "STM32CubeMX"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
        monkeypatch.delenv("STM32CUBEMX_PATH", raising=False)
        monkeypatch.setenv("PATH", str(bin_dir))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert resolve_cubemx_launcher(ctx) == fake

    def test_unresolved_raises_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32CUBEMX_PATH", raising=False)
        monkeypatch.setenv("PATH", "")
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        with pytest.raises(CubeMXLauncherError) as excinfo:
            resolve_cubemx_launcher(ctx)
        err = excinfo.value
        assert err.hint is not None
        assert "STM32CubeMX" in err.hint
        # PATH-lookup candidate always recorded.
        assert "$(which STM32CubeMX)" in err.checked_candidates


# ---------------------------------------------------------------------------
# Script-construction helpers
# ---------------------------------------------------------------------------


class TestQuote:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("simple", "simple"),
            ("STM32CubeIDE", "STM32CubeIDE"),
            ("/home/user/path", "/home/user/path"),
            ("path with space", '"path with space"'),
            ("name with spaces here", '"name with spaces here"'),
        ],
    )
    def test_normal_values(self, value: str, expected: str) -> None:
        assert _quote(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            'has"quote',
            'mixed "and trailing',
        ],
    )
    def test_forbidden_chars_raise(self, value: str) -> None:
        """Double-quote is unescapable in CubeMX's script parser; refuse
        loudly. Backslash is NOT refused — substrate normalises it to a
        forward slash since CubeMX's Java parser accepts both forms."""
        with pytest.raises(ValueError, match="unsupported character"):
            _quote(value)

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("has\\backslash", "has/backslash"),
            ("C:\\Users\\x", "C:/Users/x"),
            ("mixed\\path here", '"mixed/path here"'),  # space → quoted
        ],
    )
    def test_backslashes_normalised(self, value: str, expected: str) -> None:
        """Backslash normalisation runs before quoting; spaces in the
        normalised form still trigger quoting."""
        assert _quote(value) == expected


class TestForbiddenScriptChars:
    def test_set_contents(self) -> None:
        # Backslash dropped from the forbidden set as of F.3 (RES-025 /
        # ADR-007): substrate normalises backslash → forward slash so the
        # produced script is platform-uniform.
        assert set(_FORBIDDEN_SCRIPT_CHARS) == {'"'}


class TestExitCommand:
    def test_hardcoded_value(self) -> None:
        assert EXIT_COMMAND == "exit_mx"


# ---------------------------------------------------------------------------
# CubeMX skeleton
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx_with_cubemx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake = tmp_path / "STM32CubeMX"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("STM32CUBEMX_PATH", str(fake))
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestCubeMXSkeleton:
    def test_construct(self, ctx_with_cubemx: SubstrateContext) -> None:
        client = CubeMX(ctx_with_cubemx)
        assert client.ctx is ctx_with_cubemx
        assert client._log.name == "embedagents.stm32.cubemx"

    # generate() implemented in C3c → tests in test_cubemx_generate.py.
