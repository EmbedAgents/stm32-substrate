"""STM32H747I-DISCO hardware tests — board-agnostic cubeprogrammer paths.

The H747I-DISCO pairs an STM32H747XI (dual-core: Cortex-M7 @ 480 MHz +
Cortex-M4 @ 240 MHz, 2 MB total flash, device_id 0x450) with an
onboard ST-Link/V3 (FW V3Jx). Substrate's cubeprogrammer module
defaults to connecting via the CM7 boot core; the CM4 is visible as a
secondary core in ``cores()`` output.

This file covers the single-core attach surface (connect, ping_swd,
cores, reset, halt/resume, memory reads). The dual-core attach +
nested build coverage for the bundled USB_Host/MSC_Standalone and
FPU_Fractal projects lives in cubeide / debug test modules and lights
up as those fixtures (F-PROJ-DISCO-H747XI-DUAL-CORE and
F-PROJ-DISCO-H747XI-FPU) get hardware-tested.

Run with ``pytest -m hardware`` when the H747I-DISCO is on the bench.
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
# Discovery — H747I-DISCO banner shape
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestDiscovery:
    def test_connect_returns_banner(self, h747i_disco_ctx) -> None:
        banner = CubeProgrammer(h747i_disco_ctx).connect()
        assert isinstance(banner, BannerResult)
        assert banner.board_name == "DISCO-H747XI"
        # H743/H747/H753 family device-id is 0x450. Assert hex-shaped;
        # the exact value depends on which core the CLI defaulted to.
        assert banner.device_id is not None
        assert banner.device_id.startswith("0x")
        # CubeProgrammer 2.22 reports the dual-core CPU as the literal
        # slashed shorthand "Cortex-M7/M4" — substrate captures verbatim
        # per ADR-004 (capture-don't-interpret). Assert the M7 is named
        # without pinning the exact string format.
        assert banner.device_cpu is not None
        assert "M7" in banner.device_cpu, (
            f"expected M7 in CPU; got {banner.device_cpu!r}"
        )
        # H747XI total flash is 2 MB (1 MB per core bank). Vendor may
        # report on-die total or per-bank; assert parsed when present.
        assert banner.flash_size_kb is None or banner.flash_size_kb > 0
        assert banner.voltage_v == pytest.approx(3.3, abs=0.25)
        assert banner.voltage_suspicious is False

    def test_ping_swd_returns_true(self, h747i_disco_ctx) -> None:
        result = CubeProgrammer(h747i_disco_ctx).ping_swd()
        assert isinstance(result, BooleanResult)
        assert result.value is True
        assert result.reason is None

    def test_cores_primary_is_cortex_m7(self, h747i_disco_ctx) -> None:
        """Substrate's ``cores()`` derives from the banner CPU field.
        CubeProgrammer 2.22 emits the slashed shorthand
        ``Cortex-M7/M4`` for the H747; substrate captures verbatim
        (ADR-004), so ``primary_core`` is the full string. M4 doesn't
        appear in ``secondary_cores`` — splitting the slashed form is
        a Claude-side concern, not substrate's."""
        result = CubeProgrammer(h747i_disco_ctx).cores()
        assert isinstance(result, CoresResult)
        assert result.primary_core is not None
        assert "M7" in result.primary_core
        # H747 is dual-core — assert the M4 is named in the captured
        # banner (in whichever form the CLI uses).
        assert "M4" in result.primary_core or "Cortex-M4" in result.secondary_cores, (
            f"expected M4 named somewhere on H747; primary={result.primary_core!r} "
            f"secondaries={result.secondary_cores!r}"
        )

    def test_board_name_helper(self, h747i_disco_ctx) -> None:
        assert CubeProgrammer(h747i_disco_ctx).board_name() == "DISCO-H747XI"


# ---------------------------------------------------------------------------
# Atomic target control — halt / resume / reset
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAtomicTargetControl:
    def test_reset_returns_confirmation(self, h747i_disco_ctx) -> None:
        result = CubeProgrammer(h747i_disco_ctx).reset()
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is False

    def test_hard_reset_returns_confirmation(self, h747i_disco_ctx) -> None:
        result = CubeProgrammer(h747i_disco_ctx).reset(hard=True)
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is True

    def test_halt_then_resume_round_trip(self, h747i_disco_ctx) -> None:
        client = CubeProgrammer(h747i_disco_ctx)
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
    def test_read_64_bytes_from_flash_start(self, h747i_disco_ctx) -> None:
        """Read 64 bytes from CM7 flash bank @ 0x08000000. H747I-DISCO
        ships with a demo firmware (Discovery animation / partition
        chooser) so flash @ 0 has real content, not 0xFF."""
        result = CubeProgrammer(h747i_disco_ctx).read_memory("0x08000000", size=64)
        assert isinstance(result, MemoryReadResult)
        assert result.address == "0x08000000"
        assert result.size == 64
        assert result.bytes_read == 64

    def test_read_default_256_bytes(self, h747i_disco_ctx) -> None:
        result = CubeProgrammer(h747i_disco_ctx).read_memory("0x08000000")
        assert result.size == 256
        assert result.bytes_read == 256
