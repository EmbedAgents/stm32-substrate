"""ST-LINK gdbserver + arm-none-eabi-gdb wrapper + SVD lookup.

Per RES-012 + RES-013, the v1 surface is **raw reads only**: lifecycle +
target control + ``read_registers`` / ``read_peripheral`` / ``read_memory``
/ ``callstack`` / ``snapshot`` + breakpoint workflow. DIAG-001..017
peripheral-state checks live as **Claude-side recipes** composed from
these raw reads.

Public surface:

- ``Debug`` — lifecycle entry point (``start_session`` /
  ``attach_running``).
- ``DebugSession`` — context manager owning gdbserver + gdb subprocesses.
- ``SvdDb`` — 3-path priority SVD lookup (CubeIDE → CubeProgrammer →
  CLT); populated on ``SubstrateContext.from_environment`` and exposed
  as ``ctx.svd_db`` for cross-module reads (cubeprogrammer D-008).
- Result types: ``SessionHandle`` / ``RegisterDump`` / ``PeripheralDump``
  / ``CallStack`` / ``Breakpoint`` / ``RunResult`` / ``VariableValue`` /
  ``ComparisonResult`` / ``DebugSnapshot`` + supporting shapes.

See ``v1/debug-api.md`` for the full method list, gdb-MI primitives,
and recipe-vs-substrate split.
"""

from __future__ import annotations

from embedagents.stm32.debug.client import Debug
from embedagents.stm32.debug.results import (
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
)
from embedagents.stm32.debug.session import DebugSession
from embedagents.stm32.debug.svd import SvdDb, SvdSourceRoots

__all__ = [
    "Debug",
    "DebugSession",
    "SvdDb",
    "SvdSourceRoots",
    "Breakpoint",
    "CallStack",
    "ComparisonResult",
    "DebugSnapshot",
    "FieldValue",
    "MIAsyncRecord",
    "MIResultRecord",
    "MIStreamRecord",
    "PeripheralDump",
    "RegisterDump",
    "RegisterValue",
    "RunResult",
    "SessionHandle",
    "StackFrame",
    "StoppedNotification",
    "ThreadInfo",
    "VariableValue",
]
