"""STM32H7S78-DK hardware tests — board-agnostic cubeprogrammer paths.

Mirrors the L476RG hardware suite for the second supported single-core
target on the bench. The H7S78-DK uses the STM32H7S7L8H8 (Cortex-M7
@ 600 MHz) with on-die flash mapped at ``0x08000000`` plus an XSPI
external flash window mapped at ``0x70000000`` (used by the bundled
USB_Device/MSC_Standalone reference project).

Run with ``pytest -m hardware`` when the H7S78-DK is on the bench;
skipped cleanly otherwise. Probe-discovery test under
``@pytest.mark.smoke_with_probe`` already covers any-board enumeration.
"""

from __future__ import annotations

import pytest

from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.results import (
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    MemoryReadResult,
    ResetConfirmation,
)


# ---------------------------------------------------------------------------
# Discovery — H7S78-DK banner shape
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestDiscovery:
    def test_connect_returns_banner(self, h7s78_dk_ctx) -> None:
        banner = CubeProgrammer(h7s78_dk_ctx).connect()
        assert isinstance(banner, BannerResult)
        assert banner.board_name == "STM32H7S78-DK"
        # H7S7 family device-id is 0x485 (STM32H7Sx/7Rx). Substrate
        # captures whatever the CLI reports; just assert it parsed
        # something hex-shaped.
        assert banner.device_id is not None
        assert banner.device_id.startswith("0x")
        assert banner.device_cpu == "Cortex-M7"
        # H7S7L8H8 has 64 KB on-die flash (the bulk lives in XSPI).
        # Don't pin a specific size — vendor may report on-die only or
        # the XSPI-mapped total; assert it parsed.
        assert banner.flash_size_kb is None or banner.flash_size_kb > 0
        assert banner.voltage_v == pytest.approx(3.3, abs=0.25)
        assert banner.voltage_suspicious is False

    def test_ping_swd_returns_true(self, h7s78_dk_ctx) -> None:
        result = CubeProgrammer(h7s78_dk_ctx).ping_swd()
        assert isinstance(result, BooleanResult)
        assert result.value is True
        assert result.reason is None

    def test_cores_primary_is_cortex_m7(self, h7s78_dk_ctx) -> None:
        result = CubeProgrammer(h7s78_dk_ctx).cores()
        assert isinstance(result, CoresResult)
        assert result.primary_core == "Cortex-M7"
        # H7S7 is single-core (M7 only); no secondary M4.
        assert result.secondary_cores == []

    def test_board_name_helper(self, h7s78_dk_ctx) -> None:
        assert CubeProgrammer(h7s78_dk_ctx).board_name() == "STM32H7S78-DK"


# ---------------------------------------------------------------------------
# Atomic target control — halt / resume / reset
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAtomicTargetControl:
    def test_reset_returns_confirmation(self, h7s78_dk_ctx) -> None:
        result = CubeProgrammer(h7s78_dk_ctx).reset()
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is False

    def test_hard_reset_returns_confirmation(self, h7s78_dk_ctx) -> None:
        result = CubeProgrammer(h7s78_dk_ctx).reset(hard=True)
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is True

    def test_halt_then_resume_round_trip(self, h7s78_dk_ctx) -> None:
        client = CubeProgrammer(h7s78_dk_ctx)
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
    def test_read_64_bytes_from_flash_start(self, h7s78_dk_ctx) -> None:
        """Read 64 bytes from on-die flash @ 0x08000000. H7S78-DK ships
        with a boot ROM that places the FSBL there; even after a user
        reflash, address 0 (initial SP) is non-0xFF."""
        result = CubeProgrammer(h7s78_dk_ctx).read_memory("0x08000000", size=64)
        assert isinstance(result, MemoryReadResult)
        assert result.address == "0x08000000"
        assert result.size == 64
        assert result.bytes_read == 64

    def test_read_default_256_bytes(self, h7s78_dk_ctx) -> None:
        result = CubeProgrammer(h7s78_dk_ctx).read_memory("0x08000000")
        assert result.size == 256
        assert result.bytes_read == 256
