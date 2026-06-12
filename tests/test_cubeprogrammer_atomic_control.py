"""B7 tests — atomic target control (reset / halt / resume).

Two routing paths per method: direct CLI (no active debug session) and
gdb-mediated (active session via ctx.session_state.active_debug_session).
The gdb path uses a duck-typed mock since DebugSession lives in C4."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.codes import CubeProgrammerErrorCode
from embedagents.stm32.cubeprogrammer.results import (
    Confirmation,
    ResetConfirmation,
)
from embedagents.stm32.errors import CubeProgrammerError, ToolError
from embedagents.stm32.subprocess_runner import ToolRunResult


ERRORS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "errors"


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def ctx_with_debug_session(
    ctx: SubstrateContext,
) -> tuple[SubstrateContext, MagicMock]:
    """Install a duck-typed mock DebugSession on the context."""
    mock_session = MagicMock()
    mock_session.send_monitor = MagicMock()
    ctx.session_state.active_debug_session = mock_session
    return ctx, mock_session


def _success() -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout="", stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# reset — F-016
# ---------------------------------------------------------------------------


class TestResetCliPath:
    def test_soft_reset(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.reset()
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-rst"]
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.via_gdb is False
        assert result.hard is False

    def test_hard_reset(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.reset(hard=True)
        argv = mocked.call_args[0][1]
        assert "-hardRst" in argv
        assert "-rst" not in argv
        assert result.hard is True
        assert result.via_gdb is False

    def test_uses_atomic_timeout(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.reset()
        assert mocked.call_args.kwargs["timeout_s"] == 30.0

    def test_cli_error_surfaces(self, ctx: SubstrateContext) -> None:
        runner_err = ToolError(
            message="failed",
            code=7,
            tool_output="Error: target reset failed",
        )
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError):
                client.reset()


class TestResetGdbPath:
    def test_routes_through_send_monitor(
        self, ctx_with_debug_session: tuple[SubstrateContext, MagicMock]
    ) -> None:
        ctx, session = ctx_with_debug_session
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool"
        ) as mocked_run_tool:
            result = client.reset()
        # CLI never invoked when session is active.
        assert mocked_run_tool.call_count == 0
        session.send_monitor.assert_called_once_with("reset")
        assert result.via_gdb is True
        assert result.reset_issued is True

    def test_hard_flag_preserved_in_result_only(
        self, ctx_with_debug_session: tuple[SubstrateContext, MagicMock]
    ) -> None:
        """gdb's ``monitor reset`` doesn't distinguish hard/soft; substrate
        records ``hard`` on the result for caller inspection but the gdb
        command is always ``reset``."""
        ctx, session = ctx_with_debug_session
        client = CubeProgrammer(ctx)
        result = client.reset(hard=True)
        session.send_monitor.assert_called_once_with("reset")
        assert result.hard is True
        assert result.via_gdb is True


# ---------------------------------------------------------------------------
# halt — F-017
# ---------------------------------------------------------------------------


class TestHaltCliPath:
    def test_argv(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.halt()
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-halt"]
        assert isinstance(result, Confirmation)
        assert result.operation == "halt"
        assert result.data["halted"] is True
        assert result.data["via_gdb"] is False
        assert result.data["prior_state"] == "unknown"

    def test_cli_error_surfaces(self, ctx: SubstrateContext) -> None:
        runner_err = ToolError(
            message="failed",
            code=11,
            tool_output="Error: target halt failed",
        )
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError):
                client.halt()


class TestHaltGdbPath:
    def test_routes_through_session_halt(
        self, ctx_with_debug_session: tuple[SubstrateContext, MagicMock]
    ) -> None:
        """RES-041: gdb-side halt is the MI-level ``session.halt()``
        (-exec-interrupt) — keeps gdb's target-state machine in sync."""
        ctx, session = ctx_with_debug_session
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool"
        ) as mocked_run_tool:
            result = client.halt()
        assert mocked_run_tool.call_count == 0
        session.halt.assert_called_once_with()
        assert result.data["via_gdb"] is True
        assert result.data["halted"] is True


# ---------------------------------------------------------------------------
# resume — F-018
# ---------------------------------------------------------------------------


class TestResumeCliPath:
    def test_argv_uses_dash_run(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.resume()
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-run"]
        assert isinstance(result, Confirmation)
        assert result.operation == "resume"
        assert result.data["running"] is True
        assert result.data["via_gdb"] is False

    def test_uses_atomic_timeout(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.resume()
        assert mocked.call_args.kwargs["timeout_s"] == 30.0


class TestResumeGdbPath:
    def test_routes_through_session_resume(
        self, ctx_with_debug_session: tuple[SubstrateContext, MagicMock]
    ) -> None:
        """RES-041: gdb-side resume is MI ``session.resume()``
        (-exec-continue) — ST-LINK gdbserver has no resume-flavored Rcmd
        (``monitor continue``/``go``/``resume`` all ^error on v7.13.0)."""
        ctx, session = ctx_with_debug_session
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool"
        ) as mocked_run_tool:
            result = client.resume()
        assert mocked_run_tool.call_count == 0
        session.resume.assert_called_once_with()
        assert result.data["via_gdb"] is True


# ---------------------------------------------------------------------------
# Routing precedence
# ---------------------------------------------------------------------------


class TestRoutingPrecedence:
    def test_active_session_always_wins(
        self, ctx_with_debug_session: tuple[SubstrateContext, MagicMock]
    ) -> None:
        """Even when the CLI is configured + would work, an active debug
        session takes precedence to avoid SWD-probe contention."""
        ctx, session = ctx_with_debug_session
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool"
        ) as mocked_run_tool:
            client.reset()
            client.halt()
            client.resume()
        assert mocked_run_tool.call_count == 0
        # reset → send_monitor("reset"); halt/resume → MI-level
        # session.halt()/session.resume() (RES-041).
        session.send_monitor.assert_called_once_with("reset")
        session.halt.assert_called_once_with()
        session.resume.assert_called_once_with()

    def test_clearing_session_returns_to_cli_path(
        self, ctx_with_debug_session: tuple[SubstrateContext, MagicMock]
    ) -> None:
        ctx, session = ctx_with_debug_session
        client = CubeProgrammer(ctx)

        # First call: gdb path.
        client.halt()
        assert session.halt.call_count == 1

        # Clear the session and try again — CLI path should fire.
        ctx.session_state.active_debug_session = None
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked_run_tool:
            client.halt()
        assert mocked_run_tool.call_count == 1
