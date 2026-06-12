"""B9 tests — parse_hardfault + CubeProgrammer.analyze_hardfault (DIAG-001 binary)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.parsers import parse_hardfault
from embedagents.stm32.cubeprogrammer.results import HardFaultDecode
from embedagents.stm32.subprocess_runner import ToolRunResult


HF = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "hardfaults"


def _hf(name: str) -> str:
    return (HF / name).read_text(encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _ok(stdout: str) -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# parse_hardfault — no-fault path
# ---------------------------------------------------------------------------


class TestParseHardfaultNoFault:
    def test_returns_clean_decode(self) -> None:
        result = parse_hardfault(_hf("no-fault.txt"))
        assert isinstance(result, HardFaultDecode)
        assert result.hardfault_detected is False
        assert result.fault_type is None
        assert result.faulty_pc is None
        assert result.nvic_position is None
        assert result.register_snapshot == {}
        assert result.fault_decode == "No fault detected"
        assert result.source_used == "cubeprogrammer-hf"

    def test_empty_input_treated_as_no_fault(self) -> None:
        result = parse_hardfault("")
        assert result.hardfault_detected is False
        assert result.fault_type is None

    def test_no_status_line_treated_as_no_fault(self) -> None:
        result = parse_hardfault("some random text\nno status line")
        assert result.hardfault_detected is False


# ---------------------------------------------------------------------------
# parse_hardfault — UsageFault (escalated)
# ---------------------------------------------------------------------------


class TestParseHardfaultUsageFault:
    def test_detected(self) -> None:
        result = parse_hardfault(_hf("usagefault-undefinstr.txt"))
        assert result.hardfault_detected is True
        assert result.fault_type == "UsageFault"

    def test_faulty_pc(self) -> None:
        result = parse_hardfault(_hf("usagefault-undefinstr.txt"))
        assert result.faulty_pc == "0x080012AB"

    def test_nvic_position(self) -> None:
        result = parse_hardfault(_hf("usagefault-undefinstr.txt"))
        assert result.nvic_position == -1

    def test_register_snapshot_contains_cfsr_hfsr(self) -> None:
        result = parse_hardfault(_hf("usagefault-undefinstr.txt"))
        assert result.register_snapshot["CFSR"] == 0x00010000
        assert result.register_snapshot["HFSR"] == 0x40000000
        assert result.register_snapshot["MMFAR"] == 0xE000ED34
        assert result.register_snapshot["BFAR"] == 0xE000ED38

    def test_fault_decode_summary(self) -> None:
        result = parse_hardfault(_hf("usagefault-undefinstr.txt"))
        assert "UsageFault" in result.fault_decode
        assert "0x080012AB" in result.fault_decode
        assert "CFSR=0x00010000" in result.fault_decode
        assert "HFSR=0x40000000" in result.fault_decode


# ---------------------------------------------------------------------------
# parse_hardfault — MemManage
# ---------------------------------------------------------------------------


class TestParseHardfaultMemManage:
    def test_detected(self) -> None:
        result = parse_hardfault(_hf("memmanage-mpu-violation.txt"))
        assert result.hardfault_detected is True
        assert result.fault_type == "MemManage"

    def test_pc_and_mmfar(self) -> None:
        result = parse_hardfault(_hf("memmanage-mpu-violation.txt"))
        assert result.faulty_pc == "0x08001234"
        # MMFAR captures the offending data-access address.
        assert result.register_snapshot["MMFAR"] == 0x20020000
        assert result.register_snapshot["CFSR"] == 0x00000082

    def test_shcsr_captured(self) -> None:
        """SHCSR.MEMFAULTENA bit reveals whether MemManage handler was
        enabled — substrate captures the raw register so callers can
        inspect bit semantics."""
        result = parse_hardfault(_hf("memmanage-mpu-violation.txt"))
        assert result.register_snapshot["SHCSR"] == 0x00010000


# ---------------------------------------------------------------------------
# parse_hardfault — BusFault
# ---------------------------------------------------------------------------


class TestParseHardfaultBusFault:
    def test_detected(self) -> None:
        result = parse_hardfault(_hf("busfault-imprecise.txt"))
        assert result.hardfault_detected is True
        assert result.fault_type == "BusFault"

    def test_cfsr_bfsr_byte(self) -> None:
        """CFSR=0x00000400 → BFSR.IMPRECISERR=1."""
        result = parse_hardfault(_hf("busfault-imprecise.txt"))
        assert result.register_snapshot["CFSR"] == 0x00000400


# ---------------------------------------------------------------------------
# parse_hardfault — plain HardFault (debug-event)
# ---------------------------------------------------------------------------


class TestParseHardfaultHardFault:
    def test_detected(self) -> None:
        result = parse_hardfault(_hf("hardfault-only.txt"))
        assert result.hardfault_detected is True
        assert result.fault_type == "HardFault"

    def test_debug_event_hfsr_bit(self) -> None:
        result = parse_hardfault(_hf("hardfault-only.txt"))
        assert result.register_snapshot["HFSR"] == 0x80000000


# ---------------------------------------------------------------------------
# parse_hardfault — edge cases
# ---------------------------------------------------------------------------


class TestParseHardfaultCliV222MinimalFormat:
    """CLI 2.22 emits a minimal fault-analyzer format when its decoder
    can't capture rich register state (e.g. against a HardFault_Handler
    tight loop that hasn't preserved CFSR/HFSR/SCB). No "Status:" line;
    detection carried inline by "Hard Fault detected in instruction
    located at 0x...". Bench-captured 2026-05-19 on NUCLEO-L476RG with
    UDF #0 firmware + HOTPLUG-mode connect (per analyze_hardfault fix
    same date)."""

    def test_minimal_format_detected_with_inline_pc(self) -> None:
        result = parse_hardfault(_hf("cli-v2.22-minimal-format.txt"))
        assert isinstance(result, HardFaultDecode)
        assert result.hardfault_detected is True
        # Inline PC is harvested even without a "Faulty PC:" line.
        assert result.faulty_pc == "0x2000089F"
        # Rich format fields absent: no Fault type / NVIC / SCB block in
        # this CLI output → all None/empty, decode falls back to a
        # canonical "Fault" label.
        assert result.fault_type is None
        assert result.nvic_position is None
        assert result.register_snapshot == {}
        assert result.source_used == "cubeprogrammer-hf"


class TestParseHardfaultEdgeCases:
    def test_unknown_fault_type_falls_back_to_none(self) -> None:
        """Free-text fault label outside the Literal set → fault_type=None,
        but raw label survives in fault_decode."""
        text = (
            "HardFault Analyzer\n"
            "------------------\n"
            "Status: HardFault detected\n"
            "Faulty PC      : 0x08000000\n"
            "NVIC position  : -1\n"
            "Fault type     : ExoticFutureFault\n"
            "CFSR : 0x00000000\n"
        )
        result = parse_hardfault(text)
        assert result.hardfault_detected is True
        assert result.fault_type is None
        assert "ExoticFutureFault" in result.fault_decode

    def test_ansi_escapes_tolerated(self) -> None:
        raw = _hf("usagefault-undefinstr.txt")
        wrapped = f"\x1b[31m{raw}\x1b[0m"
        result = parse_hardfault(wrapped)
        assert result.hardfault_detected is True
        assert result.fault_type == "UsageFault"
        assert result.faulty_pc == "0x080012AB"

    def test_register_snapshot_filters_unknown_keys(self) -> None:
        """Random `KEY : 0x<hex>` lines that aren't in the canonical
        register set should NOT pollute the snapshot."""
        text = (
            "HardFault Analyzer\n"
            "Status: HardFault detected\n"
            "FANCYREG : 0xDEADBEEF\n"
            "CFSR : 0x00000001\n"
        )
        result = parse_hardfault(text)
        assert result.register_snapshot == {"CFSR": 0x00000001}
        assert "FANCYREG" not in result.register_snapshot

    def test_missing_pc_handled_gracefully(self) -> None:
        text = (
            "HardFault Analyzer\n"
            "Status: HardFault detected\n"
            "Fault type     : HardFault\n"
            "CFSR : 0x00000000\n"
        )
        result = parse_hardfault(text)
        assert result.hardfault_detected is True
        assert result.faulty_pc is None
        assert result.nvic_position is None
        assert result.fault_decode == "HardFault (CFSR=0x00000000)"


# ---------------------------------------------------------------------------
# analyze_hardfault — runner integration
# ---------------------------------------------------------------------------


class TestAnalyzeHardfaultIntegration:
    def test_argv_uses_dash_hf(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_ok(_hf("no-fault.txt")),
        ) as mocked:
            result = client.analyze_hardfault()
        argv = mocked.call_args[0][1]
        # mode=HOTPLUG preserves the chip's fault registers (CFSR/HFSR/
        # SHCSR/MMFAR/BFAR) across the connect — Normal mode would apply
        # a Software reset and wipe them before -hf reads them.
        assert argv == ["-c", "port=swd", "mode=HOTPLUG", "-hf"]
        assert result.hardfault_detected is False

    def test_uses_atomic_timeout(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_ok(_hf("no-fault.txt")),
        ) as mocked:
            client.analyze_hardfault()
        assert mocked.call_args.kwargs["timeout_s"] == 30.0

    def test_no_fault_does_not_raise(self, ctx: SubstrateContext) -> None:
        """No-fault is a valid result, not an exception."""
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_ok(_hf("no-fault.txt")),
        ):
            result = client.analyze_hardfault()
        assert result.hardfault_detected is False
        assert result.source_used == "cubeprogrammer-hf"

    def test_detected_fault_returns_decode(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_ok(_hf("usagefault-undefinstr.txt")),
        ):
            result = client.analyze_hardfault()
        assert result.hardfault_detected is True
        assert result.fault_type == "UsageFault"
        assert result.faulty_pc == "0x080012AB"
        assert result.source_used == "cubeprogrammer-hf"

    def test_warning_on_detected_fault(
        self,
        ctx: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeProgrammer(ctx)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool",
                return_value=_ok(_hf("memmanage-mpu-violation.txt")),
            ):
                client.analyze_hardfault()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings
        assert "MemManage" in warnings[0].message
        assert "0x08001234" in warnings[0].message

    def test_info_on_no_fault(
        self,
        ctx: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeProgrammer(ctx)
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool",
                return_value=_ok(_hf("no-fault.txt")),
            ):
                client.analyze_hardfault()
        msgs = [r.message for r in caplog.records]
        assert any("no fault" in m.lower() for m in msgs)
