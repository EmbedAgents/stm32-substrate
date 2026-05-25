"""B6d tests — compound flash methods (flash_pair / flash_signed_pair /
flash_bin_no_address / flash_to_bank / download_image)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.codes import CubeProgrammerErrorCode
from stm32_substrate.cubeprogrammer.results import FlashConfirmation, PairFlashResult
from stm32_substrate.errors import (
    CubeProgrammerError,
    ToolError,
    UserAbortedError,
)
from stm32_substrate.subprocess_runner import ToolRunResult


ERRORS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "errors"


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def bin_file(tmp_path: Path) -> Path:
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00" * 1024)
    return p


@pytest.fixture()
def elf_file(tmp_path: Path) -> Path:
    p = tmp_path / "blink.elf"
    p.write_bytes(b"\x7fELF" + b"\x00" * 1020)
    return p


@pytest.fixture()
def hex_file(tmp_path: Path) -> Path:
    p = tmp_path / "blink.hex"
    p.write_bytes(b":020000040800F2\n:00000001FF\n")
    return p


def _success() -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout="", stderr="", duration_s=0.05, timed_out=False
    )


def _rdp_error() -> ToolError:
    return ToolError(
        message="failed",
        code=10,
        tool_output=(ERRORS / "flash-protected-rdp.txt").read_text(),
    )


# ---------------------------------------------------------------------------
# flash_to_bank — F-011
# ---------------------------------------------------------------------------


class TestFlashToBank:
    def test_bank_recorded_in_result(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.flash_to_bank(bin_file, bank=2, address="0x08100000")
        assert isinstance(result, FlashConfirmation)
        assert result.bank == 2
        assert result.address == "0x08100000"

    def test_invalid_bank_rejected(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        for bad_bank in (0, 3, -1):
            with pytest.raises(ValueError, match="bank"):
                client.flash_to_bank(bin_file, bank=bad_bank, address="0x08000000")

    def test_invalid_address_rejected(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client.flash_to_bank(bin_file, bank=1, address="not-hex")


# ---------------------------------------------------------------------------
# flash_bin_no_address — F-005
# ---------------------------------------------------------------------------


class TestFlashBinNoAddress:
    def test_infers_universal_flash_start(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_bin_no_address(bin_file)
        argv = mocked.call_args[0][1]
        assert "0x08000000" in argv
        assert result.address == "0x08000000"
        assert result.address_inferred is True
        assert result.user_confirmed is False

    def test_on_confirm_accepts(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        captured: list[str] = []

        def cb(addr: str) -> bool:
            captured.append(addr)
            return True

        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.flash_bin_no_address(bin_file, on_confirm=cb)
        assert captured == ["0x08000000"]
        assert result.user_confirmed is True

    def test_on_confirm_rejects_raises_user_aborted(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            with pytest.raises(UserAbortedError):
                client.flash_bin_no_address(bin_file, on_confirm=lambda _: False)
        # No CLI invocation when user aborts.
        assert mocked.call_count == 0


# ---------------------------------------------------------------------------
# flash_pair — F-008
# ---------------------------------------------------------------------------


class TestFlashPair:
    def test_happy_path_both_succeeded(
        self,
        ctx: SubstrateContext,
        bin_file: Path,
        tmp_path: Path,
    ) -> None:
        app_file = tmp_path / "app.bin"
        app_file.write_bytes(b"\x00" * 512)
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_pair(
                bin_file,
                app_file,
                bootloader_address="0x08000000",
                application_address="0x08008000",
            )
        # Two CLI invocations — one per leg.
        assert mocked.call_count == 2
        assert isinstance(result, PairFlashResult)
        assert result.bootloader is not None
        assert result.application is not None
        assert result.both_succeeded is True
        assert result.bootloader.address == "0x08000000"
        assert result.application.address == "0x08008000"

    def test_first_leg_failure_reraises(
        self,
        ctx: SubstrateContext,
        bin_file: Path,
        tmp_path: Path,
    ) -> None:
        """HIL: nothing was written. Caller sees the typed error."""
        app_file = tmp_path / "app.bin"
        app_file.write_bytes(b"\x00" * 512)
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=_rdp_error(),
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.flash_pair(
                    bin_file,
                    app_file,
                    bootloader_address="0x08000000",
                    application_address="0x08008000",
                )
        assert excinfo.value.error_code == CubeProgrammerErrorCode.TARGET_CMD_ERR

    def test_second_leg_failure_returns_partial(
        self,
        ctx: SubstrateContext,
        bin_file: Path,
        tmp_path: Path,
    ) -> None:
        """Partial state on the device — capture, don't raise. Caller
        decides whether to recover (re-flash, erase, etc.)."""
        app_file = tmp_path / "app.bin"
        app_file.write_bytes(b"\x00" * 512)
        client = CubeProgrammer(ctx)

        call_count = {"n": 0}

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _success()
            raise _rdp_error()

        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            result = client.flash_pair(
                bin_file,
                app_file,
                bootloader_address="0x08000000",
                application_address="0x08008000",
            )
        assert result.bootloader is not None
        assert result.application is None
        assert result.both_succeeded is False


# ---------------------------------------------------------------------------
# flash_signed_pair — F-009
# ---------------------------------------------------------------------------


class TestFlashSignedPair:
    def test_sets_signed_true_on_both_legs(
        self,
        ctx: SubstrateContext,
        bin_file: Path,
        tmp_path: Path,
    ) -> None:
        app_file = tmp_path / "app.bin"
        app_file.write_bytes(b"\x00" * 512)
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.flash_signed_pair(
                bin_file,
                app_file,
                bootloader_address="0x70000000",
                application_address="0x70010000",
            )
        assert result.both_succeeded is True
        assert result.bootloader is not None and result.bootloader.signed is True
        assert result.application is not None and result.application.signed is True

    def test_sign_unsigned_true_raises_not_implemented(
        self,
        ctx: SubstrateContext,
        bin_file: Path,
        tmp_path: Path,
    ) -> None:
        """sign_unsigned=True wires to the signing module (C2). Until
        C2 lands, substrate raises NotImplementedError loudly."""
        app_file = tmp_path / "app.bin"
        app_file.write_bytes(b"\x00" * 512)
        client = CubeProgrammer(ctx)
        with pytest.raises(NotImplementedError, match="signing module"):
            client.flash_signed_pair(
                bin_file,
                app_file,
                bootloader_address="0x70000000",
                application_address="0x70010000",
                sign_unsigned=True,
            )


# ---------------------------------------------------------------------------
# download_image — CP-001 router
# ---------------------------------------------------------------------------


class TestDownloadImage:
    def test_elf_routes_to_flash_file(
        self, ctx: SubstrateContext, elf_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.download_image(elf_file)
        assert result.route_used == "flash_file"

    def test_hex_routes_to_flash_file(
        self, ctx: SubstrateContext, hex_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.download_image(hex_file)
        assert result.route_used == "flash_file"

    def test_bin_with_address_routes_to_flash_bin(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.download_image(bin_file, address="0x08000000")
        assert result.route_used == "flash_bin"
        assert result.address == "0x08000000"

    def test_bin_without_address_routes_to_no_address(
        self, ctx: SubstrateContext, bin_file: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.download_image(bin_file)
        assert result.route_used == "flash_bin_no_address"
        assert result.address == "0x08000000"
        assert result.address_inferred is True

    def test_unsupported_extension_rejected(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        weird = tmp_path / "blob.dat"
        weird.write_bytes(b"\x00" * 64)
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="cannot infer route"):
            client.download_image(weird, address="0x08000000")

    def test_extension_case_insensitive(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        upper = tmp_path / "image.ELF"
        upper.write_bytes(b"\x7fELF" + b"\x00" * 100)
        client = CubeProgrammer(ctx)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            result = client.download_image(upper)
        assert result.route_used == "flash_file"
