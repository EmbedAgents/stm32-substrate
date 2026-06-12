"""B6a tests — shared flash infra + erase family (F-001 / F-002)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.codes import CubeProgrammerErrorCode
from embedagents.stm32.cubeprogrammer.results import EraseConfirmation
from embedagents.stm32.errors import (
    CubeProgrammerError,
    ToolError,
    UserAbortedError,
)
from embedagents.stm32.subprocess_runner import ToolRunResult


ERRORS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "errors"


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _success(stdout: str = "") -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# Address validator (shared infra)
# ---------------------------------------------------------------------------


class TestValidateAddress:
    @pytest.mark.parametrize(
        "address",
        [
            "0x08000000",
            "0x0",
            "0x20000000",
            "0xFFFFFFFF",
            "0x90000000",
            "0xdeadbeef",
        ],
    )
    def test_valid(self, ctx: SubstrateContext, address: str) -> None:
        client = CubeProgrammer(ctx)
        assert client._validate_address(address) == address

    @pytest.mark.parametrize(
        "address",
        [
            "08000000",          # missing 0x prefix
            "0X08000000",        # uppercase X
            "0x080,000,000",     # commas
            "0x ",               # trailing whitespace
            "",                  # empty
            "0xZZZZ",            # non-hex
            "0x_1234",           # underscore
        ],
    )
    def test_invalid_raises_value_error(
        self, ctx: SubstrateContext, address: str
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client._validate_address(address)


# ---------------------------------------------------------------------------
# Flash timeout helper
# ---------------------------------------------------------------------------


class TestFlashTimeout:
    def test_uses_base_when_file_missing(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        # path.stat() raises; defaults to base.
        result = client._flash_timeout_s(Path("/nonexistent"))
        assert result == pytest.approx(120.0)

    def test_scales_with_file_size(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        big = tmp_path / "big.bin"
        big.write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2 MB
        result = client._flash_timeout_s(big)
        # base 120 + 2 MB * 10 s/MB = 140s
        assert result == pytest.approx(140.0, abs=0.1)


# ---------------------------------------------------------------------------
# erase_chip — F-001
# ---------------------------------------------------------------------------


class TestEraseChip:
    def test_invokes_dash_e_all(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.erase_chip(confirm_destructive=True)
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-e", "all"]
        assert isinstance(result, EraseConfirmation)
        assert result.erase_complete is True
        assert result.reset_issued is False
        assert result.duration_s >= 0

    def test_uses_atomic_timeout(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.erase_chip(confirm_destructive=True)
        assert mocked.call_args.kwargs["timeout_s"] == 30.0

    def test_rdp_protected_raises(self, ctx: SubstrateContext) -> None:
        runner_err = ToolError(
            message="failed",
            code=10,
            tool_output=(ERRORS / "flash-protected-rdp.txt").read_text(),
        )
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.erase_chip(confirm_destructive=True)
        assert excinfo.value.error_code == CubeProgrammerErrorCode.TARGET_CMD_ERR

    def test_propagates_default_probe_sn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_cli = tmp_path / "STM32_Programmer_CLI"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
        monkeypatch.setenv("STM32_PROGRAMMER_DEFAULT_SN", "066BFFTESTSN")
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(ctx2)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.erase_chip(confirm_destructive=True)
        argv = mocked.call_args[0][1]
        assert "sn=066BFFTESTSN" in argv


# ---------------------------------------------------------------------------
# erase_and_reset — F-002
# ---------------------------------------------------------------------------


class TestEraseAndReset:
    def test_invokes_dash_e_all_dash_rst(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.erase_and_reset(confirm_destructive=True)
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-e", "all", "-rst"]
        assert result.erase_complete is True
        assert result.reset_issued is True


# ---------------------------------------------------------------------------
# Destructive gate (HIL HARD RULE 1) — erase must require consent
# ---------------------------------------------------------------------------


class TestEraseDestructiveGate:
    def test_erase_chip_default_aborts_without_consent(
        self, ctx: SubstrateContext
    ) -> None:
        """Bare ``erase_chip()`` must NOT touch the CLI — it aborts loud."""
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            with pytest.raises(UserAbortedError) as excinfo:
                client.erase_chip()
        mocked.assert_not_called()
        assert excinfo.value.hint is not None
        assert "confirm_destructive" in excinfo.value.hint

    def test_erase_and_reset_default_aborts_without_consent(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            with pytest.raises(UserAbortedError):
                client.erase_and_reset()
        mocked.assert_not_called()

    def test_erase_chip_callable_consent_receives_targets(
        self, ctx: SubstrateContext
    ) -> None:
        """The callable form is invoked with the destructive target list."""
        seen: list[list[str]] = []

        def approve(targets: list[str]) -> bool:
            seen.append(targets)
            return True

        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.erase_chip(confirm_destructive=approve)
        mocked.assert_called_once()
        assert seen and "erase" in seen[0][0].lower()

    def test_erase_chip_callable_decline_aborts(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            with pytest.raises(UserAbortedError):
                client.erase_chip(confirm_destructive=lambda _t: False)
        mocked.assert_not_called()
