"""C2b tests — ``stm32 prog sign`` CLI subcommand.

Per ADR-002 §M1, signing routes through ``/stm32prog`` (intentional
exception: signing is a sub-step of programming, no standalone slash
command). Tests verify CLI argv parsing + dispatch to SigningTool."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stm32_substrate.cli import main
from stm32_substrate.signing.results import SigningResult


@pytest.fixture()
def ensure_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make both STM32_Programmer_CLI and SigningTool resolve so the
    dispatcher constructs cleanly."""
    for env_var, name in [
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
        ("STM32_SIGNING_TOOL_CLI", "STM32_SigningTool_CLI"),
    ]:
        binary = tmp_path / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        monkeypatch.setenv(env_var, str(binary))


@pytest.fixture()
def mock_signing(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    instance = MagicMock(name="SigningTool-instance")
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("stm32_substrate.signing.SigningTool", factory)
    monkeypatch.setattr("stm32_substrate.signing.client.SigningTool", factory, raising=False)
    # The handler imports SigningTool inside _cmd_sign, so patch the
    # import target inside the cli._prog module path. Since we used
    # `from stm32_substrate.signing import SigningTool` inside the
    # function, the lookup happens at call time and the package-level
    # patch above takes effect.
    return instance


def _result(input_path: Path, output_path: Path) -> SigningResult:
    return SigningResult(
        input_path=input_path,
        output_path=output_path,
        bytes_in=4096,
        bytes_out=4112,
        load_address="0x70000000",
        entry_point="0x70000400",
        image_type="fsbl",
        header_version="2.3",
        option_flags=None,
        no_auth_flag=False,
        align_applied=True,
        device_family="STM32N657XX",
        duration_s=0.5,
        log_path=Path("/tmp/sign.log"),
    )


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# Help discoverability
# ---------------------------------------------------------------------------


class TestHelpListsSign:
    def test_prog_help_includes_sign(
        self, ensure_cli, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            _run(["prog", "--help"], capsys)
        out = capsys.readouterr().out
        assert "sign" in out


# ---------------------------------------------------------------------------
# Argv parsing → SigningTool.sign_binary call
# ---------------------------------------------------------------------------


class TestArgvParsing:
    def test_minimal_required_args(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        mock_signing.sign_binary.return_value = _result(
            input_file, input_file.with_name("app-trusted.bin")
        )
        code, out, _ = _run(
            [
                "prog", "sign",
                str(input_file),
                "--la=0x70000000",
                "--type", "fsbl",
                "--hv", "2.3",
                "--ep=0x70000400",
            ],
            capsys,
        )
        assert code == 0
        call = mock_signing.sign_binary.call_args
        assert call.args[0] == input_file
        assert call.kwargs["load_address"] == "0x70000000"
        assert call.kwargs["image_type"] == "fsbl"
        assert call.kwargs["header_version"] == "2.3"
        assert call.kwargs["entry_point"] == "0x70000400"

    def test_full_args(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        output = tmp_path / "out" / "signed.bin"
        mock_signing.sign_binary.return_value = _result(input_file, output)
        _run(
            [
                "prog", "sign",
                str(input_file),
                "--la=0x70000000",
                "--type", "fsbl",
                "--hv", "2.3",
                "--ep=0x70000400",
                "--of=0x80000000",
                "--no-key",
                "--align",
                "-o", str(output),
                "--device-family", "STM32N657XX",
            ],
            capsys,
        )
        call = mock_signing.sign_binary.call_args
        assert call.kwargs["option_flags"] == "0x80000000"
        assert call.kwargs["no_key"] is True
        assert call.kwargs["align"] is True
        assert call.kwargs["output"] == output if False else True  # output kwarg name
        # Actually the SigningTool kwarg is output_path:
        assert call.kwargs.get("output_path") == output
        assert call.kwargs["device_family"] == "STM32N657XX"

    def test_no_align_routes_false(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        mock_signing.sign_binary.return_value = _result(
            input_file, input_file.with_name("app-trusted.bin")
        )
        _run(
            [
                "prog", "sign",
                str(input_file),
                "--la=0x70000000",
                "--type", "copro",
                "--hv", "2",
                "--no-align",
            ],
            capsys,
        )
        call = mock_signing.sign_binary.call_args
        assert call.kwargs["align"] is False

    def test_align_default_none(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        mock_signing.sign_binary.return_value = _result(
            input_file, input_file.with_name("app-trusted.bin")
        )
        _run(
            [
                "prog", "sign",
                str(input_file),
                "--la=0x70000000",
                "--type", "copro",
                "--hv", "2",
            ],
            capsys,
        )
        call = mock_signing.sign_binary.call_args
        assert call.kwargs["align"] is None

    def test_invalid_type_rejected_by_argparse(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        with pytest.raises(SystemExit):
            _run(
                [
                    "prog", "sign",
                    str(input_file),
                    "--la=0x70000000",
                    "--type", "bogus",
                    "--hv", "2.3",
                ],
                capsys,
            )

    def test_invalid_hv_rejected_by_argparse(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        with pytest.raises(SystemExit):
            _run(
                [
                    "prog", "sign",
                    str(input_file),
                    "--la=0x70000000",
                    "--type", "fsbl",
                    "--hv", "9.9",
                ],
                capsys,
            )

    def test_align_and_no_align_mutually_exclusive(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        with pytest.raises(SystemExit):
            _run(
                [
                    "prog", "sign",
                    str(input_file),
                    "--la=0x70000000",
                    "--type", "fsbl",
                    "--hv", "2.3",
                    "--ep=0x70000400",
                    "--align",
                    "--no-align",
                ],
                capsys,
            )


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_result_serialised_as_json(
        self,
        ensure_cli,
        mock_signing: MagicMock,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        input_file = tmp_path / "app.bin"
        output = input_file.with_name("app-trusted.bin")
        mock_signing.sign_binary.return_value = _result(input_file, output)
        code, out, _ = _run(
            [
                "prog", "sign",
                str(input_file),
                "--la=0x70000000",
                "--type", "fsbl",
                "--hv", "2.3",
                "--ep=0x70000400",
            ],
            capsys,
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["input_path"] == str(input_file)
        assert payload["output_path"] == str(output)
        assert payload["bytes_in"] == 4096
        assert payload["bytes_out"] == 4112
        assert payload["image_type"] == "fsbl"
        assert payload["header_version"] == "2.3"
        assert payload["align_applied"] is True
