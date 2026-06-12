"""C4f DebugSession tests — in-session methods.

Mocks GDBClient + SvdDb directly to exercise orchestration without
involving real subprocesses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock
from typing import Any

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.debug.parsers import parse_mi_record
from embedagents.stm32.debug.results import (
    Breakpoint,
    CallStack,
    DebugSnapshot,
    MIAsyncRecord,
    MemoryReadResult,
    PeripheralDump,
    RegisterDump,
    RunResult,
    SessionHandle,
    StoppedNotification,
    VariableValue,
)
from embedagents.stm32.debug.session import DebugSession
from embedagents.stm32.debug.svd import (
    SvdDb,
    SvdField,
    SvdPeripheral,
    SvdRegister,
    SvdSourceRoots,
)
from embedagents.stm32.errors import GDBError, TargetNotHalted


# ---------------------------------------------------------------------------
# Test fixtures: fake subprocesses + minimal SvdDb stub
# ---------------------------------------------------------------------------


@dataclass
class FakeGDBServer:
    pid: int = 11111
    port: int = 61234
    _closed: bool = False

    def close(self, *, grace_s: float = 3.0) -> int | None:
        self._closed = True
        return 0

    def poll(self) -> int | None:
        return None if not self._closed else 0


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    for env_var, name in (
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _make_session(
    ctx: SubstrateContext, gdb_mock: MagicMock, *, elf: Path | None = None
) -> DebugSession:
    elf = elf or (ctx.cwd / "demo.elf")
    if not elf.exists():
        elf.write_bytes(b"")
    gdb_mock.pid = 22222
    return DebugSession(
        ctx=ctx,
        gdbserver=FakeGDBServer(),  # type: ignore[arg-type]
        gdb=gdb_mock,
        elf_path=elf,
    )


# ---------------------------------------------------------------------------
# Context-manager protocol + session-state registration
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_enter_registers_in_session_state(
        self, ctx: SubstrateContext
    ) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        with session as s:
            assert ctx.session_state.active_debug_session is s
        assert ctx.session_state.active_debug_session is None

    def test_exit_clears_session_state(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        with session:
            pass
        # Subprocesses asked to close.
        gdb.close.assert_called_once()

    def test_close_idempotent(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        session.close()
        session.close()
        gdb.close.assert_called_once()


# ---------------------------------------------------------------------------
# Target control
# ---------------------------------------------------------------------------


class TestTargetControl:
    def test_halt_after_attach_running_uses_monitor_halt(
        self, ctx: SubstrateContext
    ) -> None:
        # RES-041: the attach_running case — the core runs but gdb's
        # connect snapshot says stopped. -exec-interrupt here corrupts a
        # faulted core's sticky CFSR; the server-side `halt` Rcmd is the
        # clean mechanism.
        gdb = MagicMock()
        gdb.send_console.return_value = []
        session = _make_session(ctx, gdb)
        session.target_halted = False
        session.halt()
        gdb.send_console.assert_called_with("monitor halt", timeout_s=10.0)
        gdb.send_mi.assert_not_called()
        assert session.target_halted is True

    def test_halt_while_gdb_believes_running_uses_exec_interrupt(
        self, ctx: SubstrateContext
    ) -> None:
        # After OUR -exec-continue gdb knows the target runs —
        # -exec-interrupt is the proper MI interrupt (bench-verified
        # under mi-async).
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        session.resume()
        session.halt()
        gdb.send_mi.assert_called_with("-exec-interrupt", timeout_s=5.0)
        assert session.target_halted is True
        assert session._gdb_believes_running is False

    def test_resume(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        session.resume()
        gdb.send_mi.assert_called_with("-exec-continue", timeout_s=5.0)
        assert session.target_halted is False

    def test_reset_halt_after(self, ctx: SubstrateContext) -> None:
        # RES-041: ST-LINK gdbserver rejects the OpenOCD `reset halt`
        # form; plain `monitor reset` halts at Reset_Handler while
        # attached, so halt_after=True needs no extra command.
        gdb = MagicMock()
        gdb.send_console.return_value = []
        session = _make_session(ctx, gdb)
        session.reset(halt_after=True)
        gdb.send_console.assert_called_with("monitor reset", timeout_s=10.0)
        gdb.send_mi.assert_not_called()  # no resume
        assert session.target_halted is True

    def test_reset_no_halt(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_console.return_value = []
        session = _make_session(ctx, gdb)
        session.reset(halt_after=False)
        gdb.send_console.assert_called_with("monitor reset", timeout_s=10.0)
        # The reset leaves the core halted; halt_after=False resumes.
        assert gdb.send_mi.call_args[0][0] == "-exec-continue"
        assert session.target_halted is False

    def test_send_monitor_updates_halt_state(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_console.return_value = []
        session = _make_session(ctx, gdb)
        session.target_halted = False
        session.send_monitor("halt")
        assert session.target_halted is True
        session.send_monitor("continue")
        assert session.target_halted is False


# ---------------------------------------------------------------------------
# read_registers
# ---------------------------------------------------------------------------


class TestReadRegisters:
    def test_combines_names_and_values(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        names_rec = parse_mi_record('^done,register-names=["r0","sp","pc"]')
        values_rec = parse_mi_record(
            '^done,register-values=['
            '{number="0",value="0x0"},'
            '{number="1",value="0x20001000"},'
            '{number="2",value="0x08000234"}'
            ']'
        )
        gdb.send_mi.side_effect = [names_rec, values_rec]
        session = _make_session(ctx, gdb)
        dump = session.read_registers()
        assert isinstance(dump, RegisterDump)
        assert dump.values["sp"] == 0x20001000
        assert dump.values["pc"] == 0x08000234

    def test_target_running_raises(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        session.target_halted = False
        with pytest.raises(TargetNotHalted):
            session.read_registers()


# ---------------------------------------------------------------------------
# read_memory
# ---------------------------------------------------------------------------


class TestReadMemory:
    def test_canonical_invocation(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record(
            '^done,memory=[{begin="0x20000000",end="0x20000010",'
            'offset="0x0",contents="deadbeefcafebabe1122334455667788"}]'
        )
        session = _make_session(ctx, gdb)
        result = session.read_memory("0x20000000", 16)
        assert isinstance(result, MemoryReadResult)
        assert result.bytes_read == 16
        # Address is MI-quoted (IMP-15) so expressions with spaces stay
        # one argument.
        assert gdb.send_mi.call_args[0][0] == (
            '-data-read-memory-bytes "0x20000000" 16'
        )
        # A-012: timeout derives from debug.read_memory_base_s (5) +
        # read_memory_per_mb_s (5) scaled by size — 16 bytes ≈ base.
        assert gdb.send_mi.call_args[1]["timeout_s"] == pytest.approx(
            5.0, abs=0.01
        )

    def test_all_ff_flagged_suspicious(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record(
            '^done,memory=[{begin="0x080F0000",end="0x080F0010",'
            'offset="0x0",contents="ffffffffffffffffffffffffffffffff"}]'
        )
        session = _make_session(ctx, gdb)
        result = session.read_memory("0x080F0000", 16)
        assert result.suspicious_unmapped is True

    def test_zero_size_rejected(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        with pytest.raises(ValueError, match="positive"):
            session.read_memory("0x20000000", 0)


# ---------------------------------------------------------------------------
# read_peripheral
# ---------------------------------------------------------------------------


class TestReadPeripheral:
    def test_decodes_via_svd(
        self, ctx: SubstrateContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a fake SvdDb that returns a single-register peripheral.
        fake_periph = SvdPeripheral(
            name="USART1",
            base_address=0x40013800,
            registers={
                "CR1": SvdRegister(
                    name="CR1",
                    address_offset=0,
                    width_bits=32,
                    access="RW",
                    reset_value=0,
                    fields={
                        "UE": SvdField(name="UE", bit_offset=0, bit_width=1),
                    },
                )
            },
        )

        svd_mock = MagicMock(spec=SvdDb)
        svd_mock.get_peripheral.return_value = fake_periph
        from embedagents.stm32.debug.svd import _canonical_svd_filename  # noqa: F401

        # decode_register passes through to the real implementation so
        # we can verify the bitfield extraction end-to-end.
        from embedagents.stm32.debug.svd import SvdDb as _RealSvdDb

        svd_mock.decode_register.side_effect = _RealSvdDb.decode_register.__get__(
            SvdDb(roots=SvdSourceRoots()), SvdDb
        )

        ctx.session_state.active_debug_session = None
        # Inject the mock onto the frozen SubstrateContext.
        object.__setattr__(ctx, "svd_db", svd_mock)

        gdb = MagicMock()
        # Memory read: 4 bytes covering CR1 (offset 0, 32-bit).
        # UE=1 → first byte LE = 0x01.
        gdb.send_mi.return_value = parse_mi_record(
            '^done,memory=[{begin="0x40013800",end="0x40013804",'
            'offset="0x0",contents="01000000"}]'
        )

        session = _make_session(ctx, gdb)
        dump = session.read_peripheral("USART1")
        assert isinstance(dump, PeripheralDump)
        assert dump.peripheral == "USART1"
        assert "CR1" in dump.registers
        assert dump.registers["CR1"].fields["UE"].raw_value == 1

    def test_no_svd_db_raises(self, ctx: SubstrateContext) -> None:
        object.__setattr__(ctx, "svd_db", None)
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        with pytest.raises(GDBError) as excinfo:
            session.read_peripheral("USART1")
        assert excinfo.value.gdb_marker == "svd-not-found"


# ---------------------------------------------------------------------------
# callstack
# ---------------------------------------------------------------------------


class TestCallstack:
    def test_combines_frames_and_threads(
        self, ctx: SubstrateContext
    ) -> None:
        gdb = MagicMock()
        stack_rec = parse_mi_record(
            '^done,stack=[frame={level="0",addr="0x100",func="main",file="m.c",line="42"}]'
        )
        threads_rec = parse_mi_record(
            '^done,threads=[{id="1",name="t",state="stopped"}],current-thread-id="1"'
        )
        gdb.send_mi.side_effect = [stack_rec, threads_rec]
        session = _make_session(ctx, gdb)
        cs = session.callstack()
        assert isinstance(cs, CallStack)
        assert len(cs.frames) == 1
        assert cs.frames[0].function == "main"
        assert cs.threads[0].state == "halted"

    def test_full_keeps_frames_and_fills_args(
        self, ctx: SubstrateContext
    ) -> None:
        """A-004: full=True must run frames + arguments (merge), not
        substitute the frames command — that returned empty frames."""
        gdb = MagicMock()
        stack_rec = parse_mi_record(
            '^done,stack=[frame={level="0",addr="0x100",func="main",file="m.c",line="42"}]'
        )
        args_rec = parse_mi_record(
            '^done,stack-args=[frame={level="0",args=[{name="argc",value="1"}]}]'
        )
        threads_rec = parse_mi_record(
            '^done,threads=[{id="1",name="t",state="stopped"}],current-thread-id="1"'
        )
        gdb.send_mi.side_effect = [stack_rec, args_rec, threads_rec]
        session = _make_session(ctx, gdb)
        cs = session.callstack(full=True)
        sent = [c.args[0] for c in gdb.send_mi.call_args_list]
        assert sent[0].startswith("-stack-list-frames")
        assert sent[1].startswith("-stack-list-arguments")
        assert len(cs.frames) == 1
        assert cs.frames[0].function == "main"
        assert cs.frames[0].args == {"argc": "1"}


# ---------------------------------------------------------------------------
# Breakpoint workflow
# ---------------------------------------------------------------------------


class TestBreakpoints:
    def test_set_breakpoint(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record(
            '^done,bkpt={number="1",addr="0x08001234",file="m.c",line="42",'
            'original-location="m.c:42"}'
        )
        session = _make_session(ctx, gdb)
        bp = session.set_breakpoint("m.c:42")
        assert isinstance(bp, Breakpoint)
        assert bp.number == 1
        assert session._breakpoints[1] is bp

    def test_set_breakpoint_location_mi_quoted(
        self, ctx: SubstrateContext
    ) -> None:
        # IMP-15: spaces stay one argument; a newline can't inject a
        # second MI command.
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record(
            '^done,bkpt={number="1",addr="0x0",original-location="x"}'
        )
        session = _make_session(ctx, gdb)
        session.set_breakpoint("my file.c:loop\n-exec-run")
        sent = gdb.send_mi.call_args[0][0]
        assert sent == '-break-insert "my file.c:loop\\n-exec-run"'
        assert "\n" not in sent

    def test_remove_breakpoint(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        bp = Breakpoint(number=5, location="main")
        session._breakpoints[5] = bp
        session.remove_breakpoint(bp)
        gdb.send_mi.assert_called_with("-break-delete 5", timeout_s=5.0)
        assert 5 not in session._breakpoints

    def test_run_until_breakpoint_hit(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        # First send_mi → exec-continue ^done.
        gdb.send_mi.return_value = parse_mi_record("1^done")
        from embedagents.stm32.debug.parsers import parse_stopped

        stopped_record = parse_mi_record(
            '*stopped,reason="breakpoint-hit",bkptno="3"'
        )
        assert isinstance(stopped_record, MIAsyncRecord)
        gdb.wait_for_stopped.return_value = parse_stopped(stopped_record)

        session = _make_session(ctx, gdb)
        bp3 = Breakpoint(number=3, location="loop")
        session._breakpoints[3] = bp3

        result = session.run_until_breakpoint(timeout_s=1.0)
        assert isinstance(result, RunResult)
        assert result.breakpoint_hit is True
        assert result.breakpoint is bp3
        assert result.halt_reason == "breakpoint"

    def test_run_until_timeout_not_hit(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record("1^done")
        gdb.wait_for_stopped.return_value = None
        session = _make_session(ctx, gdb)
        result = session.run_until_breakpoint(timeout_s=1.0)
        assert result.breakpoint_hit is False
        assert result.halt_reason == "timeout"
        # Substrate sent -exec-interrupt to recover.
        assert any(
            call.args == ("-exec-interrupt",)
            or (call.args and "-exec-interrupt" in str(call.args[0]))
            for call in gdb.send_mi.call_args_list
        )


# ---------------------------------------------------------------------------
# read_variable + compare
# ---------------------------------------------------------------------------


class TestVariablesAndCompare:
    def test_read_variable(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record('^done,value="42"')
        session = _make_session(ctx, gdb)
        v = session.read_variable("counter")
        assert isinstance(v, VariableValue)
        assert v.name == "counter"
        assert v.integer_value == 42

    def test_read_variable_expression_mi_quoted(
        self, ctx: SubstrateContext
    ) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record('^done,value="1"')
        session = _make_session(ctx, gdb)
        session.read_variable("buf[i + 1]")
        assert gdb.send_mi.call_args[0][0] == (
            '-data-evaluate-expression "buf[i + 1]"'
        )

    def test_compare_variable_matches(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record('^done,value="0x42"')
        session = _make_session(ctx, gdb)
        c = session.compare_variable("counter", 0x42)
        assert c.matches is True

    def test_compare_variable_with_mask(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record('^done,value="0xAA"')
        session = _make_session(ctx, gdb)
        # 0xAA & 0x0F = 0x0A; expected 0x0A → match.
        c = session.compare_variable("x", 0x0A, mask=0x0F)
        assert c.matches is True

    def test_compare_register(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        gdb.send_mi.side_effect = [
            parse_mi_record('^done,register-names=["pc"]'),
            parse_mi_record(
                '^done,register-values=[{number="0",value="0x08000234"}]'
            ),
        ]
        session = _make_session(ctx, gdb)
        c = session.compare_register("pc", 0x08000234)
        assert c.matches is True

    def test_compare_register_missing_raises(
        self, ctx: SubstrateContext
    ) -> None:
        gdb = MagicMock()
        gdb.send_mi.side_effect = [
            parse_mi_record('^done,register-names=["pc"]'),
            parse_mi_record(
                '^done,register-values=[{number="0",value="0x0"}]'
            ),
        ]
        session = _make_session(ctx, gdb)
        with pytest.raises(GDBError) as excinfo:
            session.compare_register("nosuch", 0)
        assert excinfo.value.gdb_marker == "register-not-in-dump"


# ---------------------------------------------------------------------------
# snapshot (DIAG-021)
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_composes_raw_reads(
        self, ctx: SubstrateContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mock everything snapshot uses: read_registers, callstack,
        # _capture_disasm_around_pc, read_peripheral.
        gdb = MagicMock()
        gdb.send_console.return_value = ["disasm output\n"]
        session = _make_session(ctx, gdb)

        # Stub the high-level methods to avoid wiring full gdb responses.
        from unittest.mock import patch

        with patch.object(session, "read_registers") as mock_read_regs, \
             patch.object(session, "callstack") as mock_callstack, \
             patch.object(session, "read_peripheral") as mock_read_periph:
            mock_read_regs.return_value = RegisterDump(values={"pc": 0x100}, fpu_present=False)
            mock_callstack.return_value = CallStack(frames=[], threads=[])
            mock_read_periph.return_value = PeripheralDump(
                peripheral="SCB", instance="SCB", base_address="0xE000ED00", registers={}
            )
            snap = session.snapshot()
        assert isinstance(snap, DebugSnapshot)
        assert snap.registers.values["pc"] == 0x100
        assert len(snap.peripheral_dumps) == 1
        assert snap.peripheral_dumps[0].peripheral == "SCB"

    def test_failing_peripheral_doesnt_abort(
        self, ctx: SubstrateContext
    ) -> None:
        gdb = MagicMock()
        gdb.send_console.return_value = []
        session = _make_session(ctx, gdb)
        from unittest.mock import patch

        with patch.object(session, "read_registers") as mock_regs, \
             patch.object(session, "callstack") as mock_cs, \
             patch.object(session, "read_peripheral") as mock_periph:
            mock_regs.return_value = RegisterDump(values={}, fpu_present=False)
            mock_cs.return_value = CallStack(frames=[], threads=[])
            mock_periph.side_effect = GDBError(message="boom", gdb_marker="svd-not-found")
            snap = session.snapshot()
        assert snap.peripheral_dumps == ()


# ---------------------------------------------------------------------------
# runtime-default knobs (A-012)
# ---------------------------------------------------------------------------


def _ctx_with_debug_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug_defaults: dict
) -> SubstrateContext:
    import json

    for env_var, name in (
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    (tmp_path / "stm32-runtime-defaults.jsonc").write_text(
        json.dumps({"version": 1, "debug": debug_defaults})
    )
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestRuntimeDefaultKnobs:
    """A-012 — every ``debug.*`` schema knob must steer real behavior."""

    def test_read_registers_uses_read_timeout_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx_with_debug_defaults(
            tmp_path, monkeypatch, {"read_timeout_s": 42}
        )
        gdb = MagicMock()
        gdb.send_mi.side_effect = [
            parse_mi_record('^done,register-names=["pc"]'),
            parse_mi_record(
                '^done,register-values=[{number="0",value="0x0"}]'
            ),
        ]
        session = _make_session(ctx, gdb)
        session.read_registers()
        for call in gdb.send_mi.call_args_list:
            assert call.kwargs["timeout_s"] == 42.0

    def test_read_memory_timeout_scales_with_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx_with_debug_defaults(
            tmp_path,
            monkeypatch,
            {"read_memory_base_s": 2, "read_memory_per_mb_s": 4},
        )
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record(
            '^done,memory=[{begin="0x20000000",end="0x20000010",'
            'offset="0x0",contents="00"}]'
        )
        session = _make_session(ctx, gdb)
        session.read_memory("0x20000000", 2 * 1_048_576)
        # base 2 + per_mb 4 × 2 MB = 10 s.
        assert gdb.send_mi.call_args[1]["timeout_s"] == pytest.approx(10.0)

    def test_run_until_breakpoint_default_from_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx_with_debug_defaults(
            tmp_path, monkeypatch, {"breakpoint_wait_timeout_s": 7}
        )
        gdb = MagicMock()
        gdb.send_mi.return_value = parse_mi_record("1^done")
        gdb.wait_for_stopped.return_value = None
        session = _make_session(ctx, gdb)
        session.run_until_breakpoint()  # no explicit timeout
        assert gdb.wait_for_stopped.call_args.kwargs["timeout_s"] == 7.0

    def test_snapshot_budget_skips_remaining_peripherals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        ctx = _ctx_with_debug_defaults(
            tmp_path, monkeypatch, {"snapshot_timeout_s": 1}
        )
        # First monotonic() call computes the deadline; every later call
        # reports the budget as long blown.
        clock = iter([0.0] + [100.0] * 50)
        monkeypatch.setattr(
            "embedagents.stm32.debug.session.time",
            SimpleNamespace(monotonic=lambda: next(clock)),
        )
        gdb = MagicMock()
        gdb.send_console.return_value = []
        session = _make_session(ctx, gdb)
        with patch.object(session, "read_registers") as mock_regs, \
             patch.object(session, "callstack") as mock_cs, \
             patch.object(session, "read_peripheral") as mock_periph:
            mock_regs.return_value = RegisterDump(values={}, fpu_present=False)
            mock_cs.return_value = CallStack(frames=[], threads=[])
            snap = session.snapshot(include_peripherals=["SCB", "RCC"])
        # Budget exhausted before the first peripheral → none read,
        # registers/callstack still captured.
        mock_periph.assert_not_called()
        assert snap.peripheral_dumps == ()
        assert snap.registers is mock_regs.return_value


# ---------------------------------------------------------------------------
# session_handle
# ---------------------------------------------------------------------------


class TestSessionHandle:
    def test_returns_handle_snapshot(self, ctx: SubstrateContext) -> None:
        gdb = MagicMock()
        session = _make_session(ctx, gdb)
        h = session.session_handle()
        assert isinstance(h, SessionHandle)
        assert h.gdb_port == 61234
        assert h.target_halted is True
        assert h.target_state == "halted"
