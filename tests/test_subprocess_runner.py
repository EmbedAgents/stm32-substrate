"""Unit tests for ``run_tool``.

Tests spawn short-lived python subprocesses as the tool-under-test so each
case is hermetic + fast. No vendor CLI is exercised at this layer.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.errors import ToolError
from embedagents.stm32.subprocess_runner import ToolRunResult, run_tool


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

    def test_timeout_kills_grandchildren(
        self, ctx: SubstrateContext, python_bin: Path, tmp_path: Path
    ) -> None:
        """IMP-16: the timeout kill must take the whole process tree —
        signalling only the direct child orphaned JVM grandchildren of
        the vendor bootstrap launchers."""
        import time as _time

        from embedagents.stm32.platform import process_alive

        pidfile = tmp_path / "grandchild.pid"
        # The pidfile lands via write-then-rename so the kill can never
        # expose a created-but-empty file; timeout_s must cover two CPython
        # startups + the rename on a cold CI runner before the kill fires.
        spawner = (
            "import os, subprocess, sys, pathlib\n"
            "p = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'])\n"
            f"tmp = pathlib.Path({str(pidfile)!r} + '.tmp')\n"
            "tmp.write_text(str(p.pid))\n"
            f"os.replace(tmp, pathlib.Path({str(pidfile)!r}))\n"
            "p.wait()\n"
        )
        with pytest.raises(ToolError):
            run_tool(python_bin, ["-c", spawner], ctx=ctx, timeout_s=3.0)

        assert pidfile.is_file(), (
            "spawner was killed before it recorded the grandchild pid — "
            "timeout_s does not cover process startup on this machine"
        )
        grandchild_pid = int(pidfile.read_text().strip())
        deadline = _time.monotonic() + 3.0
        while _time.monotonic() < deadline and process_alive(grandchild_pid):
            _time.sleep(0.05)
        assert not process_alive(grandchild_pid)


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
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.subprocess_runner"):
            run_tool(python_bin, ["-c", "pass"], ctx=ctx, timeout_s=5)
        msgs = [r.message for r in caplog.records]
        assert any("run_tool argv=" in m for m in msgs)
        assert any("run_tool exit code=0" in m for m in msgs)
