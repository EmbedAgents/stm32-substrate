"""B10 tests — parse_itm_line + CubeProgrammer.tail_swo (VCP-007).

The streaming path uses ``subprocess.Popen`` directly (bypasses
``run_tool`` because tail_swo is unbounded by design). Tests mock
``subprocess.Popen`` with a fake whose stdout is a canned line iterator
+ a recorded terminate/kill so we can verify cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.parsers import (
    is_swv_dropped_bytes_warning,
    parse_itm_line,
)
from embedagents.stm32.cubeprogrammer.results import ITMRecord
from embedagents.stm32.subprocess_runner import ToolRunResult


SWV = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "swv"


def _swv(name: str) -> str:
    return (SWV / name).read_text(encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


# ---------------------------------------------------------------------------
# parse_itm_line — pure parser
# ---------------------------------------------------------------------------


class TestParseItmLineChannelFormat:
    @pytest.mark.parametrize(
        "raw,expected_port,expected_payload",
        [
            ("ITM channel 0: Hello, STM32!", 0, "Hello, STM32!"),
            ("ITM channel 1: net: rx_packet len=128", 1, "net: rx_packet len=128"),
            ("ITM channel 2: trace: enter ISR", 2, "trace: enter ISR"),
            ("ITM channel 15: edge", 15, "edge"),
        ],
    )
    def test_channel_lines(
        self, raw: str, expected_port: int, expected_payload: str
    ) -> None:
        record = parse_itm_line(raw)
        assert isinstance(record, ITMRecord)
        assert record.port_number == expected_port
        assert record.line == expected_payload


class TestParseItmLineBracketFormat:
    def test_bracket_form(self) -> None:
        record = parse_itm_line("[0] counter=42")
        assert record is not None
        assert record.port_number == 0
        assert record.line == "counter=42"

    def test_bracket_with_port_number(self) -> None:
        record = parse_itm_line("[3] payload")
        assert record is not None
        assert record.port_number == 3


class TestParseItmLineTimestamp:
    def test_timestamp_forwarded(self) -> None:
        record = parse_itm_line("ITM channel 0: hello", timestamp_s=12.5)
        assert record is not None
        assert record.timestamp_s == pytest.approx(12.5)

    def test_timestamp_defaults_to_zero(self) -> None:
        record = parse_itm_line("ITM channel 0: hello")
        assert record is not None
        assert record.timestamp_s == 0.0


class TestParseItmLineNoise:
    @pytest.mark.parametrize(
        "noise",
        [
            "ST-LINK SN  : 066BFF",
            "Board       : NUCLEO-L476RG",
            "Voltage     : 3.28V",
            "SWD freq    : 4000 KHz",
            "Device name : STM32L47xxx/L48xxx",
            "SWV started on port 0, frequency 80 MHz",
            "WARNING: SWV dropped 16 bytes (overflow)",
            "Error: foo",
            "                        STM32CubeProgrammer v2.22.0",
            "------------------",
            "===================",
            "",
            "   ",
            # Real v2.22 interactive -swv chrome (bench 2026-05-24) — these
            # previously leaked through the free-form fallback as port-0
            # records and corrupted the ITM stream.
            "NVM size  : 1 MBytes",
            "Debug in Low Power mode enabled",
            "Entering Serial Wire Viewer reception mode :",
            "Press R to Start the reception",
            "Press S to Stop the reception",
            "Press E to Exit this mode",
            "Reception Started",
            "Reception Stopped",
            "Exiting Serial Wire Viewer mode",
        ],
    )
    def test_returns_none_for_noise(self, noise: str) -> None:
        assert parse_itm_line(noise) is None


class TestParseItmLineFreeForm:
    def test_unrecognised_text_goes_to_port_zero(self) -> None:
        """Lines that don't match ITM patterns but aren't noise default
        to port 0 — supports consumers using tail_swo as a generic line
        capture."""
        record = parse_itm_line("just some printf without channel prefix")
        assert record is not None
        assert record.port_number == 0
        assert record.line == "just some printf without channel prefix"

    def test_strips_trailing_whitespace(self) -> None:
        record = parse_itm_line("ITM channel 0: hello   \r\n")
        assert record is not None
        assert record.line == "hello"


class TestParseItmLineAnsi:
    def test_strips_ansi_escapes(self) -> None:
        raw = "\x1b[32mITM channel 0: green text\x1b[0m"
        record = parse_itm_line(raw)
        assert record is not None
        assert record.port_number == 0
        assert record.line == "green text"


# ---------------------------------------------------------------------------
# is_swv_dropped_bytes_warning
# ---------------------------------------------------------------------------


class TestSwvDroppedBytesWarning:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("WARNING: SWV dropped 16 bytes (overflow)", True),
            ("SWV dropped 4 bytes", True),
            ("SWO overflow detected at offset N", True),
            ("ITM channel 0: hello", False),
            ("Board: NUCLEO-L476RG", False),
            ("", False),
        ],
    )
    def test_detection(self, raw: str, expected: bool) -> None:
        assert is_swv_dropped_bytes_warning(raw) is expected


# ---------------------------------------------------------------------------
# tail_swo — streaming via mocked subprocess.Popen
# ---------------------------------------------------------------------------


def _make_fake_popen(lines: list[str]) -> MagicMock:
    """Build a Popen-shaped mock with stdout that iterates ``lines``.

    Records terminate / kill / wait so cleanup-path tests can assert.
    """
    mock = MagicMock(spec=subprocess.Popen)
    mock.stdout = iter(lines)
    mock.poll = MagicMock(return_value=None)  # alive during iteration
    # On terminate, flip poll() to return 0 so subsequent calls see "dead".
    def _terminate():
        mock.poll = MagicMock(return_value=0)
    mock.terminate = MagicMock(side_effect=_terminate)
    mock.kill = MagicMock()
    mock.wait = MagicMock(return_value=0)
    mock.pid = 12345
    return mock


class TestTailSwoArgvShape:
    def test_argv_includes_swv_freq_port(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ) as popen:
            list(client.tail_swo(freq_mhz=80.0, port_number=2))
        argv = popen.call_args[0][0]
        # First element is the CLI path; the rest is the substrate's args.
        # `-startswv` is the non-interactive auto-start form (plain `-swv`
        # blocks at an interactive menu and captures nothing under DEVNULL).
        assert argv[1:] == [
            "-c",
            "port=swd",
            "-startswv",
            "freq=80.0",
            "portnumber=2",
        ]

    def test_default_port_zero(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ) as popen:
            list(client.tail_swo(freq_mhz=80.0))
        argv = popen.call_args[0][0]
        assert "portnumber=0" in argv

    def test_log_path_appended(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        log = tmp_path / "swv.log"
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ) as popen:
            list(client.tail_swo(freq_mhz=80.0, log_path=log))
        argv = popen.call_args[0][0]
        assert str(log) in argv

    def test_start_new_session_true(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ) as popen:
            list(client.tail_swo(freq_mhz=80.0))
        assert popen.call_args.kwargs["start_new_session"] is True

    def test_uses_startswv_auto_start_not_interactive_swv(
        self, ctx: SubstrateContext
    ) -> None:
        """Plain `-swv` is interactive (blocks at a menu waiting for `R`)
        and captures nothing under DEVNULL stdin. tail_swo must use the
        `-startswv` auto-start form so reception begins immediately with no
        stdin interaction (bench fix 2026-05-24)."""
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ) as popen:
            list(client.tail_swo(freq_mhz=80.0))
        argv = popen.call_args[0][0]
        assert "-startswv" in argv
        assert "-swv" not in argv  # the interactive form must not be used
        # No stdin interaction needed with auto-start.
        assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL


class TestTailSwoStreaming:
    def test_yields_itm_records(self, ctx: SubstrateContext) -> None:
        lines = [
            "SWV started on port 0, frequency 80 MHz\n",  # noise
            "ITM channel 0: hello\n",
            "ITM channel 0: counter=0\n",
            "ITM channel 1: trace\n",
        ]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            records = list(client.tail_swo(freq_mhz=80.0))
        # Noise filtered → 3 records left.
        assert len(records) == 3
        assert records[0].port_number == 0
        assert records[0].line == "hello"
        assert records[1].line == "counter=0"
        assert records[2].port_number == 1
        assert records[2].line == "trace"

    def test_timestamps_monotonic_and_increasing(
        self, ctx: SubstrateContext
    ) -> None:
        lines = [f"ITM channel 0: tick {i}\n" for i in range(5)]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            records = list(client.tail_swo(freq_mhz=80.0))
        timestamps = [r.timestamp_s for r in records]
        assert timestamps == sorted(timestamps)
        assert all(t >= 0 for t in timestamps)

    def test_skips_blank_lines(self, ctx: SubstrateContext) -> None:
        lines = [
            "\n",
            "ITM channel 0: hello\n",
            "   \n",
            "ITM channel 0: world\n",
        ]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            records = list(client.tail_swo(freq_mhz=80.0))
        assert len(records) == 2

    def test_drop_warning_logs_but_doesnt_yield(
        self,
        ctx: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        lines = [
            "ITM channel 0: counter=0\n",
            "WARNING: SWV dropped 16 bytes (overflow)\n",
            "ITM channel 0: counter=1\n",
        ]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
                return_value=fake,
            ):
                records = list(client.tail_swo(freq_mhz=80.0))
        # 2 records (drop line filtered).
        assert len(records) == 2
        # WARNING logged.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings
        assert "SWV overflow" in warnings[0].message


class TestTailSwoCleanup:
    def test_terminates_subprocess_on_full_consume(
        self, ctx: SubstrateContext
    ) -> None:
        lines = ["ITM channel 0: hello\n"]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            list(client.tail_swo(freq_mhz=80.0))
        # finally block ran → terminate was called.
        fake.terminate.assert_called_once()

    def test_terminates_subprocess_on_break(
        self, ctx: SubstrateContext
    ) -> None:
        """Consumer breaks out of the for loop early — generator close
        runs finally → subprocess terminated."""
        lines = [
            "ITM channel 0: hello\n",
            "ITM channel 0: more\n",
            "ITM channel 0: more\n",
        ]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            gen = client.tail_swo(freq_mhz=80.0)
            next(gen)  # consume one record
            gen.close()  # explicit close — triggers finally
        fake.terminate.assert_called_once()

    def test_no_terminate_when_already_dead(
        self, ctx: SubstrateContext
    ) -> None:
        lines = ["ITM channel 0: hello\n"]
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        # Pretend the process exited on its own before cleanup.
        fake.poll = MagicMock(return_value=0)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            list(client.tail_swo(freq_mhz=80.0))
        # poll() returned non-None → _terminate_swo skipped terminate.
        fake.terminate.assert_not_called()


class TestTailSwoLogging:
    def test_info_on_start_and_stop(
        self,
        ctx: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
                return_value=fake,
            ):
                list(client.tail_swo(freq_mhz=80.0, port_number=1))
        msgs = [r.message for r in caplog.records]
        assert any("tail_swo starting" in m for m in msgs)
        assert any("tail_swo stopped" in m for m in msgs)


class TestTailSwoFixtureIntegration:
    def test_printf_port0_fixture(self, ctx: SubstrateContext) -> None:
        """Read the canonical printf-port-0 fixture, treat its text as
        the subprocess stdout, and verify the expected sequence of
        ITMRecords comes through."""
        lines = _swv("itm-printf-port0.txt").splitlines(keepends=True)
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            records = list(client.tail_swo(freq_mhz=80.0))
        payloads = [r.line for r in records]
        assert payloads == [
            "Hello, STM32!",
            "counter=0",
            "counter=1",
            "counter=2",
            "tick",
        ]
        assert all(r.port_number == 0 for r in records)

    def test_mixed_ports_fixture(self, ctx: SubstrateContext) -> None:
        lines = _swv("itm-mixed-ports.txt").splitlines(keepends=True)
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            records = list(client.tail_swo(freq_mhz=80.0))
        ports = [r.port_number for r in records]
        # Per fixture order: 0, 1, 0, 2, 1, 0.
        assert ports == [0, 1, 0, 2, 1, 0]

    def test_dropped_bytes_fixture(
        self,
        ctx: SubstrateContext,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        lines = _swv("dropped-bytes-marker.txt").splitlines(keepends=True)
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(lines)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
                return_value=fake,
            ):
                records = list(client.tail_swo(freq_mhz=80.0))
        # 4 counter lines (the WARNING is filtered).
        assert len(records) == 4
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings


# ---------------------------------------------------------------------------
# IMP-04 — SWV start failure must raise, not yield an empty stream
# ---------------------------------------------------------------------------


class TestTailSwoFailure:
    def test_cli_error_exit_raises_instead_of_empty_stream(
        self, ctx: SubstrateContext
    ) -> None:
        from embedagents.stm32.errors import CubeProgrammerError

        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(
            [
                "      -------------------------------------------------------------------\n",
                "                       STM32CubeProgrammer v2.22.0\n",
                "      -------------------------------------------------------------------\n",
                "Error: No debug probe detected.\n",
            ]
        )
        fake.wait = MagicMock(return_value=1)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                list(client.tail_swo(freq_mhz=80.0))
        # The CLI's Error: line reaches the message, not the void.
        assert "No debug probe detected" in (
            excinfo.value.message + (excinfo.value.tool_output or "")
        )

    def test_clean_exit_after_records_does_not_raise(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen(["[port 0] hello\n"])
        fake.wait = MagicMock(return_value=0)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ):
            records = list(client.tail_swo(freq_mhz=80.0))
        assert len(records) == 1

    def test_stderr_merged_into_stdout(self, ctx: SubstrateContext) -> None:
        # IMP-25 rider: a separate never-drained stderr PIPE can block
        # the child once the OS buffer fills.
        client = CubeProgrammer(ctx)
        fake = _make_fake_popen([])
        with patch(
            "embedagents.stm32.cubeprogrammer.client.subprocess.Popen",
            return_value=fake,
        ) as popen:
            list(client.tail_swo(freq_mhz=80.0))
        assert popen.call_args.kwargs["stderr"] == subprocess.STDOUT
