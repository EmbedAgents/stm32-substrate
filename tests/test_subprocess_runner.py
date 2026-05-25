"""Unit tests for ``run_tool``.

Tests spawn short-lived python subprocesses as the tool-under-test so each
case is hermetic + fast. No vendor CLI is exercised at this layer.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.errors import ToolError
from stm32_substrate.subprocess_runner import ToolRunResult, run_tool


@pytest.fixture()
def ctx(tmp_path: Path) -> SubstrateContext:
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def python_bin() -> Path:
    return Path(sys.executable)


class TestSuccess:
    def test_zero_exit_returns_result(self, ctx: SubstrateContext, python_bin: Path) -> None:
        r = run_tool(
            python_bin,
            ["-c", "print('hello')"],
            ctx=ctx,
            timeout_s=5,
        )
        assert isinstance(r, ToolRunResult)
        assert r.exit_code == 0
        assert "hello" in r.stdout
        assert r.stderr == ""
        assert r.timed_out is False
        assert r.duration_s >= 0

    def test_stderr_captured(self, ctx: SubstrateContext, python_bin: Path) -> None:
        r = run_tool(
            python_bin,
            ["-c", "import sys; sys.stderr.write('err\\n')"],
            ctx=ctx,
            timeout_s=5,
        )
        assert "err" in r.stderr
        assert r.exit_code == 0

    def test_stdin_piped(self, ctx: SubstrateContext, python_bin: Path) -> None:
        r = run_tool(
            python_bin,
            ["-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            ctx=ctx,
            timeout_s=5,
            stdin="hello\n",
        )
        assert "HELLO" in r.stdout

    def test_cwd_respected(
        self, ctx: SubstrateContext, python_bin: Path, tmp_path: Path
    ) -> None:
        subdir = tmp_path / "workdir"
        subdir.mkdir()
        r = run_tool(
            python_bin,
            ["-c", "import os; print(os.getcwd())"],
            ctx=ctx,
            timeout_s=5,
            cwd=subdir,
        )
        assert str(subdir) in r.stdout


class TestNonZeroExit:
    def test_default_raises_tool_error(self, ctx: SubstrateContext, python_bin: Path) -> None:
        with pytest.raises(ToolError) as excinfo:
            run_tool(
                python_bin,
                ["-c", "import sys; sys.exit(7)"],
                ctx=ctx,
                timeout_s=5,
            )
        err = excinfo.value
        assert err.code == 7
        assert "exited with code 7" in err.message

    def test_raise_on_nonzero_false_returns_result(
        self, ctx: SubstrateContext, python_bin: Path
    ) -> None:
        r = run_tool(
            python_bin,
            ["-c", "import sys; sys.exit(3)"],
            ctx=ctx,
            timeout_s=5,
            raise_on_nonzero=False,
        )
        assert r.exit_code == 3
        assert r.timed_out is False


class TestTimeout:
    def test_raises_tool_error_with_timeout_code(
        self, ctx: SubstrateContext, python_bin: Path
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            run_tool(
                python_bin,
                ["-c", "import time; time.sleep(10)"],
                ctx=ctx,
                timeout_s=0.3,
            )
        err = excinfo.value
        assert err.code == "timeout"
        assert "timed out" in err.message


class TestLogPath:
    def test_log_path_captures_both_streams(
        self,
        ctx: SubstrateContext,
        python_bin: Path,
        tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "logs" / "run.log"
        r = run_tool(
            python_bin,
            [
                "-c",
                "import sys; print('out-line'); sys.stderr.write('err-line\\n')",
            ],
            ctx=ctx,
            timeout_s=5,
            log_path=log_path,
        )
        assert r.exit_code == 0
        assert log_path.exists()
        content = log_path.read_text()
        assert "argv:" in content
        assert "out-line" in content
        assert "err-line" in content
        assert "exit_code: 0" in content


class TestLogging:
    def test_argv_logged_at_info(
        self,
        ctx: SubstrateContext,
        python_bin: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="stm32_substrate.subprocess_runner"):
            run_tool(python_bin, ["-c", "pass"], ctx=ctx, timeout_s=5)
        msgs = [r.message for r in caplog.records]
        assert any("run_tool argv=" in m for m in msgs)
        assert any("run_tool exit code=0" in m for m in msgs)
