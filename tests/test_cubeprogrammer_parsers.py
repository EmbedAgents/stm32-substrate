"""B2 parser tests — banner parsing across happy paths + edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from embedagents.stm32.cubeprogrammer.parsers import parse_banner
from embedagents.stm32.cubeprogrammer.results import BannerResult


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "banners"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy path: NUCLEO-L476RG canonical banner
# ---------------------------------------------------------------------------


class TestNucleoL476rgGood:
    def test_returns_banner_result(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert isinstance(result, BannerResult)

    def test_stlink_fields(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.stlink_sn == "066BFF514852898767094734"
        assert result.stlink_fw == "V3J11M3"

    def test_board_and_voltage(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.board_name == "NUCLEO-L476RG"
        assert result.voltage_v == pytest.approx(3.28)
        assert result.voltage_suspicious is False

    def test_swd_freq(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.swd_freq_khz == 4000

    def test_device_ids(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.device_id == "0x415"
        assert result.device_name == "STM32L47xxx/L48xxx"
        assert result.device_type == "MCU"
        assert result.device_cpu == "Cortex-M4"

    def test_flash_size_mbytes_to_kb(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.flash_size_kb == 1024  # "1 MBytes" → 1024 KB

    def test_default_connect_mode(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.mode_used == "NORMAL"


# ---------------------------------------------------------------------------
# Voltage suspicious flag
# ---------------------------------------------------------------------------


class TestSuspiciousVoltage:
    def test_below_threshold_flagged(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-suspicious-voltage.txt"))
        assert result.voltage_v == pytest.approx(2.32)
        assert result.voltage_suspicious is True

    def test_above_threshold_not_flagged(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.voltage_v >= 2.5
        assert result.voltage_suspicious is False

    def test_missing_voltage_does_not_falsely_flag(self) -> None:
        """A banner missing the Voltage line yields 0.0 — the threshold
        comparison must NOT flag this case as suspicious (zero just means
        the field is absent, not actually low)."""
        text = (FIXTURES / "nucleo-l476rg-good.txt").read_text(encoding="utf-8")
        # Strip out the Voltage line entirely.
        text = "\n".join(line for line in text.splitlines() if "Voltage" not in line)
        result = parse_banner(text)
        assert result.voltage_v == 0.0
        assert result.voltage_suspicious is False


# ---------------------------------------------------------------------------
# Connect mode mapping
# ---------------------------------------------------------------------------


class TestConnectMode:
    def test_under_reset(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-under-reset.txt"))
        assert result.mode_used == "UR"
        assert result.swd_freq_khz == 1800

    def test_unknown_mode_falls_back_to_normal(self) -> None:
        text = "Connect mode: Some Future Mode\n"
        result = parse_banner(text)
        assert result.mode_used == "NORMAL"


# ---------------------------------------------------------------------------
# Custom board (no Board name)
# ---------------------------------------------------------------------------


class TestCustomBoard:
    def test_dash_dash_yields_none(self) -> None:
        result = parse_banner(_load("custom-board-no-name.txt"))
        assert result.board_name is None
        # Device fields still parse correctly.
        assert result.device_id == "0x435"
        assert result.device_name == "STM32L4x6"


# ---------------------------------------------------------------------------
# Locale variations
# ---------------------------------------------------------------------------


class TestLocale:
    def test_comma_decimal_voltage(self) -> None:
        result = parse_banner(_load("locale-comma-decimal.txt"))
        assert result.voltage_v == pytest.approx(3.28)
        assert result.voltage_suspicious is False


# ---------------------------------------------------------------------------
# Flash size unit handling
# ---------------------------------------------------------------------------


class TestFlashSize:
    def test_kbytes_unit(self) -> None:
        result = parse_banner(_load("flash-size-kbytes.txt"))
        assert result.flash_size_kb == 256

    def test_mbytes_unit(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-good.txt"))
        assert result.flash_size_kb == 1024


# ---------------------------------------------------------------------------
# ANSI escape tolerance
# ---------------------------------------------------------------------------


class TestAnsiEscapes:
    def test_ansi_codes_stripped(self) -> None:
        # Re-wrap a banner with the same ANSI escape codes the real CLI emits.
        raw = _load("nucleo-l476rg-good.txt")
        ansi_wrapped = f"\x1b[36m\x1b[01m{raw}\x1b[39;49m\x1b[0m"
        result = parse_banner(ansi_wrapped)
        assert result.board_name == "NUCLEO-L476RG"
        assert result.voltage_v == pytest.approx(3.28)


# ---------------------------------------------------------------------------
# Missing-field defaults
# ---------------------------------------------------------------------------


class TestMissingFields:
    def test_completely_empty_input(self) -> None:
        result = parse_banner("")
        assert result.stlink_sn == ""
        assert result.stlink_fw == ""
        assert result.board_name is None
        assert result.voltage_v == 0.0
        assert result.swd_freq_khz == 0
        assert result.device_id == ""
        assert result.flash_size_kb == 0
        assert result.voltage_suspicious is False

    def test_header_only_input(self) -> None:
        """Stripped down to just the version header — every field defaults."""
        text = (
            "      -------------------------------------------------------------------\n"
            "                        STM32CubeProgrammer v2.22.0\n"
            "      -------------------------------------------------------------------\n"
        )
        result = parse_banner(text)
        assert result.board_name is None
        assert result.device_name == ""


# ---------------------------------------------------------------------------
# Field-line regex robustness
# ---------------------------------------------------------------------------


class TestFieldExtraction:
    def test_variable_whitespace(self) -> None:
        text = "Board:NUCLEO-X\nVoltage   :   3.30V\nSWD freq:8000 KHz\n"
        result = parse_banner(text)
        assert result.board_name == "NUCLEO-X"
        assert result.voltage_v == pytest.approx(3.30)
        assert result.swd_freq_khz == 8000

    def test_trailing_whitespace(self) -> None:
        text = "Board       : NUCLEO-L476RG   \n"
        result = parse_banner(text)
        assert result.board_name == "NUCLEO-L476RG"


# ---------------------------------------------------------------------------
# Live capture from STM32CubeProgrammer 2.22.0 on Windows + L476RG bench.
# These tests exercise the v2.22 field-name drift (Board Name / NVM size).
# ---------------------------------------------------------------------------


class TestLiveBannerL476RG_v2_22:
    """nucleo-l476rg-live-v2.22.txt is a real capture; the older
    nucleo-l476rg-good.txt fixture used the legacy "Board" / "Flash size"
    field names. v2.22.0 emits "Board       : ..." (still "Board" for the
    banner, no drift) but "NVM size  : ..." (drift). The probe-list parser
    drift is exercised separately in test_cubeprogrammer_list_probes.py.
    """

    def test_board_name_resolved_from_live_banner(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-live-v2.22.txt"))
        assert result.board_name == "NUCLEO-L476RG"

    def test_flash_size_kb_resolved_from_nvm_size_field(self) -> None:
        """v2.22 emits 'NVM size  : 1 MBytes' (not 'Flash size'). Parser
        accepts both keys; live capture exercises the NVM variant."""
        result = parse_banner(_load("nucleo-l476rg-live-v2.22.txt"))
        assert result.flash_size_kb == 1024  # 1 MBytes -> 1024 KB

    def test_voltage_and_device_id_unchanged_in_v2_22(self) -> None:
        result = parse_banner(_load("nucleo-l476rg-live-v2.22.txt"))
        assert result.voltage_v == pytest.approx(3.25, abs=0.05)
        assert result.device_id == "0x415"
        assert result.device_cpu == "Cortex-M4"
