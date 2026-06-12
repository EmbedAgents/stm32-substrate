"""B6e tests — external_loader.py + CubeProgrammer.flash_external (F-010)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.external_loader import (
    discover_external_loader,
    extract_family_prefix,
)
from embedagents.stm32.cubeprogrammer.results import FlashConfirmation
from embedagents.stm32.errors import ConfigurationError
from embedagents.stm32.subprocess_runner import ToolRunResult


BANNERS = Path(__file__).resolve().parent / "fixtures" / "cubeprogrammer" / "banners"


def _banner(name: str) -> str:
    return (BANNERS / name).read_text(encoding="utf-8")


def _success(stdout: str = "") -> ToolRunResult:
    return ToolRunResult(
        exit_code=0, stdout=stdout, stderr="", duration_s=0.05, timed_out=False
    )


# ---------------------------------------------------------------------------
# extract_family_prefix
# ---------------------------------------------------------------------------


class TestExtractFamilyPrefix:
    @pytest.mark.parametrize(
        "device_name,expected",
        [
            ("STM32L47xxx/L48xxx", "STM32L4"),
            ("STM32L4x6", "STM32L4"),
            ("STM32H7Sx", "STM32H7S"),
            ("STM32H757", "STM32H7"),
            ("STM32N657", "STM32N6"),
            ("STM32U5", "STM32U5"),
            ("STM32F401xB/C", "STM32F4"),
            ("STM32G0B0xx", "STM32G0B"),  # matches family digit + optional letter
        ],
    )
    def test_canonical_families(self, device_name: str, expected: str) -> None:
        assert extract_family_prefix(device_name) == expected

    def test_unknown_falls_back_to_input(self) -> None:
        assert extract_family_prefix("XYZ") == "XYZ"

    def test_empty_input(self) -> None:
        assert extract_family_prefix("") == ""


# ---------------------------------------------------------------------------
# discover_external_loader
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_programmer(tmp_path: Path) -> Path:
    """Create a fake STM32_Programmer_CLI + ExternalLoader directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    loader_dir = bin_dir / "ExternalLoader"
    loader_dir.mkdir()
    return fake_cli


class TestDiscoverExternalLoader:
    def test_zero_match_returns_empty(self, fake_programmer: Path) -> None:
        # No .stldr files in the loader dir.
        result = discover_external_loader(
            programmer_path=fake_programmer, device_family="STM32L4"
        )
        assert result == []

    def test_single_match(self, fake_programmer: Path) -> None:
        loader_dir = fake_programmer.parent / "ExternalLoader"
        (loader_dir / "MX25LM51245G_STM32L4P5G-DK.stldr").write_bytes(b"")
        (loader_dir / "MX66UW1G45G_STM32H7S78-DK.stldr").write_bytes(b"")
        result = discover_external_loader(
            programmer_path=fake_programmer, device_family="STM32L4"
        )
        assert len(result) == 1
        assert result[0].name == "MX25LM51245G_STM32L4P5G-DK.stldr"

    def test_multiple_matches(self, fake_programmer: Path) -> None:
        loader_dir = fake_programmer.parent / "ExternalLoader"
        (loader_dir / "MX25LM51245G_STM32L4P5G-DK.stldr").write_bytes(b"")
        (loader_dir / "S25FL128_STM32L496-DK.stldr").write_bytes(b"")
        (loader_dir / "MX66UW1G45G_STM32H7S78-DK.stldr").write_bytes(b"")
        result = discover_external_loader(
            programmer_path=fake_programmer, device_family="STM32L4"
        )
        assert len(result) == 2
        names = {p.name for p in result}
        assert names == {
            "MX25LM51245G_STM32L4P5G-DK.stldr",
            "S25FL128_STM32L496-DK.stldr",
        }

    def test_case_insensitive(self, fake_programmer: Path) -> None:
        loader_dir = fake_programmer.parent / "ExternalLoader"
        (loader_dir / "MX25LM51245G_stm32l4p5g-DK.stldr").write_bytes(b"")
        result = discover_external_loader(
            programmer_path=fake_programmer, device_family="STM32L4"
        )
        assert len(result) == 1

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        """No ExternalLoader directory → empty (not error)."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_cli = bin_dir / "STM32_Programmer_CLI"
        fake_cli.write_text("")
        result = discover_external_loader(
            programmer_path=fake_cli, device_family="STM32L4"
        )
        assert result == []

    def test_explicit_override_returned(
        self, fake_programmer: Path, tmp_path: Path
    ) -> None:
        custom = tmp_path / "my-loader.stldr"
        custom.write_bytes(b"")
        result = discover_external_loader(
            programmer_path=fake_programmer,
            device_family="ignored",
            explicit=custom,
        )
        assert result == [custom]

    def test_explicit_missing_returns_empty(
        self, fake_programmer: Path, tmp_path: Path
    ) -> None:
        result = discover_external_loader(
            programmer_path=fake_programmer,
            device_family="ignored",
            explicit=tmp_path / "does-not-exist.stldr",
        )
        assert result == []

    def test_only_stldr_files_considered(self, fake_programmer: Path) -> None:
        loader_dir = fake_programmer.parent / "ExternalLoader"
        (loader_dir / "MX_STM32L4-DK.stldr").write_bytes(b"")
        (loader_dir / "MX_STM32L4-DK.txt").write_bytes(b"")
        (loader_dir / "STM32L4-readme.md").write_bytes(b"")
        result = discover_external_loader(
            programmer_path=fake_programmer, device_family="STM32L4"
        )
        names = {p.name for p in result}
        assert names == {"MX_STM32L4-DK.stldr"}


# ---------------------------------------------------------------------------
# flash_external integration
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx_with_loader_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SubstrateContext, Path]:
    """Build a context whose CLI lives next to an ExternalLoader/ dir."""
    bin_dir = tmp_path / "stcli" / "bin"
    bin_dir.mkdir(parents=True)
    fake_cli = bin_dir / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    loader_dir = bin_dir / "ExternalLoader"
    loader_dir.mkdir()
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    ctx = SubstrateContext.from_environment(project_path=tmp_path)
    return ctx, loader_dir


@pytest.fixture()
def payload(tmp_path: Path) -> Path:
    p = tmp_path / "external-firmware.bin"
    p.write_bytes(b"\x00" * 2048)
    return p


class TestFlashExternalExplicitLoader:
    def test_uses_explicit_path(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
        tmp_path: Path,
    ) -> None:
        ctx, _ = ctx_with_loader_dir
        loader = tmp_path / "custom.stldr"
        loader.write_bytes(b"")
        client = CubeProgrammer(ctx)
        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            return_value=_success(),
        ) as mocked:
            result = client.flash_external(
                payload, "0x90000000", loader_path=loader
            )
        argv = mocked.call_args[0][1]
        assert "-el" in argv
        assert str(loader) in argv
        assert "0x90000000" in argv
        assert isinstance(result, FlashConfirmation)
        assert result.loader_used == "custom.stldr"
        assert result.address == "0x90000000"

    def test_missing_explicit_path_raises(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
        tmp_path: Path,
    ) -> None:
        ctx, _ = ctx_with_loader_dir
        client = CubeProgrammer(ctx)
        with pytest.raises(ConfigurationError, match="does not exist"):
            client.flash_external(
                payload,
                "0x90000000",
                loader_path=tmp_path / "missing.stldr",
            )


class TestFlashExternalAutoDiscovery:
    def test_single_match_auto_picked(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
    ) -> None:
        ctx, loader_dir = ctx_with_loader_dir
        loader = loader_dir / "MX25LM51245G_STM32L4P5G-DK.stldr"
        loader.write_bytes(b"")

        client = CubeProgrammer(ctx)

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            if "-el" in args:
                return _success()
            return _success(_banner("nucleo-l476rg-good.txt"))

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            result = client.flash_external(payload, "0x90000000")
        assert result.loader_used == "MX25LM51245G_STM32L4P5G-DK.stldr"

    def test_zero_match_raises_loud(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
    ) -> None:
        ctx, _ = ctx_with_loader_dir  # No .stldr files
        client = CubeProgrammer(ctx)

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            return _success(_banner("nucleo-l476rg-good.txt"))

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            with pytest.raises(ConfigurationError, match="no external loader"):
                client.flash_external(payload, "0x90000000")

    def test_multi_match_without_callback_raises(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
    ) -> None:
        ctx, loader_dir = ctx_with_loader_dir
        (loader_dir / "MX25LM51245G_STM32L4P5G-DK.stldr").write_bytes(b"")
        (loader_dir / "S25FL128_STM32L496-DK.stldr").write_bytes(b"")
        client = CubeProgrammer(ctx)

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            return _success(_banner("nucleo-l476rg-good.txt"))

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            with pytest.raises(ConfigurationError, match="multiple external loaders"):
                client.flash_external(payload, "0x90000000")

    def test_multi_match_with_callback_routes(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
    ) -> None:
        ctx, loader_dir = ctx_with_loader_dir
        a = loader_dir / "MX25LM51245G_STM32L4P5G-DK.stldr"
        b = loader_dir / "S25FL128_STM32L496-DK.stldr"
        a.write_bytes(b"")
        b.write_bytes(b"")
        client = CubeProgrammer(ctx)
        captured: list[list[str]] = []

        def chooser(candidates: list[Path]) -> Path:
            captured.append([p.name for p in candidates])
            return b  # caller picks 'b'

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            if "-el" in args:
                return _success()
            return _success(_banner("nucleo-l476rg-good.txt"))

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            result = client.flash_external(
                payload, "0x90000000", on_loader_choice=chooser
            )
        assert len(captured) == 1
        assert set(captured[0]) == {a.name, b.name}
        assert result.loader_used == b.name

    def test_callback_returns_unknown_path_raises(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
        tmp_path: Path,
    ) -> None:
        ctx, loader_dir = ctx_with_loader_dir
        (loader_dir / "MX25LM51245G_STM32L4P5G-DK.stldr").write_bytes(b"")
        (loader_dir / "S25FL128_STM32L496-DK.stldr").write_bytes(b"")
        client = CubeProgrammer(ctx)

        def cheater(candidates: list[Path]) -> Path:
            return tmp_path / "not-in-candidates.stldr"

        def fake_run_tool(binary, args, **kw):  # type: ignore[no-untyped-def]
            return _success(_banner("nucleo-l476rg-good.txt"))

        with patch(
            "embedagents.stm32.cubeprogrammer.client.run_tool",
            side_effect=fake_run_tool,
        ):
            with pytest.raises(ValueError, match="not one of the discovered"):
                client.flash_external(
                    payload, "0x90000000", on_loader_choice=cheater
                )


class TestFlashExternalValidation:
    def test_invalid_address_rejected(
        self,
        ctx_with_loader_dir: tuple[SubstrateContext, Path],
        payload: Path,
        tmp_path: Path,
    ) -> None:
        ctx, _ = ctx_with_loader_dir
        loader = tmp_path / "custom.stldr"
        loader.write_bytes(b"")
        client = CubeProgrammer(ctx)
        with pytest.raises(ValueError, match="invalid flash address"):
            client.flash_external(payload, "not-hex", loader_path=loader)
