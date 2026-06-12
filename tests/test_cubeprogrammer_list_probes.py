"""B4 tests — parse_probe_list + CubeProgrammer.list_probes()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.codes import CubeProgrammerErrorCode
from stm32_substrate.cubeprogrammer.parsers import parse_probe_list
from stm32_substrate.cubeprogrammer.results import ProbeRecord
from stm32_substrate.errors import CubeProgrammerError, ToolError
from stm32_substrate.subprocess_runner import ToolRunResult


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "probe-lists"
ERRORS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "errors"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_probe_list — pure parser
# ---------------------------------------------------------------------------


class TestParseProbeListEmpty:
    def test_no_probes_returns_empty_list(self) -> None:
        result = parse_probe_list(_load("empty.txt"))
        assert result == []

    def test_completely_blank_input(self) -> None:
        assert parse_probe_list("") == []


class TestParseProbeListSingle:
    def test_one_probe_parsed(self) -> None:
        result = parse_probe_list(_load("single-stlink-v3.txt"))
        assert len(result) == 1
        record = result[0]
        assert isinstance(record, ProbeRecord)
        assert record.stlink_sn == "066BFF514852898767094734"
        assert record.stlink_fw == "V3J11M3"
        assert record.board_name == "NUCLEO-L476RG"

    def test_v1_defaults(self) -> None:
        record = parse_probe_list(_load("single-stlink-v3.txt"))[0]
        assert record.target_sel is None
        assert record.query_failed is False
        assert record.multidrop_unavailable is False


class TestParseProbeListMultiple:
    def test_two_probes_parsed_in_order(self) -> None:
        result = parse_probe_list(_load("two-stlinks.txt"))
        assert len(result) == 2
        first, second = result
        assert first.stlink_sn == "066BFF514852898767094734"
        assert first.board_name == "NUCLEO-L476RG"
        assert second.stlink_sn == "002D003D3438511434343935"
        assert second.board_name is None  # "--" → None

    def test_v3_and_v2_firmware_versions(self) -> None:
        result = parse_probe_list(_load("two-stlinks.txt"))
        assert result[0].stlink_fw == "V3J11M3"
        assert result[1].stlink_fw == "V2J37M27"


class TestParseProbeListEdgeCases:
    def test_dash_dash_board_yields_none(self) -> None:
        result = parse_probe_list(_load("stlink-without-board.txt"))
        assert len(result) == 1
        assert result[0].board_name is None

    def test_ansi_escapes_tolerated(self) -> None:
        raw = _load("single-stlink-v3.txt")
        wrapped = f"\x1b[36m{raw}\x1b[0m"
        result = parse_probe_list(wrapped)
        assert len(result) == 1
        assert result[0].board_name == "NUCLEO-L476RG"

    def test_block_without_sn_dropped(self) -> None:
        """Defensive: a malformed probe block missing SN is silently skipped."""
        text = (
            "=====  STLink Interface  =====\n"
            "\n"
            "ST-Link Probe 0 :\n"
            "   ST-LINK FW  : V3J11M3\n"
            "   Board       : NUCLEO-L476RG\n"
        )
        result = parse_probe_list(text)
        assert result == []


# ---------------------------------------------------------------------------
# CubeProgrammer.list_probes() — runner wired through mocked run_tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx_with_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _success_result(stdout: str) -> ToolRunResult:
    return ToolRunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration_s=0.05,
        timed_out=False,
    )


class TestListProbesEmpty:
    def test_no_probes_returns_empty_list(self, ctx_with_cli: SubstrateContext) -> None:
        result = _success_result(_load("empty.txt"))
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ):
            probes = client.list_probes()
        assert probes == []

    def test_argv_is_dash_l(self, ctx_with_cli: SubstrateContext) -> None:
        result = _success_result(_load("empty.txt"))
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ) as mocked:
            client.list_probes()
        args = mocked.call_args[0][1]
        assert args == ["-l"]
        # raise_on_nonzero is disabled for list_probes per docstring rationale.
        assert mocked.call_args[1]["raise_on_nonzero"] is False


class TestListProbesSingle:
    def test_one_probe_in_result(self, ctx_with_cli: SubstrateContext) -> None:
        result = _success_result(_load("single-stlink-v3.txt"))
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ):
            probes = client.list_probes()
        assert len(probes) == 1
        assert probes[0].stlink_sn == "066BFF514852898767094734"
        assert probes[0].board_name == "NUCLEO-L476RG"


class TestListProbesErrors:
    def test_non_zero_exit_raises_typed_error(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        result = ToolRunResult(
            exit_code=2,
            stdout="",
            stderr=(ERRORS / "target-dll-err.txt").read_text(),
            duration_s=0.05,
            timed_out=False,
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.list_probes()
        err = excinfo.value
        assert err.error_code == CubeProgrammerErrorCode.TARGET_DLL_ERR
        assert err.code == 2

    def test_runner_timeout_is_translated(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        runner_err = ToolError(
            message="STM32_Programmer_CLI timed out after 30s",
            code="timeout",
            tool_output="",
            hint="raise the timeout knob or check the device responsiveness",
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.list_probes()
        # IMP-03: run_tool's timeout diagnosis is preserved — previously
        # rendered as the misleading "exited with code -1" with no hint.
        err = excinfo.value
        assert err.code == "timeout"
        assert "timed out after 30s" in err.message
        assert err.hint == runner_err.hint


class TestListProbesLogging:
    def test_info_log_includes_probe_count(
        self, ctx_with_cli: SubstrateContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        result = _success_result(_load("two-stlinks.txt"))
        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="stm32_substrate.cubeprogrammer"):
            with patch(
                "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
            ):
                client.list_probes()
        msgs = [r.message for r in caplog.records]
        assert any("list_probes detected 2 probe" in m for m in msgs)


# ---------------------------------------------------------------------------
# Live capture from STM32CubeProgrammer 2.22.0 on Windows + L476RG bench.
# v2.22 probe-list output uses "Board Name" (not "Board" as the legacy
# synthesised fixtures had) — this exercises the drift-tolerant lookup.
# ---------------------------------------------------------------------------


class TestLiveProbeListL476RG_v2_22:
    def test_board_name_resolved_from_board_name_key(self) -> None:
        result = parse_probe_list(_load("nucleo-l476rg-live-v2.22.txt"))
        assert len(result) == 1
        assert result[0].board_name == "NUCLEO-L476RG"
        assert result[0].stlink_sn == "066DFF485754727567021514"
        assert result[0].stlink_fw == "V2J46M32"
