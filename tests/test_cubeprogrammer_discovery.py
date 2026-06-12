"""B5a tests — banner-subset views (connect_under_reset, board_name,
memory_layout, cores) + the _raw_connect refactor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.results import (
    BannerResult,
    CoresResult,
    MemoryLayoutResult,
)
from embedagents.stm32.errors import ResolutionError
from embedagents.stm32.subprocess_runner import ToolRunResult


BANNERS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "banners"


def _banner(name: str) -> str:
    return (BANNERS / name).read_text(encoding="utf-8")


def _success(stdout: str) -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


@pytest.fixture()
def ctx_with_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


# ---------------------------------------------------------------------------
# _raw_connect — building block used by connect / connect_under_reset / ping_swd
# ---------------------------------------------------------------------------


class TestRawConnect:
    def test_returns_banner_no_logging(
        self,
        ctx_with_cli: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool",
                return_value=_success(_banner("nucleo-l476rg-good.txt")),
            ):
                banner = client._raw_connect()
        assert isinstance(banner, BannerResult)
        assert banner.board_name == "NUCLEO-L476RG"
        # _raw_connect is the no-logging path — used by D-002 ladder.
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert info_records == [], "expected no INFO log from _raw_connect"

    def test_mode_argument_appears_in_argv(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-under-reset.txt")),
        ) as mocked:
            client._raw_connect(mode="UR")
        argv = mocked.call_args[0][1]
        assert "mode=UR" in argv

    def test_no_mode_argument_when_omitted(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-good.txt")),
        ) as mocked:
            client._raw_connect()
        argv = mocked.call_args[0][1]
        assert not any(arg.startswith("mode=") for arg in argv)


# ---------------------------------------------------------------------------
# connect_under_reset — D-011
# ---------------------------------------------------------------------------


class TestConnectUnderReset:
    def test_uses_mode_ur(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-under-reset.txt")),
        ) as mocked:
            banner = client.connect_under_reset()
        argv = mocked.call_args[0][1]
        assert "mode=UR" in argv
        assert banner.mode_used == "UR"
        assert banner.board_name == "NUCLEO-L476RG"

    def test_logs_info_on_success(
        self,
        ctx_with_cli: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool",
                return_value=_success(_banner("nucleo-l476rg-under-reset.txt")),
            ):
                client.connect_under_reset()
        msgs = [r.message for r in caplog.records]
        assert any("connected" in m and "NUCLEO-L476RG" in m for m in msgs)


# ---------------------------------------------------------------------------
# board_name — D-003
# ---------------------------------------------------------------------------


class TestBoardName:
    def test_returns_board_name_from_banner(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-good.txt")),
        ):
            name = client.board_name()
        assert name == "NUCLEO-L476RG"

    def test_raises_resolution_error_when_custom_board(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("custom-board-no-name.txt")),
        ):
            with pytest.raises(ResolutionError) as excinfo:
                client.board_name()
        err = excinfo.value
        assert "no catalog name" in err.message
        assert err.hint is not None
        assert "firmware.board" in err.hint


# ---------------------------------------------------------------------------
# memory_layout — D-004
# ---------------------------------------------------------------------------


class TestMemoryLayout:
    def test_flash_size_from_banner(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-good.txt")),
        ):
            layout = client.memory_layout()
        assert isinstance(layout, MemoryLayoutResult)
        assert layout.flash_size_kb == 1024
        assert layout.device_name == "STM32L47xxx/L48xxx"

    def test_ram_and_bank_layout_none_in_v1(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        """No DeviceDB in v1 (cubemx scope cut) — these surface as None
        rather than incorrect mini-table guesses per RES-020."""
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-good.txt")),
        ):
            layout = client.memory_layout()
        assert layout.ram_size_kb is None
        assert layout.bank_layout is None

    def test_flash_size_kbytes_unit(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("flash-size-kbytes.txt")),
        ):
            layout = client.memory_layout()
        assert layout.flash_size_kb == 256


# ---------------------------------------------------------------------------
# cores — D-007
# ---------------------------------------------------------------------------


class TestCores:
    def test_primary_core_from_banner(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-good.txt")),
        ):
            result = client.cores()
        assert isinstance(result, CoresResult)
        assert result.primary_core == "Cortex-M4"
        assert result.device_name == "STM32L47xxx/L48xxx"

    def test_secondary_cores_empty_in_v1(self, ctx_with_cli: SubstrateContext) -> None:
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_banner("nucleo-l476rg-good.txt")),
        ):
            result = client.cores()
        assert result.secondary_cores == []
        assert result.multi_core is None
