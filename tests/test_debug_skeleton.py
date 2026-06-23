"""C4a debug skeleton tests — package imports, result dataclasses are
frozen, error hierarchy extended correctly."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.debug import (
    Breakpoint,
    CallStack,
    ComparisonResult,
    Debug,
    DebugSession,
    DebugSnapshot,
    FieldValue,
    MIAsyncRecord,
    MIResultRecord,
    MIStreamRecord,
    PeripheralDump,
    RegisterDump,
    RegisterValue,
    RunResult,
    SessionHandle,
    StackFrame,
    StoppedNotification,
    SvdDb,
    SvdSourceRoots,
    ThreadInfo,
    VariableValue,
)
from embedagents.stm32.debug.results import MemoryReadResult
from embedagents.stm32.errors import (
    GDBError,
    GDBSessionLost,
    SVDLookupError,
    TargetNotHalted,
)


@pytest.fixture()
def ctx(tmp_path: Path) -> SubstrateContext:
    return SubstrateContext.from_environment(project_path=tmp_path)


# ---------------------------------------------------------------------------
# Public surface visible
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_all_classes_importable(self) -> None:
        for cls in (
            Breakpoint,
            CallStack,
            ComparisonResult,
            DebugSnapshot,
            FieldValue,
            MIAsyncRecord,
            MIResultRecord,
            MIStreamRecord,
            PeripheralDump,
            RegisterDump,
            RegisterValue,
            RunResult,
            SessionHandle,
            StackFrame,
            StoppedNotification,
            ThreadInfo,
            VariableValue,
        ):
            assert is_dataclass(cls)


# ---------------------------------------------------------------------------
# Result dataclasses are frozen
# ---------------------------------------------------------------------------


class TestResultsFrozen:
    def test_session_handle_frozen(self, tmp_path: Path) -> None:
        sh = SessionHandle(
            gdbserver_pid=123,
            gdb_pid=456,
            gdb_port=61234,
            target_halted=True,
            target_state="halted",
            elf_path=tmp_path / "x.elf",
        )
        with pytest.raises(FrozenInstanceError):
            sh.gdb_port = 0  # type: ignore[misc]

    def test_register_dump_frozen(self) -> None:
        rd = RegisterDump(values={"r0": 0}, fpu_present=False)
        with pytest.raises(FrozenInstanceError):
            rd.fpu_present = True  # type: ignore[misc]

    def test_run_result_defaults(self) -> None:
        r = RunResult(breakpoint_hit=False, breakpoint=None, target_halted=False)
        assert r.halt_reason == "unknown"
        assert r.duration_s == 0.0


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    def test_gdb_session_lost_is_gdb_error(self) -> None:
        assert issubclass(GDBSessionLost, GDBError)

    def test_target_not_halted_is_gdb_error(self) -> None:
        assert issubclass(TargetNotHalted, GDBError)

    def test_svd_lookup_is_gdb_error(self) -> None:
        assert issubclass(SVDLookupError, GDBError)

    def test_gdb_error_extra_fields(self) -> None:
        err = GDBError(
            message="boom",
            gdb_marker="port-busy",
            gdbserver_exit_code=1,
            gdb_exit_code=None,
            target_state="halted",
        )
        assert err.gdbserver_exit_code == 1
        assert err.target_state == "halted"

    def test_svd_lookup_carries_candidates(self, tmp_path: Path) -> None:
        err = SVDLookupError(
            message="nope",
            device_id="STM32X",
            requested_name="USART1",
            candidates=(tmp_path / "a", tmp_path / "b"),
            attempted_paths=(tmp_path / "x.svd",),
        )
        assert len(err.candidates) == 2
        assert len(err.attempted_paths) == 1


# ---------------------------------------------------------------------------
# Class skeletons — NotImplementedError until body lands
# ---------------------------------------------------------------------------


class TestClassSkeletons:
    def test_debug_construct(self, ctx: SubstrateContext) -> None:
        client = Debug(ctx)
        assert client.ctx is ctx
        assert client._log.name == "embedagents.stm32.debug"

    # start_session() + attach_running() implemented in C4g;
    # verified in test_debug_start_session.py.


# ---------------------------------------------------------------------------
# ctx.svd_db populated by from_environment
# ---------------------------------------------------------------------------


class TestSvdDbPopulated:
    def test_ctx_has_svd_db(self, ctx: SubstrateContext) -> None:
        assert ctx.svd_db is not None
        assert isinstance(ctx.svd_db, SvdDb)

    def test_svd_db_has_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hermetic: force tool resolution to find nothing, regardless of
        # whether ST tools happen to be installed on the test runner. The
        # env-var / PATH fallback in _resolve_one_tool otherwise resolves a
        # real STM32CubeIDE on a tool-equipped machine (e.g. a Windows
        # release-test station with CubeIDE installed), which gives the SVD
        # roots a live cubeide source and breaks the "no tools" premise —
        # the test passed on Linux CI only because no ST tools are on PATH
        # there.
        from embedagents.stm32 import context as _context

        monkeypatch.setattr(_context, "_resolve_one_tool", lambda _def: None)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        roots = ctx.svd_db.roots
        assert isinstance(roots, SvdSourceRoots)
        # All sources None / unresolved when no tools resolve.
        assert roots.configured() == ()
