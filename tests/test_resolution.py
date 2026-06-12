"""resolution.py helpers (IMP-22 / IMP-23 / A-013) — str tolerance,
descriptor anchoring, loud-error shape — plus per-module regressions
pinning that public Path-typed entry points accept plain strings
(the str-vs-Path AttributeError family)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.errors import ConfigurationError
from embedagents.stm32.resolution import coerce_path, resolve_file


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    for env_var, name in (
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _ctx_with_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor: dict
) -> SubstrateContext:
    for env_var, name in (
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    (tmp_path / "stm32-project.jsonc").write_text(
        json.dumps({"version": 1, **descriptor})
    )
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestCoercePath:
    def test_str_becomes_resolved_path(self, tmp_path: Path) -> None:
        result = coerce_path(str(tmp_path / "fw.elf"))
        assert isinstance(result, Path)
        assert result == (tmp_path / "fw.elf").resolve()

    def test_path_passes_through(self, tmp_path: Path) -> None:
        assert coerce_path(tmp_path) == tmp_path.resolve()

    def test_relative_anchors_to_anchor(self, tmp_path: Path) -> None:
        assert coerce_path("sub/fw.elf", anchor=tmp_path) == (
            tmp_path / "sub" / "fw.elf"
        ).resolve()

    def test_absolute_ignores_anchor(self, tmp_path: Path) -> None:
        absolute = tmp_path / "fw.elf"
        assert coerce_path(absolute, anchor=Path("/elsewhere")) == absolute.resolve()


class TestResolveFile:
    def test_explicit_str_wins(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        result = resolve_file(
            str(tmp_path / "x.elf"),
            ctx=ctx,
            descriptor_field="debug.elf_path",
            arg_name="elf_path",
        )
        assert result == (tmp_path / "x.elf").resolve()

    def test_descriptor_relative_anchors_to_ctx_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IMP-23: a relative descriptor path reads against the project
        root (ctx.cwd), never the process CWD."""
        ctx = _ctx_with_descriptor(
            tmp_path, monkeypatch, {"debug": {"elf_path": "Debug/app.elf"}}
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)  # process CWD ≠ project root
        result = resolve_file(
            None, ctx=ctx, descriptor_field="debug.elf_path", arg_name="elf_path"
        )
        assert result == (tmp_path / "Debug" / "app.elf").resolve()

    def test_missing_raises_configuration_error_naming_field(
        self, ctx: SubstrateContext
    ) -> None:
        with pytest.raises(ConfigurationError, match="cubemx.ioc_path"):
            resolve_file(
                None, ctx=ctx, descriptor_field="cubemx.ioc_path", arg_name="ioc_path"
            )

    def test_optional_returns_none(self, ctx: SubstrateContext) -> None:
        assert (
            resolve_file(
                None,
                ctx=ctx,
                descriptor_field="cubemx.output_path",
                arg_name="output_path",
                required=False,
            )
            is None
        )


class TestStrToleranceAcrossModules:
    """A-013 generalization: the canonical stm32debug.md heredoc passes a
    plain string — every module's entry point must accept it."""

    def test_debug_resolve_elf_accepts_str(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        from embedagents.stm32.debug.client import Debug

        elf = tmp_path / "demo.elf"
        elf.write_bytes(b"")
        resolved = Debug(ctx)._resolve_elf(str(elf))
        assert resolved == elf.resolve()

    def test_cubemx_resolve_ioc_accepts_str(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        from embedagents.stm32.cubemx.client import CubeMX

        ioc = tmp_path / "demo.ioc"
        ioc.write_text("")
        assert CubeMX(ctx)._resolve_ioc_path(str(ioc)) == ioc.resolve()

    def test_flash_bin_accepts_str(self, ctx: SubstrateContext) -> None:
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        # Reaching the .bin extension check (not AttributeError) proves
        # the string was coerced before any Path-attribute access.
        with pytest.raises(ValueError, match=r"\.bin"):
            CubeProgrammer(ctx).flash_bin("firmware.elf", address="0x08000000")

    def test_cubeide_explicit_project_accepts_str(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        from embedagents.stm32.cubeide import CubeIDE

        # Nonexistent str path → the resolver's loud ConfigurationError,
        # not an AttributeError off the string.
        with pytest.raises(ConfigurationError, match="does not exist"):
            CubeIDE(ctx)._resolve_explicit_project(str(tmp_path / "nope"))

    def test_signing_accepts_str(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        from embedagents.stm32.errors import SigningToolError
        from embedagents.stm32.signing.client import SigningTool

        # Missing-file str input → the typed input-file-not-found error,
        # not an AttributeError.
        with pytest.raises(SigningToolError, match="not found"):
            SigningTool(ctx).sign_binary(
                str(tmp_path / "missing.bin"),
                load_address="0x70000000",
                image_type="fsbl",
                header_version="2.3",
            )
