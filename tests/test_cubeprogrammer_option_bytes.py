"""B5c tests — parse_option_bytes + CubeProgrammer.read_option_bytes()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.parsers import parse_option_bytes
from stm32_substrate.cubeprogrammer.results import OptionBytesResult
from stm32_substrate.subprocess_runner import ToolRunResult


OB = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "option-bytes"


def _ob(name: str) -> str:
    return (OB / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_option_bytes — pure parser
# ---------------------------------------------------------------------------


class TestParseOptionBytesL4Default:
    def test_returns_option_bytes_result(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-default.txt"), device_name="STM32L47xxx/L48xxx")
        assert isinstance(result, OptionBytesResult)
        assert result.device_name == "STM32L47xxx/L48xxx"

    def test_rdp_level_zero(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-default.txt"), device_name="STM32L47xxx/L48xxx")
        assert result.rdp_level == 0

    def test_rdp_value_in_observed(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-default.txt"), device_name="STM32L47xxx/L48xxx")
        # Hex values are coerced to int.
        assert result.observed["RDP"] == 0xAA

    def test_multiple_fields_captured(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-default.txt"), device_name="STM32L47xxx/L48xxx")
        expected_keys = {
            "RDP",
            "BOR_LEV",
            "nRST_STOP",
            "nRST_STDBY",
            "nRST_SHDW",
            "IWDG_SW",
            "IWDG_STOP",
            "IWDG_STDBY",
            "WWDG_SW",
            "nBOOT1",
            "SRAM2_PE",
            "SRAM2_RST",
            "BFB2",
            "DBANK",
            "DB1M",
        }
        assert expected_keys.issubset(set(result.observed.keys()))

    def test_redacted_flag_false_by_default(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-default.txt"), device_name="STM32L47xxx/L48xxx")
        assert result.redacted_due_to_rdp is False


class TestParseOptionBytesRdpLevels:
    def test_rdp1_byte_yields_level_one(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-rdp1.txt"), device_name="STM32L47xxx/L48xxx")
        assert result.observed["RDP"] == 0x55
        assert result.rdp_level == 1

    def test_rdp2_byte_yields_level_two(self) -> None:
        result = parse_option_bytes(_ob("stm32l4-rdp2.txt"), device_name="STM32L47xxx/L48xxx")
        assert result.observed["RDP"] == 0xCC
        assert result.rdp_level == 2

    def test_arbitrary_non_aa_non_cc_yields_level_one(self) -> None:
        """Any byte other than 0xAA / 0xCC maps to level 1 per universal
        STM32 convention."""
        text = "  RDP          : 0x42 (some weird value)\n"
        result = parse_option_bytes(text, device_name="STM32X")
        assert result.rdp_level == 1


class TestParseOptionBytesValueCoercion:
    def test_hex_value_coerced_to_int(self) -> None:
        text = "  IWDG_SW      : 0x1 (Software watchdog)\n"
        result = parse_option_bytes(text, device_name="STM32X")
        assert result.observed["IWDG_SW"] == 1

    def test_plain_integer_value_coerced(self) -> None:
        text = "  CUSTOM_FIELD : 42 (no hex)\n"
        result = parse_option_bytes(text, device_name="STM32X")
        assert result.observed["CUSTOM_FIELD"] == 42

    def test_section_header_not_captured(self) -> None:
        """``Read Out Protection:`` is a section header (no value after
        the colon) and must not become a field entry."""
        text = (
            "Read Out Protection:\n"
            "  RDP          : 0xAA (Level 0)\n"
            "BOR Level:\n"
            "  BOR_LEV      : 0x0 (Level 0)\n"
        )
        result = parse_option_bytes(text, device_name="STM32X")
        assert "Read Out Protection" not in result.observed
        assert "BOR Level" not in result.observed
        assert "RDP" in result.observed
        assert "BOR_LEV" in result.observed


class TestParseOptionBytesEmptyInput:
    def test_empty_string(self) -> None:
        result = parse_option_bytes("", device_name="STM32L4")
        assert result.observed == {}
        assert result.rdp_level is None

    def test_banner_only_no_ob_section(self) -> None:
        """Banner without an OB section — observed stays empty, rdp_level None."""
        text = "ST-LINK SN  : 066BFF\nDevice name : STM32L4\n"
        result = parse_option_bytes(text, device_name="STM32L4")
        assert "RDP" not in result.observed
        assert result.rdp_level is None


# ---------------------------------------------------------------------------
# CubeProgrammer.read_option_bytes() — runner wired through mocked run_tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx_with_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _success(stdout: str) -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


class TestReadOptionBytesIntegration:
    def test_happy_path(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(_ob("stm32l4-default.txt")),
        ):
            result = client.read_option_bytes()
        assert isinstance(result, OptionBytesResult)
        assert result.device_name == "STM32L47xxx/L48xxx"
        assert result.rdp_level == 0
        assert result.observed["RDP"] == 0xAA

    def test_invokes_ob_displ_argv(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(_ob("stm32l4-default.txt")),
        ) as mocked:
            client.read_option_bytes()
        argv = mocked.call_args[0][1]
        assert "-ob" in argv
        assert "displ" in argv
        assert argv.index("displ") == argv.index("-ob") + 1

    def test_rdp2_path(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool",
            return_value=_success(_ob("stm32l4-rdp2.txt")),
        ):
            result = client.read_option_bytes()
        assert result.rdp_level == 2

    def test_logs_info(
        self,
        ctx_with_cli: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="stm32_substrate.cubeprogrammer"):
            with patch(
                "stm32_substrate.cubeprogrammer.client.run_tool",
                return_value=_success(_ob("stm32l4-default.txt")),
            ):
                client.read_option_bytes()
        msgs = [r.message for r in caplog.records]
        assert any("read_option_bytes" in m and "rdp_level=0" in m for m in msgs)
