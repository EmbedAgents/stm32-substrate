"""N6 signing + external-loader flash hardware tests.

These run against an attached STM32N6570-DK. They exercise the cross-
module substrate chain that the L476RG suite can't cover: SigningTool
(STM32_SigningTool_CLI) producing a hv=2.3 trusted-binary for the N6
family, then CubeProgrammer.flash_external (-c port=swd -el <stldr>
-d <bin> <address>) writing it to the DK's MX66UW1G45G OctoSPI flash
at 0x70100000.

What's covered:
  - SigningTool.sign_binary on STM32N6 (hv=2.3, ssbl, -nk, auto-align).
    The signed binary has a 1024-byte header prepended (per UM2543);
    the test asserts bytes_out == bytes_in + 1024.
  - CubeProgrammer.flash_external with the MX66UW1G45G_STM32N6570-DK
    external loader, against a substrate-signed payload.
  - End-to-end: sign + flash in the same test, validating that the
    signed payload is accepted by the device flash chain.

What's NOT covered (out of scope for the substrate slice):
  - The user-provides AI model pipeline (stedgeai output, ATON lib
    version skew, etc.). The test signs a tiny synthetic .bin so it
    doesn't depend on any user-provides AI files.
  - Boot-from-OctoSPI verification (would require setting BOOT pins +
    power-cycling the board; out of substrate's HIL scope).

Excluded from the default ``pytest`` run; invoke with
``pytest -m hardware``.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.results import FlashConfirmation, PairFlashResult
from embedagents.stm32.signing import SigningTool
from embedagents.stm32.signing.results import SigningResult


_N6_LOADER_NAME = "MX66UW1G45G_STM32N6570-DK.stldr"
_SSBL_LOAD_ADDRESS = "0x70100000"

# Signed-pair (F-009) targets internal AXISRAM, not OctoSPI: flash_signed_pair
# routes each leg through flash_signed -> `-d <bin> <addr>` (plain SWD download,
# NO external loader), so it can only write SWD-addressable memory. The N6 is
# flashless (banner flash_size_kb == 0), so AXISRAM (0x34000000) is the only
# directly-writable region. The two legs sit 64 KB apart so the boot image
# (synthetic 2 KB + 1 KB signing header = 3 KB) can't overlap the app leg.
_PAIR_BOOT_ADDRESS = "0x34000000"
_PAIR_APP_ADDRESS = "0x34010000"


@pytest.fixture
def n6_loader_path(n6dk_ctx) -> Path:
    """Locate the MX66UW1G45G external loader bundled with CubeProgrammer.

    Resolves <cube_programmer_cli>.parent / ExternalLoader / <name>.
    Skips when the loader file is absent (e.g. older CubeProgrammer
    install that predates N6 support).
    """
    cli = n6dk_ctx.tools.cube_programmer_cli
    assert cli is not None, "n6dk_ctx requires cube_programmer_cli resolved"
    loader = cli.parent / "ExternalLoader" / _N6_LOADER_NAME
    if not loader.is_file():
        pytest.skip(
            f"N6 external loader not found at {loader}; "
            "upgrade STM32CubeProgrammer or install the matching ExternalLoader package"
        )
    return loader


@pytest.fixture
def synthetic_bin(tmp_path: Path) -> Path:
    """Generate a tiny random .bin payload to sign+flash.

    Substrate signing is content-agnostic — any aligned-size .bin works.
    Using random bytes ensures we're not accidentally signing a
    persisted fixture (which would risk overwrite-refusal on rerun).
    """
    payload = secrets.token_bytes(2048)
    path = tmp_path / "synthetic.bin"
    path.write_bytes(payload)
    return path


@pytest.mark.hardware
class TestN6SigningFlash:
    """Substrate end-to-end: sign a tiny synthetic .bin for N6 + flash it
    to OctoSPI via the external loader. Validates the cross-module
    SigningTool → CubeProgrammer.flash_external chain on real hardware.
    """

    def test_sign_synthetic_bin_for_n6(
        self, n6dk_ctx, synthetic_bin: Path, tmp_path: Path
    ) -> None:
        """SigningTool happy path on STM32N6: hv=2.3 + ssbl + -nk +
        auto-align. Asserts the SigningResult contract — 1024-byte
        header prepended; output file exists; align_applied=True; no_auth_flag
        carries through; device_family informational."""
        output = tmp_path / "synthetic_sign.bin"
        result = SigningTool(n6dk_ctx).sign_binary(
            input_path=synthetic_bin,
            load_address=_SSBL_LOAD_ADDRESS,
            entry_point=_SSBL_LOAD_ADDRESS,
            image_type="ssbl",
            header_version="2.3",
            no_key=True,
            device_family="STM32N6",
            output_path=output,
        )
        assert isinstance(result, SigningResult)
        assert result.output_path == output
        assert result.output_path.is_file()
        assert result.bytes_in == synthetic_bin.stat().st_size
        assert result.bytes_out == result.bytes_in + 1024, (
            f"expected 1024-byte signing header; got bytes_in={result.bytes_in} "
            f"bytes_out={result.bytes_out}"
        )
        assert result.image_type == "ssbl"
        assert result.header_version == "2.3"
        assert result.no_auth_flag is True
        assert result.align_applied is True
        assert result.device_family == "STM32N6"
        assert result.log_path.is_file()

    def test_flash_signed_to_octospi(
        self,
        n6dk_ctx,
        synthetic_bin: Path,
        n6_loader_path: Path,
        tmp_path: Path,
    ) -> None:
        """Substrate signs a synthetic .bin then flashes the signed
        payload to OctoSPI (0x70100000) via external loader. Asserts
        the FlashConfirmation contract — bytes_written matches the
        signed file size; loader_used names the .stldr basename."""
        signed = tmp_path / "to_flash_sign.bin"
        SigningTool(n6dk_ctx).sign_binary(
            input_path=synthetic_bin,
            load_address=_SSBL_LOAD_ADDRESS,
            entry_point=_SSBL_LOAD_ADDRESS,
            image_type="ssbl",
            header_version="2.3",
            no_key=True,
            device_family="STM32N6",
            output_path=signed,
        )
        confirm = CubeProgrammer(n6dk_ctx).flash_external(
            signed,
            _SSBL_LOAD_ADDRESS,
            loader_path=n6_loader_path,
        )
        assert isinstance(confirm, FlashConfirmation)
        assert confirm.loader_used == _N6_LOADER_NAME
        assert confirm.address == _SSBL_LOAD_ADDRESS
        assert confirm.bytes_written == signed.stat().st_size
        assert confirm.duration_s > 0


@pytest.fixture
def signed_pair(n6dk_ctx, tmp_path: Path) -> tuple[Path, Path]:
    """Two substrate-signed synthetic .bins for the boot+app pair.

    Each leg is signed at its own AXISRAM target address (informational
    in the header — the positional ``-d <addr>`` is what places it).
    Distinct random payloads so the two legs are byte-distinguishable.
    """
    signer = SigningTool(n6dk_ctx)
    boot_raw = tmp_path / "boot.bin"
    boot_raw.write_bytes(secrets.token_bytes(2048))
    boot_signed = tmp_path / "boot_sign.bin"
    signer.sign_binary(
        input_path=boot_raw,
        load_address=_PAIR_BOOT_ADDRESS,
        entry_point=_PAIR_BOOT_ADDRESS,
        image_type="ssbl",
        header_version="2.3",
        no_key=True,
        device_family="STM32N6",
        output_path=boot_signed,
    )
    app_raw = tmp_path / "app.bin"
    app_raw.write_bytes(secrets.token_bytes(2048))
    app_signed = tmp_path / "app_sign.bin"
    signer.sign_binary(
        input_path=app_raw,
        load_address=_PAIR_APP_ADDRESS,
        entry_point=_PAIR_APP_ADDRESS,
        image_type="ssbl",
        header_version="2.3",
        no_key=True,
        device_family="STM32N6",
        output_path=app_signed,
    )
    return boot_signed, app_signed


@pytest.mark.hardware
class TestN6SignedPair:
    """F-009 — ``flash_signed_pair`` partial-completion semantics on real
    N6 silicon. The signed analog of F-008 (``flash_pair``): two sequential
    ``flash_signed`` legs sharing the PairFlashResult contract. Targets
    AXISRAM (not OctoSPI) because flash_signed_pair uses the plain ``-d``
    download path with no external loader — see the module-level address
    note. Proves the two-leg orchestration + signed-leg confirmation, not
    a bootable boot/app image (boot-from-OctoSPI is out of HIL scope, per
    the module docstring)."""

    def test_flash_signed_pair_succeeds_with_two_signed_bins(
        self, n6dk_ctx, signed_pair: tuple[Path, Path]
    ) -> None:
        """Both signed legs flash to distinct AXISRAM addresses.

        Asserts PairFlashResult.both_succeeded=True; each leg is a
        FlashConfirmation with signed=True, address matching its target,
        and bytes_written == the signed file's on-disk size (substrate
        uses path.stat().st_size, not parsed CLI output)."""
        boot_signed, app_signed = signed_pair
        result = CubeProgrammer(n6dk_ctx).flash_signed_pair(
            boot_signed,
            app_signed,
            bootloader_address=_PAIR_BOOT_ADDRESS,
            application_address=_PAIR_APP_ADDRESS,
        )
        assert isinstance(result, PairFlashResult)
        assert result.both_succeeded is True
        assert isinstance(result.bootloader, FlashConfirmation)
        assert result.bootloader.signed is True
        assert result.bootloader.address == _PAIR_BOOT_ADDRESS
        assert result.bootloader.bytes_written == boot_signed.stat().st_size
        assert isinstance(result.application, FlashConfirmation)
        assert result.application.signed is True
        assert result.application.address == _PAIR_APP_ADDRESS
        assert result.application.bytes_written == app_signed.stat().st_size

    def test_flash_signed_pair_captures_second_leg_failure(
        self, n6dk_ctx, signed_pair: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Second leg points at a nonexistent signed .bin → partial
        completion captured (no raise). Boot leg flashes successfully;
        app leg's CLI invocation fails (file-not-found) → substrate's
        flash_signed_pair catches the CubeProgrammerError and returns
        PairFlashResult(bootloader=<populated>, application=None,
        both_succeeded=False). Mirrors F-008's second-leg-failure test."""
        boot_signed, _ = signed_pair
        missing = tmp_path / "no-such-signed.bin"
        assert not missing.exists()
        result = CubeProgrammer(n6dk_ctx).flash_signed_pair(
            boot_signed,
            missing,
            bootloader_address=_PAIR_BOOT_ADDRESS,
            application_address=_PAIR_APP_ADDRESS,
        )
        assert isinstance(result, PairFlashResult)
        assert result.both_succeeded is False
        assert isinstance(result.bootloader, FlashConfirmation)
        assert result.bootloader.signed is True
        assert result.bootloader.bytes_written == boot_signed.stat().st_size
        assert result.application is None
