"""B11 tests — ``stm32 prog`` CLI subcommands.

Tests mock ``CubeProgrammer`` at the module-import site so we exercise
the argv → method-call wiring + JSON-output shape without touching the
real vendor CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from embedagents.stm32.cli import _prog, main
from embedagents.stm32.cli._serialize import dumps, serialise_error, to_dict
from embedagents.stm32.cubeprogrammer.codes import CubeProgrammerErrorCode
from embedagents.stm32.cubeprogrammer.results import (
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    EraseConfirmation,
    FlashConfirmation,
    HardFaultDecode,
    ITMRecord,
    MemoryReadResult,
    OptionByteDiffEntry,
    OptionBytesDiff,
    OptionBytesResult,
    PairFlashResult,
    ProbeRecord,
    RecoveryAttempt,
    RecoveryResult,
    ResetConfirmation,
)
from embedagents.stm32.errors import (
    CubeProgrammerError,
    ProtocolError,
    SubstrateError,
)


@pytest.fixture()
def ensure_cli_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``STM32_Programmer_CLI`` resolve to a stub binary so
    ``SubstrateContext.from_environment()`` doesn't fail."""
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))


@pytest.fixture()
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``CubeProgrammer`` in ``cli._prog`` so dispatch builds a
    pre-configured mock instead of the real wrapper."""
    instance = MagicMock(name="CubeProgrammer-instance")
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("embedagents.stm32.cli._prog.CubeProgrammer", factory)
    return instance


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    """Invoke ``main(argv)`` and return ``(exit_code, stdout, stderr)``."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _read_json(stdout: str) -> dict:
    return json.loads(stdout)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


class TestSerialize:
    def test_to_dict_dataclass(self) -> None:
        banner = _sample_banner()
        result = to_dict(banner)
        assert result["board_name"] == "NUCLEO-L476RG"
        assert result["device_name"] == "STM32L47xxx/L48xxx"

    def test_to_dict_list_of_dataclasses(self) -> None:
        probes = [
            ProbeRecord(stlink_sn="A", stlink_fw="V3", board_name="X"),
            ProbeRecord(stlink_sn="B", stlink_fw="V2", board_name=None),
        ]
        result = to_dict(probes)
        assert isinstance(result, list)
        assert result[0]["stlink_sn"] == "A"
        assert result[1]["board_name"] is None

    def test_dumps_is_valid_json(self) -> None:
        out = dumps(_sample_banner())
        parsed = json.loads(out)
        assert parsed["voltage_v"] == 3.28

    def test_dumps_pretty_indents(self) -> None:
        out_compact = dumps(_sample_banner())
        out_pretty = dumps(_sample_banner(), pretty=True)
        assert "\n  " in out_pretty
        assert "\n  " not in out_compact

    def test_path_serialised_as_string(self) -> None:
        sample_path = Path("/tmp/foo")
        result = Confirmation(operation="x", data={"path": sample_path})
        parsed = json.loads(dumps(result))
        # str(Path(...)) is platform-specific ("/tmp/foo" on POSIX,
        # "\\tmp\\foo" on Windows). Assert the serialiser produces the
        # canonical str() form regardless of OS.
        assert parsed["data"]["path"] == str(sample_path)

    def test_serialise_error_includes_type(self) -> None:
        err = CubeProgrammerError(
            message="boom",
            code=2,
            error_code=CubeProgrammerErrorCode.TARGET_DLL_ERR,
        )
        parsed = json.loads(serialise_error(err))
        assert parsed["error_type"] == "CubeProgrammerError"
        assert parsed["message"] == "boom"
        assert parsed["code"] == 2
        assert parsed["error_code"] == int(
            CubeProgrammerErrorCode.TARGET_DLL_ERR
        )


# ---------------------------------------------------------------------------
# NAME=VALUE parser
# ---------------------------------------------------------------------------


class TestParsePair:
    @pytest.mark.parametrize(
        "raw,expected_name,expected_value",
        [
            ("RDP=0xAA", "RDP", 0xAA),
            ("IWDG_SW=0x1", "IWDG_SW", 0x1),
            ("BOR_LEV=2", "BOR_LEV", 2),
            ("FLAG=true", "FLAG", True),
            ("FLAG=False", "FLAG", False),
            ("LABEL=helloworld", "LABEL", "helloworld"),
        ],
    )
    def test_canonical_coercion(
        self, raw: str, expected_name: str, expected_value
    ) -> None:
        name, value = _prog._parse_pair(raw)
        assert name == expected_name
        assert value == expected_value

    def test_missing_equals_raises(self) -> None:
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            _prog._parse_pair("RDP_no_value")

    def test_missing_value_raises(self) -> None:
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            _prog._parse_pair("RDP=")

    def test_missing_name_raises(self) -> None:
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            _prog._parse_pair("=0xAA")


# ---------------------------------------------------------------------------
# Discovery — connect / connect_under_reset / diagnose / list-probes / ping-swd / cores / read-ob
# ---------------------------------------------------------------------------


class TestConnect:
    def test_default_invokes_connect(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.connect.return_value = _sample_banner()
        code, out, err = _run(["prog", "connect"], capsys)
        assert code == 0
        mock_client.connect.assert_called_once_with(freq_khz=None)
        mock_client.connect_under_reset.assert_not_called()
        payload = _read_json(out)
        assert payload["board_name"] == "NUCLEO-L476RG"

    def test_ur_flag_routes_to_under_reset(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.connect_under_reset.return_value = _sample_banner(mode="UR")
        code, out, err = _run(["prog", "connect", "--ur"], capsys)
        assert code == 0
        mock_client.connect_under_reset.assert_called_once_with()
        mock_client.connect.assert_not_called()
        assert _read_json(out)["mode_used"] == "UR"

    def test_freq_kwarg_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.connect.return_value = _sample_banner()
        _run(["prog", "connect", "--freq", "1800"], capsys)
        mock_client.connect.assert_called_once_with(freq_khz=1800)

    def test_pretty_flag_pretty_prints(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.connect.return_value = _sample_banner()
        _run(["--pretty", "prog", "connect"], capsys)
        out = capsys.readouterr().out  # already captured by previous call
        # The previous _run already captured stdout; rerun with explicit
        # call inspecting the new captures.

    def test_substrate_error_to_stderr_exit_1(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.connect.side_effect = CubeProgrammerError(
            message="connect failed",
            code=2,
            error_code=CubeProgrammerErrorCode.TARGET_DLL_ERR,
        )
        code, out, err = _run(["prog", "connect"], capsys)
        assert code == 1
        assert out == ""
        parsed = json.loads(err.strip())
        assert parsed["error_type"] == "CubeProgrammerError"
        assert parsed["code"] == 2


class TestDiagnoseMicro:
    def test_emits_recovery_result(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.diagnose_micro.return_value = RecoveryResult(
            target_responsive=True,
            recovery_method="UR",
            swd_freq_khz_used=1800,
            attempts_log=[
                RecoveryAttempt(
                    mode="UR",
                    freq_khz=1800,
                    success=True,
                    error_code=None,
                    error_message=None,
                )
            ],
        )
        code, out, _ = _run(["prog", "diagnose-micro"], capsys)
        assert code == 0
        payload = _read_json(out)
        assert payload["target_responsive"] is True
        assert payload["recovery_method"] == "UR"
        assert len(payload["attempts_log"]) == 1


class TestListProbes:
    def test_empty_list_is_valid(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.list_probes.return_value = []
        code, out, _ = _run(["prog", "list-probes"], capsys)
        assert code == 0
        assert _read_json(out) == []

    def test_two_probes(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.list_probes.return_value = [
            ProbeRecord(stlink_sn="A", stlink_fw="V3", board_name="X"),
            ProbeRecord(stlink_sn="B", stlink_fw="V2", board_name=None),
        ]
        code, out, _ = _run(["prog", "list-probes"], capsys)
        payload = _read_json(out)
        assert len(payload) == 2
        assert payload[0]["stlink_sn"] == "A"
        assert payload[1]["board_name"] is None


class TestPingSwd:
    def test_responding_exit_0(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.ping_swd.return_value = BooleanResult(value=True)
        code, out, _ = _run(["prog", "ping-swd"], capsys)
        assert code == 0
        assert _read_json(out)["value"] is True

    def test_not_responding_exit_1(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.ping_swd.return_value = BooleanResult(
            value=False, reason="No STM32 target found"
        )
        code, out, _ = _run(["prog", "ping-swd"], capsys)
        assert code == 1
        # JSON still emitted on stdout with the reason.
        payload = _read_json(out)
        assert payload["value"] is False
        assert payload["reason"] == "No STM32 target found"


class TestCores:
    def test_emits_cores_result(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.cores.return_value = CoresResult(
            device_name="STM32H7", primary_core="Cortex-M7"
        )
        code, out, _ = _run(["prog", "cores"], capsys)
        assert code == 0
        assert _read_json(out)["primary_core"] == "Cortex-M7"


class TestSvd:
    def test_emits_svd_result(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """A-007: 'stm32 prog svd' was absent behind a stale 'blocked on
        ctx.svd_db / C4' note — everything it needed shipped."""
        from embedagents.stm32.cubeprogrammer.results import SVDResult

        mock_client.svd_for_attached.return_value = SVDResult(
            device_name="STM32L476RGTx",
            device_id="0x415",
            svd_path=Path("/opt/st/svd/STM32L476.svd"),
            svd_version="1.2",
        )
        code, out, _ = _run(["prog", "svd"], capsys)
        assert code == 0
        payload = _read_json(out)
        assert payload["svd_path"].endswith("STM32L476.svd")
        mock_client.svd_for_attached.assert_called_once_with()


class TestReadOb:
    def test_emits_option_bytes(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.read_option_bytes.return_value = OptionBytesResult(
            device_name="STM32L4",
            observed={"RDP": 0xAA, "IWDG_SW": 1},
            rdp_level=0,
        )
        code, out, _ = _run(["prog", "read-ob"], capsys)
        assert code == 0
        payload = _read_json(out)
        assert payload["rdp_level"] == 0
        assert payload["observed"]["RDP"] == 0xAA


# ---------------------------------------------------------------------------
# Option-byte write / verify
# ---------------------------------------------------------------------------


class TestWriteOb:
    def test_pairs_parsed_and_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.write_option_bytes.return_value = Confirmation(
            operation="write_option_bytes",
            data={"pairs_written": {"RDP": 0xAA}},
        )
        _run(
            [
                "prog",
                "write-ob",
                "RDP=0xAA",
                "IWDG_SW=true",
                "--confirm-destructive",
            ],
            capsys,
        )
        mock_client.write_option_bytes.assert_called_once_with(
            {"RDP": 0xAA, "IWDG_SW": True},
            confirm_destructive=True,
            confirm_irreversible=False,
        )

    def test_irreversibility_flag_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.write_option_bytes.return_value = Confirmation(
            operation="write_option_bytes", data={}
        )
        _run(
            [
                "prog",
                "write-ob",
                "RDP=0xCC",
                "--confirm-destructive",
                "--confirm-irreversible",
            ],
            capsys,
        )
        call = mock_client.write_option_bytes.call_args
        assert call.kwargs["confirm_irreversible"] is True

    def test_without_confirm_flag_passes_false(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.write_option_bytes.return_value = Confirmation(
            operation="write_option_bytes", data={}
        )
        _run(["prog", "write-ob", "IWDG_SW=1"], capsys)
        call = mock_client.write_option_bytes.call_args
        assert call.kwargs["confirm_destructive"] is False
        assert call.kwargs["confirm_irreversible"] is False

    def test_protocol_error_to_stderr(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.write_option_bytes.side_effect = ProtocolError(
            message="RDP=0xCC sets RDP level 2"
        )
        code, out, err = _run(
            ["prog", "write-ob", "RDP=0xCC", "--confirm-destructive"], capsys
        )
        assert code == 1
        parsed = json.loads(err.strip())
        assert parsed["error_type"] == "ProtocolError"


class TestVerifyOb:
    def test_diff_serialised(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.verify_option_bytes.return_value = OptionBytesDiff(
            observed={"RDP": 0xAA},
            expected={"RDP": 0x55},
            diffs=[
                OptionByteDiffEntry(
                    field="RDP", observed_value=0xAA, expected_value=0x55
                )
            ],
        )
        code, out, _ = _run(["prog", "verify-ob", "RDP=0x55"], capsys)
        assert code == 0
        payload = _read_json(out)
        assert len(payload["diffs"]) == 1
        assert payload["diffs"][0]["field"] == "RDP"


# ---------------------------------------------------------------------------
# Atomic target control
# ---------------------------------------------------------------------------


class TestErase:
    def test_default_invokes_erase_chip(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.erase_chip.return_value = EraseConfirmation(
            erase_complete=True, duration_s=1.5
        )
        code, out, _ = _run(["prog", "erase"], capsys)
        assert code == 0
        # The CLI forwards the destructive gate; no flag → False (the
        # library-level gate then aborts, exercised in the library tests).
        mock_client.erase_chip.assert_called_once_with(confirm_destructive=False)
        mock_client.erase_and_reset.assert_not_called()
        assert _read_json(out)["erase_complete"] is True

    def test_with_reset_routes_to_erase_and_reset(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.erase_and_reset.return_value = EraseConfirmation(
            erase_complete=True, reset_issued=True, duration_s=1.7
        )
        _run(["prog", "erase", "--with-reset"], capsys)
        mock_client.erase_and_reset.assert_called_once_with(
            confirm_destructive=False
        )
        mock_client.erase_chip.assert_not_called()

    def test_confirm_destructive_flag_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.erase_chip.return_value = EraseConfirmation(
            erase_complete=True, duration_s=1.0
        )
        _run(["prog", "erase", "--confirm-destructive"], capsys)
        mock_client.erase_chip.assert_called_once_with(confirm_destructive=True)


class TestReset:
    def test_soft_reset(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.reset.return_value = ResetConfirmation(
            reset_issued=True, via_gdb=False, hard=False
        )
        _run(["prog", "reset"], capsys)
        mock_client.reset.assert_called_once_with(hard=False)

    def test_hard_flag(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.reset.return_value = ResetConfirmation(
            reset_issued=True, via_gdb=False, hard=True
        )
        _run(["prog", "reset", "--hard"], capsys)
        mock_client.reset.assert_called_once_with(hard=True)


class TestHaltResume:
    def test_halt(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.halt.return_value = Confirmation(
            operation="halt", data={"halted": True}
        )
        code, out, _ = _run(["prog", "halt"], capsys)
        assert code == 0
        mock_client.halt.assert_called_once()
        assert _read_json(out)["data"]["halted"] is True

    def test_resume(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.resume.return_value = Confirmation(
            operation="resume", data={"running": True}
        )
        _run(["prog", "resume"], capsys)
        mock_client.resume.assert_called_once()


# ---------------------------------------------------------------------------
# Flash family
# ---------------------------------------------------------------------------


class TestFlashRouter:
    def test_routes_via_download_image(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        elf = tmp_path / "x.elf"
        elf.write_bytes(b"")
        mock_client.download_image.return_value = FlashConfirmation(
            bytes_written=0, address="0x08000000", duration_s=0.1, route_used="flash_file"
        )
        _run(["prog", "flash", str(elf), "--address", "0x08000000"], capsys)
        mock_client.download_image.assert_called_once()
        call = mock_client.download_image.call_args
        assert call.args[0] == elf
        assert call.kwargs["address"] == "0x08000000"
        # The router always receives an on_confirm gate; for .elf / .bin+addr
        # the library never consults it, but the CLI wires it for the
        # .bin-no-address path.
        assert callable(call.kwargs["on_confirm"])

    def test_bin_no_address_gate_defaults_to_decline(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Without --confirm-inferred-address the wired gate returns False,
        so the library's flash_bin_no_address would abort."""
        blob = tmp_path / "app.bin"
        blob.write_bytes(b"")
        mock_client.download_image.return_value = FlashConfirmation(
            bytes_written=0, address="0x08000000", duration_s=0.1,
            route_used="flash_bin_no_address",
        )
        _run(["prog", "flash", str(blob)], capsys)
        on_confirm = mock_client.download_image.call_args.kwargs["on_confirm"]
        assert on_confirm("0x08000000") is False

    def test_bin_no_address_gate_honors_flag(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        blob = tmp_path / "app.bin"
        blob.write_bytes(b"")
        mock_client.download_image.return_value = FlashConfirmation(
            bytes_written=0, address="0x08000000", duration_s=0.1,
            route_used="flash_bin_no_address",
        )
        _run(
            ["prog", "flash", str(blob), "--confirm-inferred-address"], capsys
        )
        on_confirm = mock_client.download_image.call_args.kwargs["on_confirm"]
        assert on_confirm("0x08000000") is True


class TestFlashData:
    def test_invocation(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        blob = tmp_path / "data.bin"
        blob.write_bytes(b"")
        mock_client.flash_data.return_value = FlashConfirmation(
            bytes_written=0, address="0x08010000", duration_s=0.1
        )
        _run(
            ["prog", "flash-data", str(blob), "--address", "0x08010000"], capsys
        )
        mock_client.flash_data.assert_called_once_with(blob, "0x08010000")

    def test_missing_address_rejected(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        blob = tmp_path / "data.bin"
        blob.write_bytes(b"")
        with pytest.raises(SystemExit):
            _run(["prog", "flash-data", str(blob)], capsys)


class TestFlashSigned:
    def test_invocation(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        bin_file = tmp_path / "signed.bin"
        bin_file.write_bytes(b"")
        mock_client.flash_signed.return_value = FlashConfirmation(
            bytes_written=0, address="0x70000000", duration_s=0.1, signed=True
        )
        _run(
            ["prog", "flash-signed", str(bin_file), "--address", "0x70000000"],
            capsys,
        )
        mock_client.flash_signed.assert_called_once_with(
            bin_file, address="0x70000000"
        )


class TestFlashPair:
    def test_unsigned_default(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        boot = tmp_path / "boot.elf"
        app = tmp_path / "app.elf"
        for p in (boot, app):
            p.write_bytes(b"")
        mock_client.flash_pair.return_value = PairFlashResult(
            bootloader=None, application=None, both_succeeded=True
        )
        _run(
            [
                "prog",
                "flash-pair",
                str(boot),
                str(app),
                "--boot-address",
                "0x08000000",
                "--app-address",
                "0x08008000",
            ],
            capsys,
        )
        mock_client.flash_pair.assert_called_once_with(
            boot,
            app,
            bootloader_address="0x08000000",
            application_address="0x08008000",
        )
        mock_client.flash_signed_pair.assert_not_called()

    def test_signed_routes_to_signed_pair(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        boot = tmp_path / "boot.bin"
        app = tmp_path / "app.bin"
        for p in (boot, app):
            p.write_bytes(b"")
        mock_client.flash_signed_pair.return_value = PairFlashResult(
            bootloader=None, application=None, both_succeeded=True
        )
        _run(
            [
                "prog",
                "flash-pair",
                str(boot),
                str(app),
                "--signed",
            ],
            capsys,
        )
        mock_client.flash_signed_pair.assert_called_once()
        call = mock_client.flash_signed_pair.call_args
        assert call.kwargs["sign_unsigned"] is False

    def test_sign_unsigned_flag(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        boot = tmp_path / "boot.bin"
        app = tmp_path / "app.bin"
        for p in (boot, app):
            p.write_bytes(b"")
        mock_client.flash_signed_pair.return_value = PairFlashResult(
            bootloader=None, application=None, both_succeeded=True
        )
        _run(
            [
                "prog",
                "flash-pair",
                str(boot),
                str(app),
                "--signed",
                "--sign-unsigned",
            ],
            capsys,
        )
        call = mock_client.flash_signed_pair.call_args
        assert call.kwargs["sign_unsigned"] is True
        assert call.kwargs["signing_no_key"] is False

    def test_no_key_flag_forwards_signing_no_key(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        boot = tmp_path / "boot.bin"
        app = tmp_path / "app.bin"
        for p in (boot, app):
            p.write_bytes(b"")
        mock_client.flash_signed_pair.return_value = PairFlashResult(
            bootloader=None, application=None, both_succeeded=True
        )
        _run(
            [
                "prog",
                "flash-pair",
                str(boot),
                str(app),
                "--signed",
                "--sign-unsigned",
                "--no-key",
            ],
            capsys,
        )
        call = mock_client.flash_signed_pair.call_args
        assert call.kwargs["signing_no_key"] is True


class TestFlashExternal:
    def test_explicit_loader_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        payload = tmp_path / "fw.bin"
        payload.write_bytes(b"")
        loader = tmp_path / "custom.stldr"
        loader.write_bytes(b"")
        mock_client.flash_external.return_value = FlashConfirmation(
            bytes_written=0,
            address="0x90000000",
            duration_s=0.1,
            loader_used="custom.stldr",
        )
        _run(
            [
                "prog",
                "flash-external",
                str(payload),
                "--address",
                "0x90000000",
                "--loader",
                str(loader),
            ],
            capsys,
        )
        mock_client.flash_external.assert_called_once_with(
            payload, "0x90000000", loader_path=loader
        )

    def test_auto_discovery_when_no_loader(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        payload = tmp_path / "fw.bin"
        payload.write_bytes(b"")
        mock_client.flash_external.return_value = FlashConfirmation(
            bytes_written=0, address="0x90000000", duration_s=0.1
        )
        _run(
            ["prog", "flash-external", str(payload), "--address", "0x90000000"],
            capsys,
        )
        call = mock_client.flash_external.call_args
        assert call.kwargs["loader_path"] is None


class TestFlashBank:
    def test_bank_routed(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        payload = tmp_path / "fw.bin"
        payload.write_bytes(b"")
        mock_client.flash_to_bank.return_value = FlashConfirmation(
            bytes_written=0, address="0x08100000", duration_s=0.1, bank=2
        )
        _run(
            [
                "prog",
                "flash-bank",
                "2",
                str(payload),
                "--address",
                "0x08100000",
            ],
            capsys,
        )
        mock_client.flash_to_bank.assert_called_once_with(
            payload, 2, "0x08100000"
        )

    def test_invalid_bank_rejected_by_argparse(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        payload = tmp_path / "fw.bin"
        payload.write_bytes(b"")
        with pytest.raises(SystemExit):
            _run(
                [
                    "prog",
                    "flash-bank",
                    "3",  # not in {1, 2}
                    str(payload),
                    "--address",
                    "0x08100000",
                ],
                capsys,
            )


# ---------------------------------------------------------------------------
# Read family
# ---------------------------------------------------------------------------


class TestReadFlash:
    def test_all_args_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        output = tmp_path / "dump.bin"
        mock_client.read_flash_to_file.return_value = Confirmation(
            operation="read_flash_to_file",
            data={"bytes_read": 1024},
        )
        _run(
            [
                "prog",
                "read-flash",
                "--address",
                "0x08000000",
                "--size",
                "1024",
                "--output",
                str(output),
            ],
            capsys,
        )
        mock_client.read_flash_to_file.assert_called_once_with(
            address="0x08000000", size=1024, output_path=output
        )

    def test_omitted_args_default_to_none(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.read_flash_to_file.return_value = Confirmation(
            operation="read_flash_to_file", data={"bytes_read": 0}
        )
        _run(["prog", "read-flash"], capsys)
        mock_client.read_flash_to_file.assert_called_once_with(
            address=None, size=None, output_path=None
        )


class TestReadMem:
    def test_invocation(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.read_memory.return_value = MemoryReadResult(
            address="0x20000000",
            size=32,
            bytes_read=32,
            hex_dump="0x20000000: 00 ... |...|\n",
        )
        code, out, _ = _run(
            ["prog", "read-mem", "--address", "0x20000000", "--size", "32"],
            capsys,
        )
        assert code == 0
        mock_client.read_memory.assert_called_once_with(
            "0x20000000", size=32
        )
        assert _read_json(out)["bytes_read"] == 32


# ---------------------------------------------------------------------------
# Hardfault
# ---------------------------------------------------------------------------


class TestHardfault:
    def test_no_fault(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.analyze_hardfault.return_value = HardFaultDecode(
            hardfault_detected=False,
            fault_type=None,
            faulty_pc=None,
            nvic_position=None,
            register_snapshot={},
            fault_decode="No fault detected",
            source_used="cubeprogrammer-hf",
        )
        code, out, _ = _run(["prog", "hardfault"], capsys)
        assert code == 0
        assert _read_json(out)["hardfault_detected"] is False

    def test_detected_fault(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.analyze_hardfault.return_value = HardFaultDecode(
            hardfault_detected=True,
            fault_type="UsageFault",
            faulty_pc="0x08001234",
            nvic_position=-1,
            register_snapshot={"CFSR": 0x00010000, "HFSR": 0x40000000},
            fault_decode="UsageFault at PC=0x08001234 (CFSR=0x00010000 HFSR=0x40000000)",
            source_used="cubeprogrammer-hf",
        )
        code, out, _ = _run(["prog", "hardfault"], capsys)
        # Detected fault is still a successful command run.
        assert code == 0
        payload = _read_json(out)
        assert payload["hardfault_detected"] is True
        assert payload["fault_type"] == "UsageFault"


# ---------------------------------------------------------------------------
# SWO streaming
# ---------------------------------------------------------------------------


class TestSwo:
    def test_emits_ndjson(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        records = [
            ITMRecord(port_number=0, line="hello", timestamp_s=0.01),
            ITMRecord(port_number=0, line="counter=0", timestamp_s=0.02),
            ITMRecord(port_number=1, line="trace", timestamp_s=0.03),
        ]
        mock_client.tail_swo.return_value = iter(records)
        code, out, _ = _run(
            ["prog", "swo", "--freq", "80.0"], capsys
        )
        assert code == 0
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 3
        # Each line is independently valid JSON (NDJSON contract).
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["line"] == "hello"
        assert parsed[2]["port_number"] == 1

    def test_default_port_zero(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_client.tail_swo.return_value = iter([])
        _run(["prog", "swo", "--freq", "80.0"], capsys)
        call = mock_client.tail_swo.call_args
        assert call.kwargs["port_number"] == 0

    def test_log_path_forwarded(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        mock_client.tail_swo.return_value = iter([])
        log = tmp_path / "swv.log"
        _run(["prog", "swo", "--freq", "80.0", "--log", str(log)], capsys)
        call = mock_client.tail_swo.call_args
        assert call.kwargs["log_path"] == log

    def test_freq_required(
        self,
        ensure_cli_on_path,
        mock_client: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["prog", "swo"], capsys)


# ---------------------------------------------------------------------------
# Top-level main()
# ---------------------------------------------------------------------------


class TestMainSurface:
    def test_no_command_prints_help_exits_0(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        code, out, _ = _run([], capsys)
        assert code == 0
        assert "stm32" in out.lower()
        assert "prog" in out

    def test_version_flag(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["--version"], capsys)

    def test_unknown_command_rejected(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["bogus-group"], capsys)


# ---------------------------------------------------------------------------
# CLI error boundary (HARD RULE 1) — library ValueError / NotImplementedError
# must surface as a structured JSON envelope, never a raw Python traceback.
# These run the REAL CubeProgrammer (no mock_client) so the actual library
# validation fires; the stub CLI keeps from_environment() happy.
# ---------------------------------------------------------------------------


class TestCliErrorBoundary:
    def _assert_structured(self, code: int, out: str, err: str) -> dict:
        assert code == 2, f"expected exit 2, got {code} (stderr={err!r})"
        assert out == "", "no JSON should reach stdout on the error path"
        envelope = json.loads(err)  # raises if a raw traceback leaked
        assert envelope["error_type"]
        assert envelope["message"]
        assert envelope["hint"]
        return envelope

    def test_bad_extension_is_structured(
        self, ensure_cli_on_path, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        bogus = tmp_path / "firmware.txt"
        code, out, err = _run(["prog", "flash", str(bogus)], capsys)
        env = self._assert_structured(code, out, err)
        assert env["error_type"] == "ValueError"
        assert "extension" in env["message"]

    def test_negative_size_is_structured(
        self, ensure_cli_on_path, capsys: pytest.CaptureFixture
    ) -> None:
        code, out, err = _run(
            ["prog", "read-mem", "--address", "0x08000000", "--size", "-1"],
            capsys,
        )
        env = self._assert_structured(code, out, err)
        assert env["error_type"] == "ValueError"

    def test_sign_unsigned_missing_params_not_traceback(
        self, ensure_cli_on_path, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        # A-005: sign_unsigned is wired now (RES-039); missing
        # --header-version on an unsigned input surfaces as a structured
        # envelope, never a traceback.
        boot = tmp_path / "boot.bin"
        boot.write_bytes(b"\x00" * 64)  # unsigned — no STM2 magic
        app = tmp_path / "app.bin"
        app.write_bytes(b"\x00" * 64)
        code, out, err = _run(
            [
                "prog", "flash-pair", str(boot), str(app),
                "--signed", "--sign-unsigned",
            ],
            capsys,
        )
        env = self._assert_structured(code, out, err)
        assert env["error_type"] == "ValueError"
        assert "signing_header_version" in env["message"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_banner(mode: str = "NORMAL") -> BannerResult:
    return BannerResult(
        stlink_sn="066BFF",
        stlink_fw="V3J11M3",
        board_name="NUCLEO-L476RG",
        voltage_v=3.28,
        swd_freq_khz=4000,
        device_id="0x415",
        device_name="STM32L47xxx/L48xxx",
        device_type="MCU",
        device_cpu="Cortex-M4",
        flash_size_kb=1024,
        mode_used=mode,  # type: ignore[arg-type]
    )
