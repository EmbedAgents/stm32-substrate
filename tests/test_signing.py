"""C2 tests — signing module (F-013)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.errors import (
    ConfigurationError,
    SigningToolError,
    ToolError,
)
from embedagents.stm32.signing import SigningResult, SigningTool
from embedagents.stm32.subprocess_runner import ToolRunResult


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_SigningTool_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_SIGNING_TOOL_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def input_bin(tmp_path: Path) -> Path:
    p = tmp_path / "app.bin"
    p.write_bytes(b"\x00" * 4096)
    return p


def _success() -> ToolRunResult:
    return ToolRunResult(
        exit_code=0,
        stdout="Signing complete.\n",
        stderr="",
        duration_s=0.5,
        timed_out=False,
    )


# ---------------------------------------------------------------------------
# Configuration / construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_construct_with_resolved_cli(self, ctx: SubstrateContext) -> None:
        tool = SigningTool(ctx)
        assert tool._cli is not None
        assert tool._log.name == "embedagents.stm32.signing"

    def test_unresolved_cli_loud_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SIGNING_TOOL_CLI", raising=False)
        monkeypatch.setenv("PATH", "")
        isolated = SubstrateContext.from_environment(project_path=tmp_path)
        tool = SigningTool(isolated)
        with pytest.raises(ConfigurationError, match="STM32_SigningTool_CLI"):
            tool._require_cli()


# ---------------------------------------------------------------------------
# Input file validation
# ---------------------------------------------------------------------------


class TestInputFile:
    def test_missing_input_raises_typed(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(SigningToolError) as excinfo:
            tool.sign_binary(
                tmp_path / "missing.bin",
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
            )
        err = excinfo.value
        assert err.signing_marker == "input-file-not-found"
        assert err.recoverable is True


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


class TestAddressValidation:
    def test_bad_load_address_raises_value_error(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(ValueError, match="load_address"):
            tool.sign_binary(
                input_bin,
                load_address="0xZZZZZZ",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
            )

    def test_bad_entry_point_raises_value_error(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(ValueError, match="entry_point"):
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="bad",
            )

    def test_bad_option_flags_raises_value_error(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(ValueError, match="option_flags"):
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="copro",
                header_version="2.3",
                option_flags="not-hex",
            )


# ---------------------------------------------------------------------------
# entry_point conditional rule (RES-020)
# ---------------------------------------------------------------------------


class TestEntryPointConditional:
    def test_fsbl_without_entry_point_raises(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(ValueError, match="entry_point is required"):
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
            )

    def test_ssbl_empty_entry_point_raises(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(ValueError, match="entry_point is required"):
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="ssbl",
                header_version="2.3",
                entry_point="",
            )

    def test_copro_without_entry_point_succeeds(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            result = tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="copro",
                header_version="2",
            )
        # entry_point not in argv when omitted.
        argv = mocked.call_args[0][1]
        assert "-ep" not in argv
        assert result.entry_point is None


# ---------------------------------------------------------------------------
# --align auto-resolution (RES-020 Q3)
# ---------------------------------------------------------------------------


class TestAlignResolution:
    def test_auto_align_for_n6_hv23(
        self,
        ctx: SubstrateContext,
        input_bin: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tool = SigningTool(ctx)
        with caplog.at_level(logging.INFO, logger="embedagents.stm32.signing"):
            with patch(
                "embedagents.stm32.signing.client.run_tool",
                return_value=_success(),
            ) as mocked:
                result = tool.sign_binary(
                    input_bin,
                    load_address="0x70000000",
                    image_type="fsbl",
                    header_version="2.3",
                    entry_point="0x70000400",
                    device_family="STM32N657XX",
                )
        argv = mocked.call_args[0][1]
        assert "--align" in argv
        assert result.align_applied is True
        # INFO log emitted.
        assert any("--align auto-set" in r.message for r in caplog.records)

    def test_explicit_false_with_n6_hv23_raises(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with pytest.raises(SigningToolError) as excinfo:
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
                device_family="STM32N657XX",
                align=False,
            )
        assert excinfo.value.signing_marker == "align-required"

    def test_explicit_true_passes_through(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.2",  # not the auto-trigger combo
                entry_point="0x70000400",
                align=True,
            )
        argv = mocked.call_args[0][1]
        assert "--align" in argv

    def test_no_auto_align_without_family(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            result = tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
            )
        argv = mocked.call_args[0][1]
        assert "--align" not in argv
        assert result.align_applied is False

    def test_no_auto_align_for_non_n6_family(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
                device_family="STM32MP1",
            )
        argv = mocked.call_args[0][1]
        assert "--align" not in argv


# ---------------------------------------------------------------------------
# no_key warning
# ---------------------------------------------------------------------------


class TestNoKeyWarning:
    def test_no_key_logs_warning(
        self,
        ctx: SubstrateContext,
        input_bin: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tool = SigningTool(ctx)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.signing"):
            with patch(
                "embedagents.stm32.signing.client.run_tool",
                return_value=_success(),
            ):
                tool.sign_binary(
                    input_bin,
                    load_address="0x70000000",
                    image_type="fsbl",
                    header_version="2.3",
                    entry_point="0x70000400",
                    no_key=True,
                )
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("authentication disabled" in r.message for r in warnings)
        assert any("dev-only" in r.message for r in warnings)


# ---------------------------------------------------------------------------
# Output path handling
# ---------------------------------------------------------------------------


class TestOutputPath:
    def test_default_appends_trusted_suffix(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ):
            result = tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
            )
        # app.bin → app-trusted.bin in same dir
        assert result.output_path == input_bin.with_name("app-trusted.bin")

    def test_existing_output_refused(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        existing = input_bin.with_name("app-trusted.bin")
        existing.write_bytes(b"")
        tool = SigningTool(ctx)
        with pytest.raises(SigningToolError) as excinfo:
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
            )
        assert excinfo.value.signing_marker == "output-exists"

    def test_explicit_output_path(
        self, ctx: SubstrateContext, input_bin: Path, tmp_path: Path
    ) -> None:
        custom = tmp_path / "out" / "signed.bin"
        custom.parent.mkdir()
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            result = tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="copro",
                header_version="2",
                output_path=custom,
            )
        assert result.output_path == custom
        argv = mocked.call_args[0][1]
        assert str(custom) in argv


# ---------------------------------------------------------------------------
# Argv shape
# ---------------------------------------------------------------------------


class TestArgvShape:
    def test_canonical_argv(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
                option_flags="0x80000000",
                no_key=True,
                align=True,
            )
        argv = mocked.call_args[0][1]
        # Required pairs present.
        assert "-bin" in argv and str(input_bin) in argv
        assert "-la" in argv and "0x70000000" in argv
        assert "-t" in argv and "fsbl" in argv
        assert "-hv" in argv and "2.3" in argv
        # Optionals.
        assert "-ep" in argv and "0x70000400" in argv
        assert "-of" in argv and "0x80000000" in argv
        assert "-nk" in argv
        assert "--align" in argv
        # Output flag.
        assert "-o" in argv


# ---------------------------------------------------------------------------
# Subprocess failure → signing-cli-failed
# ---------------------------------------------------------------------------


class TestSubprocessFailure:
    def test_non_zero_exit_surfaces_as_signing_cli_failed(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        runner_err = ToolError(
            message="STM32_SigningTool_CLI exited with code 1",
            code=1,
            tool_output="Error: invalid header version\n",
        )
        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool", side_effect=runner_err
        ):
            with pytest.raises(SigningToolError) as excinfo:
                tool.sign_binary(
                    input_bin,
                    load_address="0x70000000",
                    image_type="fsbl",
                    header_version="2.3",
                    entry_point="0x70000400",
                )
        err = excinfo.value
        assert err.signing_marker == "signing-cli-failed"
        assert err.recoverable is False
        assert err.tool_output is not None and "Error" in err.tool_output
        assert err.hint is not None and "log_path" in err.hint


# ---------------------------------------------------------------------------
# Happy path result shape
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_result_shape(
        self, ctx: SubstrateContext, input_bin: Path
    ) -> None:
        # Create the expected output file so bytes_out > 0 (the real
        # signing tool would write it; we're mocking run_tool).
        output = input_bin.with_name("app-trusted.bin")

        def fake_run_tool(binary, args, **kw):
            # Simulate the tool writing the output.
            output.write_bytes(b"\x00" * 4112)  # input + 16-byte header
            return _success()

        tool = SigningTool(ctx)
        with patch(
            "embedagents.stm32.signing.client.run_tool",
            side_effect=fake_run_tool,
        ):
            result = tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
                entry_point="0x70000400",
                device_family="STM32N657XX",
            )
        assert isinstance(result, SigningResult)
        assert result.input_path == input_bin
        assert result.output_path == output
        assert result.bytes_in == 4096
        assert result.bytes_out == 4112
        assert result.image_type == "fsbl"
        assert result.header_version == "2.3"
        assert result.align_applied is True  # auto-set
        assert result.no_auth_flag is False
        assert result.device_family == "STM32N657XX"
        assert result.log_path.parent.is_dir()


# ---------------------------------------------------------------------------
# Custom timeout knob
# ---------------------------------------------------------------------------


class TestTimeoutKnob:
    def test_signing_timeout_s_honored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        input_bin: Path,
    ) -> None:
        """Wait — input_bin's tmp_path is different from the configured
        defaults dir. We need to re-construct ctx with both."""
        import json

        fake_cli = tmp_path / "STM32_SigningTool_CLI"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("STM32_SIGNING_TOOL_CLI", str(fake_cli))
        # input_bin already exists under tmp_path.
        defaults = {
            "version": 1,
            "signing": {"timeout_s": 120},
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
        tool = SigningTool(ctx2)
        with patch(
            "embedagents.stm32.signing.client.run_tool", return_value=_success()
        ) as mocked:
            tool.sign_binary(
                input_bin,
                load_address="0x70000000",
                image_type="copro",
                header_version="2",
            )
        assert mocked.call_args.kwargs["timeout_s"] == 120.0


# ---------------------------------------------------------------------------
# Security tripwire (S4: signing-key material is a named threat surface).
#
# sign_binary takes NO key/password/keystore parameter -- key material is
# provisioned out-of-band, so the vendor argv + logs structurally cannot
# carry a secret today. This guards that property: if a future signing
# parameter ever threads a credential into the argv or a log line, the flag
# whitelist + marker scan below fail loudly.
# ---------------------------------------------------------------------------


class TestSecretTripwire:
    # The complete UM2543 argv surface sign_binary may emit.
    _ALLOWED_FLAGS = {"-bin", "-la", "-t", "-hv", "-ep", "-of", "-nk", "--align", "-o"}
    # Markers that would never appear in a legitimate path/flag/address but
    # would in leaked key material -- safe to substring-scan.
    _CREDENTIAL_MARKERS = ("-----begin", "passphrase", "password", "secret=")

    def test_signing_argv_and_logs_carry_no_credentials(
        self,
        ctx: SubstrateContext,
        input_bin: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tool = SigningTool(ctx)
        with caplog.at_level(logging.DEBUG, logger="embedagents.stm32"):
            with patch(
                "embedagents.stm32.signing.client.run_tool",
                return_value=_success(),
            ) as mocked:
                tool.sign_binary(
                    input_bin,
                    load_address="0x70000000",
                    image_type="copro",
                    header_version="2",
                    no_key=True,
                )
        argv = mocked.call_args[0][1]
        flags = {tok for tok in argv if tok.startswith("-")}
        assert flags <= self._ALLOWED_FLAGS, (
            "unexpected flag(s) in signing argv (possible credential leak): "
            f"{sorted(flags - self._ALLOWED_FLAGS)}"
        )
        haystack = (
            "\n".join(argv)
            + "\n"
            + "\n".join(r.getMessage() for r in caplog.records)
        ).lower()
        for marker in self._CREDENTIAL_MARKERS:
            assert marker not in haystack, (
                f"credential marker {marker!r} leaked into signing argv/logs"
            )
