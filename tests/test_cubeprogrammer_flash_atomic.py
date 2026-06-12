"""B6b tests — atomic flash methods (flash_file / flash_bin /
flash_data / flash_signed)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.codes import CubeProgrammerErrorCode
from embedagents.stm32.cubeprogrammer.results import FlashConfirmation
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
def elf_file(tmp_path: Path) -> Path:
    p = tmp_path / "blink.elf"
    p.write_bytes(b"\x7fELF" + b"\x00" * 1020)  # 1024 bytes
    return p


@pytest.fixture()
def bin_file(tmp_path: Path) -> Path:
    p = tmp_path / "blink.bin"
    p.write_bytes(b"\xff" * 2048)
    return p


def _success() -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout="", stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# flash_file — F-003
# ---------------------------------------------------------------------------


class TestFlashFile:
    def test_with_explicit_address(self, ctx: SubstrateContext, elf_file: Path) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_file(elf_file, address="0x08000000")
        argv = mocked.call_args[0][1]
        assert argv == [
            "-c",
            "port=swd",
            "-d",
            str(elf_file),
            "0x08000000",
        ]
        assert isinstance(result, FlashConfirmation)
        assert result.bytes_written == 1024
        assert result.address == "0x08000000"
        assert result.signed is False
        assert result.duration_s >= 0

    def test_without_address(self, ctx: SubstrateContext, elf_file: Path) -> None:
        """For ELF / HEX the CLI infers the load address."""
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_file(elf_file)
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-d", str(elf_file)]
        assert result.address == ""  # CLI-default sentinel

    def test_invalid_address_raises_value_error(
        self, ctx: SubstrateContext, elf_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client.flash_file(elf_file, address="not-hex")

    def test_cli_error_surfaces_as_typed(
        self, ctx: SubstrateContext, elf_file: Path
    ) -> None:
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
                client.flash_file(elf_file, address="0x08000000")
        assert excinfo.value.error_code == CubeProgrammerErrorCode.TARGET_CMD_ERR


# ---------------------------------------------------------------------------
# flash_bin — F-004
# ---------------------------------------------------------------------------


class TestFlashBin:
    def test_happy_path(self, ctx: SubstrateContext, bin_file: Path) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_bin(bin_file, "0x08000000")
        argv = mocked.call_args[0][1]
        assert argv == [
            "-c",
            "port=swd",
            "-d",
            str(bin_file),
            "0x08000000",
        ]
        assert result.bytes_written == 2048
        assert result.address == "0x08000000"
        assert result.signed is False

    def test_non_bin_extension_rejected(
        self, ctx: SubstrateContext, elf_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match=".bin"):
            client.flash_bin(elf_file, "0x08000000")

    def test_invalid_address_rejected(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client.flash_bin(bin_file, "not-hex")

    def test_extension_check_case_insensitive(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        upper = tmp_path / "blob.BIN"
        upper.write_bytes(b"\x00" * 64)
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.flash_bin(upper, "0x08000000")
        assert result.bytes_written == 64


# ---------------------------------------------------------------------------
# flash_data — F-007
# ---------------------------------------------------------------------------


class TestFlashData:
    def test_accepts_any_extension(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """Unlike flash_bin, flash_data permits arbitrary file types
        (raw payload / SVD baseline / font blob / etc.)."""
        blob = tmp_path / "icons.dat"
        blob.write_bytes(b"\x00" * 512)
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_data(blob, "0x080F0000")
        argv = mocked.call_args[0][1]
        assert "-d" in argv
        assert str(blob) in argv
        assert "0x080F0000" in argv
        assert result.bytes_written == 512
        assert result.signed is False


# ---------------------------------------------------------------------------
# flash_signed — F-006
# ---------------------------------------------------------------------------


class TestFlashSigned:
    def test_sets_signed_true(self, ctx: SubstrateContext, bin_file: Path) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.flash_signed(bin_file, address="0x70000000")
        assert result.signed is True
        assert result.address == "0x70000000"

    def test_no_family_pre_check(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        """Per RES-018, substrate does NOT validate that the target is
        N6 / MP1 / MP2 before invoking the CLI. Non-supported families
        surface as vendor errors, not substrate pre-flight errors."""
        client = CubeProgrammer(ctx)
        # No connect() / banner check happens before flash_signed.
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.flash_signed(bin_file, address="0x70000000")
        # Exactly one CLI call: the flash itself. No banner probe.
        assert mocked.call_count == 1


# ---------------------------------------------------------------------------
# Timeout scaling
# ---------------------------------------------------------------------------


class TestFlashTimeoutScaling:
    def test_large_file_extends_timeout(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        big = tmp_path / "big.bin"
        big.write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2 MB
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.flash_bin(big, "0x08000000")
        # base 120 + 2 * 10 = 140s
        assert mocked.call_args.kwargs["timeout_s"] == pytest.approx(140.0, abs=0.1)
