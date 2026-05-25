"""B3 tests — parse_error stderr mapping + CubeProgrammer.connect()."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.codes import CubeProgrammerErrorCode
from stm32_substrate.cubeprogrammer.parsers import parse_error
from stm32_substrate.errors import (
    ConfigurationError,
    CubeProgrammerError,
    ToolError,
)
from stm32_substrate.subprocess_runner import ToolRunResult


FIXTURE_BANNERS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "banners"
FIXTURE_ERRORS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "errors"


def _banner(name: str) -> str:
    return (FIXTURE_BANNERS / name).read_text(encoding="utf-8")


def _stderr(name: str) -> str:
    return (FIXTURE_ERRORS / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_error: stderr → CubeProgrammerError mapping
# ---------------------------------------------------------------------------


class TestParseError:
    @pytest.mark.parametrize(
        "fixture,expected_code,expected_recoverable",
        [
            ("target-dll-err.txt", CubeProgrammerErrorCode.TARGET_DLL_ERR, False),
            ("target-no-device.txt", CubeProgrammerErrorCode.TARGET_NO_DEVICE, True),
            ("target-unknown-mcu.txt", CubeProgrammerErrorCode.TARGET_UNKNOWN_MCU_TARGET, True),
            ("target-firmware-old.txt", CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD, False),
            ("target-held-reset.txt", CubeProgrammerErrorCode.TARGET_HELD_UNDER_RESET, True),
            ("target-not-halted.txt", CubeProgrammerErrorCode.TARGET_NOT_HALTED, True),
            ("target-stlink-select-req.txt", CubeProgrammerErrorCode.TARGET_STLINK_SELECT_REQ, False),
            ("target-stlink-serial-not-found.txt", CubeProgrammerErrorCode.TARGET_STLINK_SERIAL_NOT_FOUND, False),
            ("flash-protected-rdp.txt", CubeProgrammerErrorCode.TARGET_CMD_ERR, False),
            ("usb-comm-err.txt", CubeProgrammerErrorCode.TARGET_USB_COMM_ERR, False),
        ],
    )
    def test_known_pattern_maps_to_code(
        self,
        fixture: str,
        expected_code: CubeProgrammerErrorCode,
        expected_recoverable: bool,
    ) -> None:
        err = parse_error(_stderr(fixture), exit_code=1)
        assert err.error_code == expected_code
        assert err.recoverable is expected_recoverable
        # Exit code is preserved alongside the parsed enum.
        assert err.code == 1
        # Hint is present for every known code.
        assert err.hint is not None
        assert err.tool_output == _stderr(fixture)

    def test_unmapped_falls_back_to_none(self) -> None:
        err = parse_error(_stderr("unmapped-error.txt"), exit_code=42)
        assert err.error_code is None
        assert err.recoverable is False
        assert err.code == 42
        assert err.hint is None

    def test_unmapped_message_uses_error_line(self) -> None:
        err = parse_error(_stderr("unmapped-error.txt"), exit_code=42)
        # The first Error: line becomes the message.
        assert err.message.startswith("Error:")

    def test_completely_empty_stderr(self) -> None:
        err = parse_error("", exit_code=255)
        assert err.error_code is None
        assert err.code == 255
        # Falls back to a generic message when there's no Error: line.
        assert "STM32_Programmer_CLI" in err.message

    def test_ansi_escapes_tolerated(self) -> None:
        ansi_wrapped = f"\x1b[31m{_stderr('target-dll-err.txt')}\x1b[0m"
        err = parse_error(ansi_wrapped, exit_code=2)
        assert err.error_code == CubeProgrammerErrorCode.TARGET_DLL_ERR


# ---------------------------------------------------------------------------
# CubeProgrammer.connect() — happy + error paths via mocked run_tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx_with_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    """A context whose cube_programmer_cli resolves to a fake binary."""
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestConnectHappyPath:
    def test_returns_parsed_banner(self, ctx_with_cli: SubstrateContext) -> None:
        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-good.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ) as mocked:
            banner = client.connect()
        assert banner.board_name == "NUCLEO-L476RG"
        assert banner.device_name == "STM32L47xxx/L48xxx"
        assert banner.voltage_v == pytest.approx(3.28)
        assert banner.voltage_suspicious is False
        mocked.assert_called_once()

    def test_invokes_correct_argv(self, ctx_with_cli: SubstrateContext) -> None:
        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-good.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ) as mocked:
            client.connect()
        # Inspect call args.
        call_args, call_kwargs = mocked.call_args
        binary, args = call_args[0], call_args[1]
        assert "STM32_Programmer_CLI" in str(binary)
        assert args == ["-c", "port=swd"]
        assert call_kwargs["timeout_s"] == 30.0

    def test_passes_freq_khz(self, ctx_with_cli: SubstrateContext) -> None:
        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-good.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ) as mocked:
            client.connect(freq_khz=1800)
        _, args = mocked.call_args[0][0], mocked.call_args[0][1]
        assert "freq=1800" in args

    def test_passes_default_probe_sn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_cli = tmp_path / "STM32_Programmer_CLI"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
        monkeypatch.setenv("STM32_PROGRAMMER_DEFAULT_SN", "066BFFTESTSN")
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(ctx)

        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-good.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ) as mocked:
            client.connect()
        args = mocked.call_args[0][1]
        assert "sn=066BFFTESTSN" in args


class TestConnectErrorPaths:
    def test_no_probe_raises_target_dll_err(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        runner_err = ToolError(
            message="STM32_Programmer_CLI exited with code 2",
            code=2,
            tool_output=_stderr("target-dll-err.txt"),
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.connect()
        err = excinfo.value
        assert err.error_code == CubeProgrammerErrorCode.TARGET_DLL_ERR
        assert err.recoverable is False
        assert err.code == 2
        assert err.hint is not None

    def test_no_device_is_recoverable(self, ctx_with_cli: SubstrateContext) -> None:
        runner_err = ToolError(
            message="STM32_Programmer_CLI exited with code 4",
            code=4,
            tool_output=_stderr("target-no-device.txt"),
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.connect()
        err = excinfo.value
        assert err.error_code == CubeProgrammerErrorCode.TARGET_NO_DEVICE
        assert err.recoverable is True

    def test_unmapped_error_falls_back(
        self, ctx_with_cli: SubstrateContext
    ) -> None:
        runner_err = ToolError(
            message="STM32_Programmer_CLI exited with code 99",
            code=99,
            tool_output=_stderr("unmapped-error.txt"),
        )
        client = CubeProgrammer(ctx_with_cli)
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(CubeProgrammerError) as excinfo:
                client.connect()
        err = excinfo.value
        assert err.error_code is None
        assert err.code == 99


class TestConnectConfigurationError:
    def test_unresolved_cli_raises_loud_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Isolate from host PATH + env vars so the built-in fallback
        # cannot resolve STM32_Programmer_CLI.
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        monkeypatch.setenv("PATH", "")
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(ctx)
        with pytest.raises(ConfigurationError) as excinfo:
            client.connect()
        assert "STM32_Programmer_CLI" in excinfo.value.message


class TestConnectLogging:
    def test_info_on_success(
        self, ctx_with_cli: SubstrateContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-good.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.INFO, logger="stm32_substrate.cubeprogrammer"):
            with patch(
                "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
            ):
                client.connect()
        msgs = [r.message for r in caplog.records]
        assert any("connected" in m and "NUCLEO-L476RG" in m for m in msgs)

    def test_warning_on_suspicious_voltage(
        self, ctx_with_cli: SubstrateContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-suspicious-voltage.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        client = CubeProgrammer(ctx_with_cli)
        with caplog.at_level(logging.WARNING, logger="stm32_substrate.cubeprogrammer"):
            with patch(
                "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
            ):
                client.connect()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "expected a WARNING log for suspicious voltage"
        assert "voltage" in warnings[0].message.lower()


class TestRuntimeDefaultsHonored:
    def test_custom_connect_timeout_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_cli = tmp_path / "STM32_Programmer_CLI"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))

        import json

        defaults = {"version": 1, "programmer": {"connect_timeout_s": 90}}
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))

        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(ctx)

        result = ToolRunResult(
            exit_code=0,
            stdout=_banner("nucleo-l476rg-good.txt"),
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )
        with patch(
            "stm32_substrate.cubeprogrammer.client.run_tool", return_value=result
        ) as mocked:
            client.connect()
        assert mocked.call_args[1]["timeout_s"] == 90.0
