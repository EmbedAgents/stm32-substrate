"""NUCLEO-F401RE hardware tests — board-agnostic cubeprogrammer paths.

The F401RE pairs an STM32F401RE (Cortex-M4 @ 84 MHz, 512 KB flash,
96 KB SRAM, device_id 0x433 — the F401xD/xE variant per RM0368) with
an onboard ST-Link/V2-1. Some bench units ship with older ST-Link
firmware (e.g. V2J28M17) that emits an EMPTY ``Board Name`` in
``STM32_Programmer_CLI -l`` output — so the ``attached_boards``
fixture can't see the board by name. The ``f401re_ctx`` fixture works
around this by latching the lone probe's SN + verifying
device_id == 0x433 via connect-banner.

Run with ``pytest -m hardware`` when the F401RE is attached; skipped
cleanly otherwise.
"""

from __future__ import annotations

import pytest

from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.results import (
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    MemoryReadResult,
    ResetConfirmation,
)


# ---------------------------------------------------------------------------
# Discovery — F401RE banner shape
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestDiscovery:
    def test_connect_returns_banner(self, f401re_ctx) -> None:
        banner = CubeProgrammer(f401re_ctx).connect()
        assert isinstance(banner, BannerResult)
        assert banner.device_id == "0x433", (
            f"F401xD/xE family id (NUCLEO-F401RE) is 0x433; got {banner.device_id}"
        )
        assert banner.device_cpu == "Cortex-M4"
        # F401RE has 512 KB on-die flash. Substrate parses NVM-size out
        # of the banner; assert the parsed kB value when present.
        if banner.flash_size_kb is not None:
            assert banner.flash_size_kb == 512
        assert banner.voltage_v == pytest.approx(3.3, abs=0.25)
        assert banner.voltage_suspicious is False

    def test_ping_swd_returns_true(self, f401re_ctx) -> None:
        result = CubeProgrammer(f401re_ctx).ping_swd()
        assert isinstance(result, BooleanResult)
        assert result.value is True
        assert result.reason is None

    def test_cores_primary_is_cortex_m4(self, f401re_ctx) -> None:
        result = CubeProgrammer(f401re_ctx).cores()
        assert isinstance(result, CoresResult)
        assert result.primary_core == "Cortex-M4"
        # F401 is single-core; no secondary core.
        assert result.secondary_cores == []


# ---------------------------------------------------------------------------
# Atomic target control — halt / resume / reset
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAtomicTargetControl:
    def test_reset_returns_confirmation(self, f401re_ctx) -> None:
        result = CubeProgrammer(f401re_ctx).reset()
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is False

    def test_hard_reset_returns_confirmation(self, f401re_ctx) -> None:
        result = CubeProgrammer(f401re_ctx).reset(hard=True)
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is True

    def test_halt_then_resume_round_trip(self, f401re_ctx) -> None:
        client = CubeProgrammer(f401re_ctx)
        halt_result = client.halt()
        try:
            assert isinstance(halt_result, Confirmation)
            assert halt_result.data.get("halted") is True
        finally:
            resume_result = client.resume()
            assert isinstance(resume_result, Confirmation)
            assert resume_result.data.get("running") is True


# ---------------------------------------------------------------------------
# Memory reads
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestMemoryReads:
    def test_read_64_bytes_from_flash_start(self, f401re_ctx) -> None:
        """Flash @ 0x08000000 carries at minimum a reset-vector + initial-SP
        pair (8 bytes) for any board that boots — F401RE ships with the
        ST-LINK mass-storage demo. Substrate's suspicious_unmapped flag
        only fires on all-0xFF (erased) regions."""
        result = CubeProgrammer(f401re_ctx).read_memory("0x08000000", size=64)
        assert isinstance(result, MemoryReadResult)
        assert result.address == "0x08000000"
        assert result.size == 64
        assert result.bytes_read == 64

    def test_read_default_256_bytes(self, f401re_ctx) -> None:
        result = CubeProgrammer(f401re_ctx).read_memory("0x08000000")
        assert result.size == 256
        assert result.bytes_read == 256
