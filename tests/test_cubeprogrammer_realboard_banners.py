"""Real-board banner regression — every connect banner captured off the
bench (via ``tools/capture-banner.sh`` into ``banners/realboards/``) must
parse cleanly with sane fields.

These fixtures are the verbatim stdout of ``STM32_Programmer_CLI -c
port=swd`` on a physically-attached board, re-captured across the bench's
expanded device set. They guard ``parse_banner`` against real-world banner
variation (family-wildcard device names, NVM-vs-Flash size lines, locale
decimals, V2/V3 ST-LINK headers) without needing the board attached at test
time. Capturing needs only a connect — no buildable project — so it covers
boards the substrate can't build (legacy SW4STM32, external-flash parts).

A unit test (CLIs not invoked): it only re-parses captured text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embedagents.stm32.cubeprogrammer.parsers import parse_banner

REALBOARDS = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "cubeprogrammer"
    / "banners"
    / "realboards"
)

_FIXTURES = sorted(REALBOARDS.glob("*.txt")) if REALBOARDS.is_dir() else []


@pytest.mark.skipif(not _FIXTURES, reason="no real-board banners captured yet")
@pytest.mark.parametrize("banner_path", _FIXTURES, ids=lambda p: p.stem)
def test_realboard_banner_parses(banner_path: Path) -> None:
    """Each captured banner parses with a device id, a Cortex-M CPU, and a
    positive flash size — the device-name / CPU / flash lines the recipe
    surface relies on."""
    result = parse_banner(banner_path.read_text(encoding="utf-8"))
    assert result.device_id.startswith("0x"), f"{banner_path.name}: bad device_id {result.device_id!r}"
    assert result.device_name, f"{banner_path.name}: empty device_name"
    assert result.device_cpu.startswith("Cortex-M"), (
        f"{banner_path.name}: unexpected CPU {result.device_cpu!r}"
    )
    assert result.flash_size_kb > 0, f"{banner_path.name}: flash_size_kb not positive"
    assert result.stlink_sn, f"{banner_path.name}: empty ST-LINK SN"
