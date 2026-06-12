"""B5b tests — ping_swd (D-006).

Pure boolean probe: success → True, any CubeProgrammerError → False with
reason captured. Does NOT escalate to the D-002 ladder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.codes import CubeProgrammerErrorCode
from embedagents.stm32.cubeprogrammer.results import BooleanResult
from embedagents.stm32.errors import ToolError
from embedagents.stm32.subprocess_runner import ToolRunResult


BANNERS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "banners"
ERRORS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "errors"


@pytest.fixture()
def ctx_with_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestPingSwdHappy:
    def test_responsive_target_returns_true(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        good = (BANNERS / "nucleo-l476rg-good.txt").read_text()
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=ToolRunResult(
                exit_code=0, stdout=good, stderr="", duration_s=0.05, timed_out=False
            ),
        ):
            result = client.ping_swd()
        assert isinstance(result, BooleanResult)
        assert result.value is True
        assert result.reason is None

    def test_uses_mode_normal(self, ctx_with_cli: SubstrateContext) -> None:
        good = (BANNERS / "nucleo-l476rg-good.txt").read_text()
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=ToolRunResult(
                exit_code=0, stdout=good, stderr="", duration_s=0.05, timed_out=False
            ),
        ) as mocked:
            client.ping_swd()
        argv = mocked.call_args[0][1]
        assert "mode=NORMAL" in argv


class TestPingSwdFailureModes:
    @pytest.mark.parametrize(
        "fixture,exit_code,expected_code",
        [
            ("target-dll-err.txt", 2, CubeProgrammerErrorCode.TARGET_DLL_ERR),
            ("target-no-device.txt", 4, CubeProgrammerErrorCode.TARGET_NO_DEVICE),
            ("target-firmware-old.txt", 6, CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD),
            ("target-held-reset.txt", 8, CubeProgrammerErrorCode.TARGET_HELD_UNDER_RESET),
        ],
    )
    def test_error_returns_false_with_reason(
        self,
        ctx_with_cli: SubstrateContext,
        fixture: str,
        exit_code: int,
        expected_code: CubeProgrammerErrorCode,
    ) -> None:
        runner_err = ToolError(
            message=f"STM32_Programmer_CLI exited with code {exit_code}",
            code=exit_code,
            tool_output=(ERRORS / fixture).read_text(),
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            result = client.ping_swd()
        # ping_swd returns False with reason — never raises.
        assert result.value is False
        assert result.reason is not None
        assert result.reason  # non-empty

    def test_no_escalation_to_diagnose(self, ctx_with_cli: SubstrateContext) -> None:
        """ping_swd does not call diagnose_micro on failure — single
        attempt by spec."""
        runner_err = ToolError(
            message="failed",
            code=4,
            tool_output=(ERRORS / "target-no-device.txt").read_text(),
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool", side_effect=runner_err
        ) as mocked:
            client.ping_swd()
        # Exactly one CLI call; no ladder.
        assert mocked.call_count == 1


class TestPingSwdLogging:
    def test_info_on_success(
        self,
        ctx_with_cli: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        good = (BANNERS / "nucleo-l476rg-good.txt").read_text()
        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool",
                return_value=ToolRunResult(
                    exit_code=0, stdout=good, stderr="", duration_s=0.05, timed_out=False
                ),
            ):
                client.ping_swd()
        msgs = [r.message for r in caplog.records]
        assert any("ping_swd: target responding" in m for m in msgs)

    def test_info_on_failure(
        self,
        ctx_with_cli: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        runner_err = ToolError(
            message="failed",
            code=4,
            tool_output=(ERRORS / "target-no-device.txt").read_text(),
        )
        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool", side_effect=runner_err
            ):
                client.ping_swd()
        msgs = [r.message for r in caplog.records]
        assert any("unresponsive" in m for m in msgs)
