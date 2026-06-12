"""B6c tests — parse_hex_dump + read_memory (F-020) + read_flash_to_file (F-019)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.parsers import parse_hex_dump
from embedagents.stm32.cubeprogrammer.results import (
    Confirmation,
    MemoryReadResult,
)
from embedagents.stm32.subprocess_runner import ToolRunResult


HEX_DUMPS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "hex-dumps"
BANNERS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "banners"


def _hex(name: str) -> str:
    return (HEX_DUMPS / name).read_text(encoding="utf-8")


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _success(stdout: str = "") -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# parse_hex_dump — pure parser
# ---------------------------------------------------------------------------


class TestParseHexDumpBasics:
    def test_returns_memory_read_result(self) -> None:
        result = parse_hex_dump(_hex("ram-32bytes.txt"), address="0x20000000", size=32)
        assert isinstance(result, MemoryReadResult)
        assert result.address == "0x20000000"
        assert result.size == 32
        assert result.bytes_read == 32

    def test_canonical_render_has_16_per_row(self) -> None:
        result = parse_hex_dump(_hex("ram-32bytes.txt"), address="0x20000000", size=32)
        lines = result.hex_dump.rstrip("\n").splitlines()
        assert len(lines) == 2  # 32 bytes / 16 per row
        for line in lines:
            assert line.startswith("0x")
            assert "|" in line  # ASCII delimiter present

    def test_render_addresses_increment_by_16(self) -> None:
        result = parse_hex_dump(_hex("ram-32bytes.txt"), address="0x20000000", size=32)
        lines = result.hex_dump.rstrip("\n").splitlines()
        assert lines[0].startswith("0x20000000:")
        assert lines[1].startswith("0x20000010:")

    def test_render_hex_bytes_match_input(self) -> None:
        # ram-32bytes first row: 00 04 01 20 7D 0B 00 08 81 12 00 08 81 12 00 08
        result = parse_hex_dump(_hex("ram-32bytes.txt"), address="0x20000000", size=32)
        lines = result.hex_dump.rstrip("\n").splitlines()
        # canonical form lowercases hex; compare on whitespace-normalised tokens
        first_bytes = lines[0].split(":", 1)[1].split("|")[0].split()
        assert first_bytes == [
            "00", "04", "01", "20", "7d", "0b", "00", "08",
            "81", "12", "00", "08", "81", "12", "00", "08",
        ]

    def test_64_bytes_yields_4_rows(self) -> None:
        result = parse_hex_dump(_hex("flash-64bytes.txt"), address="0x08000000", size=64)
        lines = result.hex_dump.rstrip("\n").splitlines()
        assert len(lines) == 4
        assert result.bytes_read == 64


class TestParseHexDumpSuspiciousUnmapped:
    def test_all_ff_flagged(self) -> None:
        result = parse_hex_dump(
            _hex("unmapped-region-allff.txt"), address="0x080F0000", size=32
        )
        assert result.suspicious_unmapped is True

    def test_mixed_bytes_not_flagged(self) -> None:
        result = parse_hex_dump(
            _hex("ram-32bytes.txt"), address="0x20000000", size=32
        )
        assert result.suspicious_unmapped is False

    def test_empty_dump_not_flagged(self) -> None:
        result = parse_hex_dump("", address="0x20000000", size=32)
        assert result.suspicious_unmapped is False
        assert result.bytes_read == 0


class TestParseHexDumpAscii:
    def test_printable_bytes_rendered(self) -> None:
        """File starts with 'Hello, STM32!\\n' — visible in ASCII column."""
        result = parse_hex_dump(
            _hex("ascii-printable.txt"), address="0x20001000", size=32
        )
        first_line = result.hex_dump.splitlines()[0]
        assert "Hello, STM32!" in first_line

    def test_non_printable_rendered_as_dot(self) -> None:
        result = parse_hex_dump(
            _hex("ascii-printable.txt"), address="0x20001000", size=32
        )
        # Second row has 0x00 0x01 0x02 0x03 0x7F 0x80 0x81 0xFE ABCD...
        second_line = result.hex_dump.splitlines()[1]
        # Non-printables 0x00-0x03 + 0x7F + 0x80-0xFE all render as '.'
        ascii_col = second_line.split("|", 1)[1].rstrip("|")
        assert ascii_col[:4] == "...."
        assert ascii_col.endswith("ABCDEFGH")


class TestParseHexDumpV1Defaults:
    def test_sr_or_dr_warning_false_in_v1(self) -> None:
        """``sr_or_dr_warning`` depends on SVD lookup (ctx.svd_db, owned
        by C4 debug module). Always False until then."""
        result = parse_hex_dump(
            _hex("ram-32bytes.txt"), address="0x20000000", size=32
        )
        assert result.sr_or_dr_warning is False


# ---------------------------------------------------------------------------
# read_memory — F-020
# ---------------------------------------------------------------------------


class TestReadMemoryHappyPath:
    def test_invokes_r8_argv(self, ctx: SubstrateContext) -> None:
        """v2.22 dropped ``-rd``; substrate switched to ``-r8`` (byte read)
        which preserves the byte-granular ``size`` semantics."""
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_hex("ram-32bytes.txt")),
        ) as mocked:
            result = client.read_memory("0x20000000", size=32)
        argv = mocked.call_args[0][1]
        assert argv == ["-c", "port=swd", "-r8", "0x20000000", "32"]
        assert isinstance(result, MemoryReadResult)
        assert result.bytes_read == 32

    def test_default_size_256(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_hex("ram-32bytes.txt")),
        ) as mocked:
            client.read_memory("0x20000000")
        argv = mocked.call_args[0][1]
        assert "256" in argv

    def test_atomic_timeout(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(_hex("ram-32bytes.txt")),
        ) as mocked:
            client.read_memory("0x20000000", size=32)
        assert mocked.call_args.kwargs["timeout_s"] == 30.0


class TestReadMemoryValidation:
    def test_invalid_address_rejected(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client.read_memory("not-hex")

    def test_negative_size_rejected(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="size must be positive"):
            client.read_memory("0x20000000", size=-1)

    def test_zero_size_rejected(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="size must be positive"):
            client.read_memory("0x20000000", size=0)


class TestReadMemoryUnmappedWarning:
    def test_unmapped_region_logs_warning(
        self, ctx: SubstrateContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        client = CubeProgrammer(ctx)
        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.cubeprogrammer"):
            with patch(
                "embedagents.stm32.cubeprogrammer.client.run_tool",
                return_value=_success(_hex("unmapped-region-allff.txt")),
            ):
                result = client.read_memory("0x080F0000", size=32)
        assert result.suspicious_unmapped is True
        msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("unmapped" in m for m in msgs)


# ---------------------------------------------------------------------------
# read_flash_to_file — F-019
# ---------------------------------------------------------------------------


class TestReadFlashToFileExplicit:
    def test_with_explicit_args(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        output = tmp_path / "dump.bin"
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.read_flash_to_file(
                address="0x08000000", size=1024, output_path=output
            )
        argv = mocked.call_args[0][1]
        assert argv == [
            "-c",
            "port=swd",
            # v2.22 uses ``-u`` / ``--upload`` for device→file dumps; the
            # earlier ``-r32 addr size file`` form did not accept a file
            # path (output went to stdout instead).
            "-u",
            "0x08000000",
            "1024",
            str(output),
        ]
        assert isinstance(result, Confirmation)
        assert result.operation == "read_flash_to_file"
        assert result.data["bytes_read"] == 1024
        assert result.data["address"] == "0x08000000"
        assert result.data["output_path"] == str(output)

    def test_invalid_address_rejected(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client.read_flash_to_file(
                address="not-hex", size=1024, output_path=tmp_path / "x.bin"
            )

    def test_zero_size_rejected(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="size must be positive"):
            client.read_flash_to_file(
                address="0x08000000", size=0, output_path=tmp_path / "x.bin"
            )

    def test_output_directory_created(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        output = tmp_path / "subdir" / "deep" / "dump.bin"
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ):
            client.read_flash_to_file(
                address="0x08000000", size=64, output_path=output
            )
        assert output.parent.is_dir()


class TestReadFlashToFileDefaults:
    def test_defaults_pull_from_banner(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """No explicit kwargs → one connect() for banner → universal
        flash start + full size + auto-named output_path."""
        banner_stdout = (BANNERS / "nucleo-l476rg-good.txt").read_text()
        client = CubeProgrammer(ctx)
        call_log: list[list[str]] = []

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            call_log.append(list(args))
            if "-u" in args:
                return _success()
            return _success(banner_stdout)

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            result = client.read_flash_to_file()

        # First call: banner connect; second call: -r32 read.
        assert len(call_log) == 2
        read_args = call_log[1]
        # Universal STM32 flash start
        assert "0x08000000" in read_args
        # 1024 KB banner * 1024 = 1048576 bytes
        assert "1048576" in read_args
        # Output path auto-named under ctx.cwd
        output_str = result.data["output_path"]
        assert output_str.endswith(".bin")
        assert "STM32L47xxx_L48xxx" in output_str  # `/` sanitised to `_`

    def test_partial_defaults(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """address explicit, size + output_path default → still uses banner."""
        banner_stdout = (BANNERS / "nucleo-l476rg-good.txt").read_text()
        client = CubeProgrammer(ctx)

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            if "-u" in args:
                return _success()
            return _success(banner_stdout)

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            result = client.read_flash_to_file(address="0x08010000")
        assert result.data["address"] == "0x08010000"
        assert result.data["size"] == 1048576  # full flash from banner


class TestReadFlashToFileTimeoutScaling:
    def test_large_read_extends_timeout(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            client.read_flash_to_file(
                address="0x08000000",
                size=2 * 1024 * 1024,  # 2 MB
                output_path=tmp_path / "dump.bin",
            )
        # base 60 + 2 MB * 10 s/MB = 80s
        assert mocked.call_args.kwargs["timeout_s"] == pytest.approx(80.0, abs=0.1)
