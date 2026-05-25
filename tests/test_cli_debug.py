"""``stm32 debug`` CLI subcommand tests — recipe-flow per RES-026.

Tests mock ``Debug`` and its ``DebugSession`` return at the module
import site so we exercise the argv → composed-operation wiring + JSON
output shape without touching real subprocesses.

Layout mirrors ``test_cli_prog.py`` / ``test_cli_build.py``:

- A fixture sets ``STM32_PROGRAMMER_CLI`` to a stub so
  ``SubstrateContext.from_environment()`` succeeds during ``dispatch``.
- A ``mock_debug`` fixture patches ``stm32_substrate.cli._debug.Debug``
  to return a MagicMock whose ``start_session(...)`` yields a fully
  pre-configured ``DebugSession`` substitute.
- One test class per subcommand validates argv parsing, recipe
  composition order, kwargs, and JSON output shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stm32_substrate.cli import main
from stm32_substrate.debug.results import (
    Breakpoint,
    CallStack,
    ComparisonResult,
    DebugSnapshot,
    MemoryReadResult,
    PeripheralDump,
    RegisterDump,
    RegisterValue,
    RunResult,
    SessionHandle,
    StackFrame,
)
from stm32_substrate.errors import GDBError, SubstrateError


@pytest.fixture()
def ensure_cli_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``STM32_Programmer_CLI`` resolve to a stub so
    ``SubstrateContext.from_environment()`` doesn't fail."""
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))


def _make_session_mock() -> MagicMock:
    """Build a DebugSession-shaped mock with sensible default returns."""
    session = MagicMock(name="DebugSession-instance")
    session.target_halted = True
    session.session_handle.return_value = SessionHandle(
        gdbserver_pid=11111,
        gdb_pid=22222,
        gdb_port=61234,
        target_halted=True,
        target_state="halted",
        elf_path=Path("/tmp/demo.elf"),
        n6_dev_mode_confirmed=False,
    )
    session.set_breakpoint.return_value = Breakpoint(
        number=1, location="main", address="0x08000400",
        file="main.c", line=42,
    )
    session.run_until_breakpoint.return_value = RunResult(
        breakpoint_hit=True,
        breakpoint=session.set_breakpoint.return_value,
        target_halted=True,
        halt_reason="breakpoint",
        duration_s=0.1,
    )
    session.compare_variable.return_value = ComparisonResult(
        name="foo", observed=0, expected=0, mask=None, matches=True,
    )
    session.compare_register.return_value = ComparisonResult(
        name="r0", observed=0, expected=0, mask=None, matches=True,
    )
    session.read_registers.return_value = RegisterDump(
        values={"pc": 0x08000400, "sp": 0x20000400, "lr": 0x08000800},
        fpu_present=False, secure_world=None,
    )
    session.read_peripheral.return_value = PeripheralDump(
        peripheral="RCC", instance="RCC", base_address="0x40021000",
        registers={}, raw_bytes=None, suspicious_unmapped=False,
    )
    session.read_memory.return_value = MemoryReadResult(
        address="0x20000000", size=16, bytes_read=16,
        hex_dump="00 11 22 33 ...", raw_bytes=None, suspicious_unmapped=False,
    )
    session.callstack.return_value = CallStack(
        frames=[StackFrame(level=0, pc="0x08000400", function="main", file="main.c", line=42)],
        threads=[], active_thread_index=0,
    )
    return session


@pytest.fixture()
def mock_debug(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``Debug`` in ``cli._debug`` to return a mocked instance.

    The mocked instance's ``start_session(...)`` returns a DebugSession
    substitute carrying default return values for every recipe primitive.
    """
    session = _make_session_mock()
    instance = MagicMock(name="Debug-instance")
    instance.start_session.return_value = session
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("stm32_substrate.cli._debug.Debug", factory)
    # Stash the session on the factory mock so tests can read it.
    factory._session = session
    factory._instance = instance
    return factory


def _run(argv: list[str], capsys: pytest.CaptureFixture) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _read_json(stdout: str) -> dict:
    return json.loads(stdout)


# ---------------------------------------------------------------------------
# `start` (lifecycle) — unchanged from pre-RES-026
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_default_halt(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_bytes(b"\x7fELF")
        code, out, _ = _run(["debug", "start", str(elf)], capsys)
        assert code == 0
        mock_debug._instance.start_session.assert_called_once()
        kwargs = mock_debug._instance.start_session.call_args.kwargs
        assert kwargs["halt"] is True
        assert kwargs["n6_dev_mode"] is False
        body = _read_json(out)
        assert body["gdb_port"] == 61234
        mock_debug._session.close.assert_called_once()

    def test_start_no_halt_passes_halt_false(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_bytes(b"\x7fELF")
        code, _, _ = _run(["debug", "start", str(elf), "--no-halt"], capsys)
        assert code == 0
        assert mock_debug._instance.start_session.call_args.kwargs["halt"] is False


# ---------------------------------------------------------------------------
# `svd-path` — pure lookup, no subprocess
# ---------------------------------------------------------------------------


class TestSvdPath:
    def test_svd_path_resolves_via_ctx(
        self, ensure_cli_on_path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        svd_path = tmp_path / "STM32L476.svd"
        svd_path.write_text("<svd/>")
        fake_svd_db = MagicMock(name="SvdDb")
        fake_svd_db.find_for.return_value = svd_path
        fake_svd_db.roots.configured.return_value = ["cubeide", "cube_programmer"]

        from stm32_substrate.context import SubstrateContext
        original_from_env = SubstrateContext.from_environment

        def patched(*args, **kwargs):
            ctx = original_from_env(*args, **kwargs)
            object.__setattr__(ctx, "svd_db", fake_svd_db)
            return ctx

        monkeypatch.setattr(SubstrateContext, "from_environment", staticmethod(patched))
        code, out, _ = _run(["debug", "svd-path", "STM32L476RG"], capsys)
        assert code == 0
        body = _read_json(out)
        assert body["device_name"] == "STM32L476RG"
        assert body["svd_path"].endswith("STM32L476.svd")
        assert "cubeide" in body["configured_sources"]


# ---------------------------------------------------------------------------
# `check-variable` (DBG-004)
# ---------------------------------------------------------------------------


class TestCheckVariable:
    def test_recipe_calls_set_run_compare(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, out, _ = _run(
            [
                "debug", "check-variable",
                "--at", "main",
                "--var", "uart_buf_count",
                "--expected", "0",
            ],
            capsys,
        )
        assert code == 0
        session = mock_debug._session
        session.set_breakpoint.assert_called_once_with("main")
        session.run_until_breakpoint.assert_called_once()
        session.compare_variable.assert_called_once_with(
            "uart_buf_count", 0, mask=None,
        )
        session.close.assert_called_once()
        body = _read_json(out)
        assert body["matches"] is True

    def test_expected_string_passthrough(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, _, _ = _run(
            [
                "debug", "check-variable",
                "--at", "process_packet",
                "--var", "state",
                "--expected", "READY",
            ],
            capsys,
        )
        assert code == 0
        mock_debug._session.compare_variable.assert_called_once_with(
            "state", "READY", mask=None,
        )

    def test_mask_parsed_as_int(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, _, _ = _run(
            [
                "debug", "check-variable",
                "--at", "main",
                "--var", "flags",
                "--expected", "0x1",
                "--mask", "0xFF",
            ],
            capsys,
        )
        assert code == 0
        mock_debug._session.compare_variable.assert_called_once_with(
            "flags", 1, mask=0xFF,
        )

    def test_breakpoint_not_hit_raises_gdb_error(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_debug._session.run_until_breakpoint.return_value = RunResult(
            breakpoint_hit=False, breakpoint=None,
            target_halted=False, halt_reason="timeout", duration_s=30.0,
        )
        code, _, err = _run(
            [
                "debug", "check-variable",
                "--at", "unreachable",
                "--var", "x",
                "--expected", "0",
            ],
            capsys,
        )
        assert code == 1
        body = json.loads(err)
        assert body["gdb_marker"] == "breakpoint-not-hit"
        mock_debug._session.compare_variable.assert_not_called()
        mock_debug._session.close.assert_called_once()


# ---------------------------------------------------------------------------
# `check-register` (DBG-005)
# ---------------------------------------------------------------------------


class TestCheckRegister:
    def test_recipe_calls_set_run_compare(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, _, _ = _run(
            [
                "debug", "check-register",
                "--at", "SystemClock_Config",
                "--reg", "r0",
                "--expected", "0x1",
                "--mask", "0xF",
            ],
            capsys,
        )
        assert code == 0
        session = mock_debug._session
        session.set_breakpoint.assert_called_once_with("SystemClock_Config")
        session.run_until_breakpoint.assert_called_once()
        session.compare_register.assert_called_once_with("r0", 1, mask=0xF)


# ---------------------------------------------------------------------------
# `read-registers` (DBG-006)
# ---------------------------------------------------------------------------


class TestReadRegisters:
    def test_recipe_calls_read_registers(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, out, _ = _run(["debug", "read-registers"], capsys)
        assert code == 0
        mock_debug._session.read_registers.assert_called_once_with()
        body = _read_json(out)
        assert body["values"]["pc"] == 0x08000400
        assert body["fpu_present"] is False


# ---------------------------------------------------------------------------
# `read-peripheral` (DBG-007)
# ---------------------------------------------------------------------------


class TestReadPeripheral:
    def test_recipe_calls_with_name(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, out, _ = _run(["debug", "read-peripheral", "RCC"], capsys)
        assert code == 0
        mock_debug._session.read_peripheral.assert_called_once_with("RCC", None)
        body = _read_json(out)
        assert body["peripheral"] == "RCC"

    def test_recipe_with_instance(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, _, _ = _run(
            ["debug", "read-peripheral", "USART", "USART1"], capsys
        )
        assert code == 0
        mock_debug._session.read_peripheral.assert_called_once_with("USART", "USART1")


# ---------------------------------------------------------------------------
# `read-memory`
# ---------------------------------------------------------------------------


class TestReadMemory:
    def test_recipe_calls_with_address_and_size(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, out, _ = _run(
            ["debug", "read-memory", "--address", "0x20000000", "--size", "16"],
            capsys,
        )
        assert code == 0
        mock_debug._session.read_memory.assert_called_once_with("0x20000000", 16)
        body = _read_json(out)
        assert body["size"] == 16


# ---------------------------------------------------------------------------
# `callstack`
# ---------------------------------------------------------------------------


class TestCallstack:
    def test_default_no_full(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, out, _ = _run(["debug", "callstack"], capsys)
        assert code == 0
        mock_debug._session.callstack.assert_called_once_with(full=False)
        body = _read_json(out)
        assert body["frames"][0]["function"] == "main"

    def test_full_passes_through(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        code, _, _ = _run(["debug", "callstack", "--full"], capsys)
        assert code == 0
        mock_debug._session.callstack.assert_called_once_with(full=True)


# ---------------------------------------------------------------------------
# `snapshot` (DIAG-021)
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_default_no_include(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_debug._session.snapshot.return_value = DebugSnapshot(
            registers=mock_debug._session.read_registers.return_value,
            callstack=mock_debug._session.callstack.return_value,
            threads=(),
            disasm_around_pc="",
            peripheral_dumps=(),
            capture_time="2026-05-21T00:00:00Z",
            session=mock_debug._session.session_handle.return_value,
        )
        code, _, _ = _run(["debug", "snapshot"], capsys)
        assert code == 0
        mock_debug._session.snapshot.assert_called_once_with(include_peripherals=None)

    def test_include_peripheral_repeated(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_debug._session.snapshot.return_value = DebugSnapshot(
            registers=mock_debug._session.read_registers.return_value,
            callstack=mock_debug._session.callstack.return_value,
            threads=(),
            disasm_around_pc="",
            peripheral_dumps=(),
            capture_time="2026-05-21T00:00:00Z",
            session=mock_debug._session.session_handle.return_value,
        )
        code, _, _ = _run(
            [
                "debug", "snapshot",
                "--include-peripheral", "SCB",
                "--include-peripheral", "RCC",
            ],
            capsys,
        )
        assert code == 0
        mock_debug._session.snapshot.assert_called_once_with(
            include_peripherals=["SCB", "RCC"]
        )


# ---------------------------------------------------------------------------
# `decode-hardfault` (DIAG-001 gdb path)
# ---------------------------------------------------------------------------


class TestDecodeHardfault:
    """DIAG-001 gdb path: substrate COMPOSES the raw SCB+regs+callstack
    bundle (a DebugSnapshot); Claude CLASSIFIES the fault. No decode rule
    lives in substrate (HARD RULE 2 / ADR-004 — captures, doesn't interpret)."""

    def _wire_snapshot(
        self, mock_debug: MagicMock, *, cfsr: int = 0, hfsr: int = 0
    ) -> None:
        def _reg(name: str, value: int) -> RegisterValue:
            return RegisterValue(
                name=name, address="0xE000ED28", raw_value=value,
                width_bits=32, access="RW", fields={},
            )

        scb = PeripheralDump(
            peripheral="SCB", instance="SCB", base_address="0xE000ED00",
            registers={"CFSR": _reg("CFSR", cfsr), "HFSR": _reg("HFSR", hfsr)},
            raw_bytes=None, suspicious_unmapped=False,
        )
        mock_debug._session.snapshot.return_value = DebugSnapshot(
            registers=RegisterDump(values={"pc": 0x08000400}, fpu_present=False),
            callstack=CallStack(frames=[]),
            threads=(),
            disasm_around_pc="",
            peripheral_dumps=(scb,),
            capture_time="2026-05-25T00:00:00Z",
            session=mock_debug._session.session_handle.return_value,
        )

    def test_composes_raw_scb_bundle(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        self._wire_snapshot(mock_debug, cfsr=(1 << 16), hfsr=0x40000000)
        code, out, _ = _run(["debug", "decode-hardfault"], capsys)
        assert code == 0
        # The recipe gathers the canonical fault peripheral (SCB) as a raw
        # bundle — nothing more, nothing interpreted.
        mock_debug._session.snapshot.assert_called_once_with(
            include_peripherals=["SCB"]
        )
        body = _read_json(out)
        scb = body["peripheral_dumps"][0]
        assert scb["peripheral"] == "SCB"
        assert scb["registers"]["CFSR"]["raw_value"] == (1 << 16)
        assert scb["registers"]["HFSR"]["raw_value"] == 0x40000000

    def test_emits_no_substrate_verdict(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Set bits that the old substrate-side rule would have classified as
        # MemManage; the bundle must carry NO classification — Claude reads
        # the raw CFSR/HFSR and decides.
        self._wire_snapshot(mock_debug, cfsr=(1 << 1) | (1 << 8), hfsr=0x40000000)
        code, out, _ = _run(["debug", "decode-hardfault"], capsys)
        assert code == 0
        body = _read_json(out)
        for verdict in ("fault_type", "fault_decode", "hardfault_detected"):
            assert verdict not in body, (
                f"{verdict!r} is a substrate verdict — must not appear "
                "(HARD RULE 2: substrate captures, doesn't interpret)"
            )


# ---------------------------------------------------------------------------
# Error envelope — SubstrateError → stderr JSON + exit 1
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    def test_gdb_error_emits_stderr_json(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        mock_debug._instance.start_session.side_effect = GDBError(
            message="probe not found",
            gdb_marker="probe-not-found",
            hint="run `stm32 prog list-probes`",
        )
        elf = tmp_path / "demo.elf"
        elf.write_bytes(b"\x7fELF")
        code, _, err = _run(["debug", "start", str(elf)], capsys)
        assert code == 1
        body = json.loads(err)
        assert body["gdb_marker"] == "probe-not-found"


# ---------------------------------------------------------------------------
# Always-tear-down — finally clause runs even on op-level exceptions
# ---------------------------------------------------------------------------


class TestAlwaysTearDown:
    def test_close_called_on_op_exception(
        self, ensure_cli_on_path, mock_debug: MagicMock,
        capsys: pytest.CaptureFixture,
    ) -> None:
        mock_debug._session.read_peripheral.side_effect = GDBError(
            message="boom", gdb_marker="command-timeout",
        )
        code, _, _ = _run(["debug", "read-peripheral", "RCC"], capsys)
        assert code == 1
        mock_debug._session.close.assert_called_once()
