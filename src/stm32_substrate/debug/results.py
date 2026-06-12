"""Debug-module result dataclasses.

All frozen, all JSON-serializable. Per ``v1/debug-api.md`` § "Result types".
Substrate captures, doesn't interpret — ``raw_value`` / ``raw`` fields
carry the unmodified gdb / SVD output for callers to inspect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Session handle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionHandle:
    """Captured-on-entry snapshot of the ``DebugSession`` identity."""

    gdbserver_pid: int
    gdb_pid: int
    gdb_port: int
    target_halted: bool
    target_state: Literal["halted", "running", "unknown"]
    elf_path: Path
    n6_dev_mode_confirmed: bool = False


# ---------------------------------------------------------------------------
# CPU registers (DBG-006)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisterDump:
    """``r0..r12``, ``sp``, ``lr``, ``pc``, ``xpsr``, ``msp``, ``psp``,
    ``primask``, ``basepri``, ``faultmask``, ``control``, ``+fpu`` when
    present. ``secure_world`` is M33-TZ-only — None on non-TZ devices.
    """

    values: dict[str, int]
    fpu_present: bool
    secure_world: bool | None = None


# ---------------------------------------------------------------------------
# Peripheral SVD-decoded (DBG-007 + every DIAG recipe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldValue:
    """One bitfield extracted from a register raw value via the SVD."""

    name: str
    bit_offset: int
    bit_width: int
    raw_value: int
    enum_name: str | None = None


@dataclass(frozen=True)
class RegisterValue:
    """One register from a peripheral block."""

    name: str
    address: str
    raw_value: int
    width_bits: int
    access: Literal["RO", "WO", "RW", "RW_w0c", "RW_w1c", "unknown"]
    fields: dict[str, FieldValue]


@dataclass(frozen=True)
class PeripheralDump:
    """Full peripheral memory block + SVD-decoded register views."""

    peripheral: str
    instance: str
    base_address: str
    registers: dict[str, RegisterValue]
    raw_bytes: bytes | None = None
    suspicious_unmapped: bool = False


# ---------------------------------------------------------------------------
# Memory read (DBG raw)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryReadResult:
    """Raw memory read from a debug session. Parity with
    ``cubeprogrammer.MemoryReadResult.suspicious_unmapped`` per RES-020."""

    address: str
    size: int
    bytes_read: int
    hex_dump: str
    raw_bytes: bytes | None = None
    suspicious_unmapped: bool = False


# ---------------------------------------------------------------------------
# Callstack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StackFrame:
    level: int
    pc: str
    function: str | None
    file: str | None
    line: int | None
    args: dict[str, str] | None = None


@dataclass(frozen=True)
class ThreadInfo:
    id: int
    name: str | None
    state: Literal["halted", "running", "unknown"]


@dataclass(frozen=True)
class CallStack:
    frames: list[StackFrame]
    threads: list[ThreadInfo] = field(default_factory=list)
    active_thread_index: int = 0


# ---------------------------------------------------------------------------
# Breakpoint workflow (DBG-004 / DBG-005)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Breakpoint:
    number: int
    location: str
    address: str | None = None
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class RunResult:
    breakpoint_hit: bool
    breakpoint: Breakpoint | None
    target_halted: bool
    halt_reason: Literal[
        "breakpoint", "signal", "exited", "timeout", "unknown"
    ] = "unknown"
    duration_s: float = 0.0


@dataclass(frozen=True)
class VariableValue:
    name: str
    type_name: str
    raw: str
    integer_value: int | None = None
    optimized_out: bool = False
    address: str | None = None


@dataclass(frozen=True)
class ComparisonResult:
    name: str
    observed: int | str
    expected: int | str
    mask: int | None
    matches: bool
    raw: VariableValue | None = None
    register_raw: int | None = None


# ---------------------------------------------------------------------------
# DIAG-021 snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DebugSnapshot:
    """DIAG-021 — composition of raw reads.

    Substrate composes; Claude reads. ``disasm_around_pc`` is verbatim
    gdb output (no parsing).
    """

    registers: RegisterDump
    callstack: CallStack
    threads: tuple[ThreadInfo, ...]
    disasm_around_pc: str
    peripheral_dumps: tuple[PeripheralDump, ...]
    capture_time: str
    session: SessionHandle


# ---------------------------------------------------------------------------
# gdb-MI records (parsers.py output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MIResultRecord:
    """``^done`` / ``^error`` / ``^running`` / ``^connected`` / ``^exit``."""

    token: int | None
    class_: str  # "done" / "error" / "running" / "connected" / "exit"
    fields: dict[str, Any]


@dataclass(frozen=True)
class MIAsyncRecord:
    """``*stopped`` / ``*running`` / ``=thread-created`` / etc."""

    kind: Literal["exec", "status", "notify"]
    class_: str  # "stopped" / "running" / thread-created etc.
    fields: dict[str, Any]


@dataclass(frozen=True)
class MIStreamRecord:
    """``~`` / ``@`` / ``&`` — console / target / log streams."""

    stream: Literal["console", "target", "log"]
    text: str


@dataclass(frozen=True)
class StoppedNotification:
    """Decoded ``*stopped`` payload."""

    reason: Literal[
        "breakpoint-hit",
        "signal-received",
        "exited-normally",
        "exited-signalled",
        "watchpoint-trigger",
        "end-stepping-range",
        "function-finished",
        "exited",
        "unknown",
    ]
    breakpoint_number: int | None = None
    signal_name: str | None = None
    frame: StackFrame | None = None
    raw_fields: dict[str, Any] = field(default_factory=dict)
