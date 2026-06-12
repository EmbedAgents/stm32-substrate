"""``DebugSession`` — in-session methods.

Per ``v1/debug-api.md`` § "DebugSession — in-session surface". Raw-reads
only (RES-012 Q1) — DIAG-001..017 peripheral-state checks compose from
these as Claude-side recipes.

The session owns gdbserver + gdb subprocesses for the lifetime of one
context-managed block. ``__enter__`` registers itself in
``ctx.session_state.active_debug_session`` so cubeprogrammer can route
``reset`` / ``halt`` / ``resume`` through the live gdb instead of
opening a competing SWD probe connection.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from embedagents.stm32.debug.gdb import mi_quote
from embedagents.stm32.debug.parsers import (
    parse_breakpoint_insert,
    parse_evaluate_expression,
    parse_memory_read,
    parse_register_dump,
    parse_stack_list_frames,
)
from embedagents.stm32.debug.results import (
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
    VariableValue,
)
from embedagents.stm32.errors import GDBError, TargetNotHalted

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext
    from embedagents.stm32.debug.gdb import GDBClient
    from embedagents.stm32.debug.gdbserver import GDBServerProcess
    from embedagents.stm32.progress import ProgressCallback


def _debug_default(ctx: "SubstrateContext", name: str, default: float) -> float:
    """Pull a numeric knob from ``ctx.defaults.debug.<name>`` with fallback.

    Mirrors ``_vcp_default`` / ``CubeProgrammer._timeout_s`` — every
    ``debug.*`` knob declared by the runtime-defaults schema is read here
    or in ``debug.client`` (A-012: the schema must not carry dead knobs).
    """
    debug_defaults = getattr(ctx.defaults, "debug", None)
    if debug_defaults is None:
        return default
    value = getattr(debug_defaults, name, None)
    return default if value is None else float(value)


class DebugSession:
    """Context manager owning gdbserver + gdb subprocesses for one session."""

    def __init__(
        self,
        *,
        ctx: "SubstrateContext",
        gdbserver: "GDBServerProcess",
        gdb: "GDBClient",
        elf_path: Path,
        n6_dev_mode_confirmed: bool = False,
    ) -> None:
        self.ctx = ctx
        self._gdbserver = gdbserver
        self._gdb = gdb
        self.elf_path = elf_path
        self.gdbserver_pid = gdbserver.pid
        self.gdb_pid = gdb.pid
        self.gdb_port = gdbserver.port
        self.target_halted = True
        self.n6_dev_mode_confirmed = n6_dev_mode_confirmed
        self._log = ctx.logger.getChild("debug.session")
        self._closed = False
        self._breakpoints: dict[int, Breakpoint] = {}
        # Whether GDB's own state machine thinks the target is running.
        # Diverges from ``target_halted`` after attach_running: the core
        # runs, but gdb received a stop snapshot at connect. halt() picks
        # its mechanism off this flag (RES-041).
        self._gdb_believes_running = False

    # ------------------------------------------------------------------
    # context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "DebugSession":
        self.ctx.session_state.active_debug_session = self
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Clear the session-state slot first so other modules
        # (cubeprogrammer.reset/halt/resume) stop routing through us.
        if self.ctx.session_state.active_debug_session is self:
            self.ctx.session_state.active_debug_session = None
        try:
            self._gdb.close()
        finally:
            self._gdbserver.close()

    # ------------------------------------------------------------------
    # target control
    # ------------------------------------------------------------------

    def halt(self) -> None:
        """Halt the target.

        Mechanism depends on what GDB believes (RES-041, bench-verified
        on the L476/v7.13.0):

        - GDB thinks the target is running (we sent ``-exec-continue``)
          → ``-exec-interrupt``, the proper MI interrupt.
        - GDB thinks the target is stopped but the core is physically
          running (the ``attach_running`` / gdbserver ``-g`` case — gdb
          got a stop snapshot at connect) → ``monitor halt``, the
          server-side Rcmd. ``-exec-interrupt`` here pokes an
          already-"stopped" target and corrupts a faulted core's sticky
          CFSR (extra UFSR/BFSR bits latch and memory reads garble).
        """
        if self._gdb_believes_running:
            self._gdb.send_mi("-exec-interrupt", timeout_s=5.0)
            self._gdb_believes_running = False
        else:
            self._gdb.send_console("monitor halt", timeout_s=10.0)
        self.target_halted = True
        self._log.info("halt")

    def resume(self) -> None:
        """Resume the target (``-exec-continue``)."""
        self._gdb.send_mi("-exec-continue", timeout_s=5.0)
        self.target_halted = False
        self._gdb_believes_running = True
        self._log.info("resume")

    def reset(self, *, halt_after: bool = True) -> None:
        """Reset the target via ``monitor reset``.

        ST-LINK gdbserver (bench-verified v7.13.0, RES-041): ``monitor
        reset`` system-resets and leaves the core HALTED at
        ``Reset_Handler`` while a debugger is attached; the OpenOCD form
        ``monitor reset halt`` is rejected with ``^error``. So
        ``halt_after=True`` is the server's natural behavior and
        ``halt_after=False`` resumes explicitly after the reset.
        """
        self._gdb.send_console("monitor reset", timeout_s=10.0)
        self.target_halted = True
        if not halt_after:
            self.resume()
        self._log.info("reset halt_after=%s", halt_after)

    def send_monitor(self, command: str) -> str:
        """Forward a raw ``monitor X`` command and return the captured
        stream output joined into one string.

        Used by cubeprogrammer's ``reset`` / ``halt`` / ``resume`` gdb-
        routing path so an active debug session takes precedence over a
        direct SWD connection.
        """
        lines = self._gdb.send_console(f"monitor {command}", timeout_s=10.0)
        # Update halt state on known commands so cross-module callers
        # stay in sync. `reset` halts at Reset_Handler while attached
        # (RES-041). The resume-flavored Rcmds don't exist on ST-LINK
        # gdbserver (would ^error above) but stay mapped defensively.
        cmd_lower = command.strip().lower()
        if cmd_lower in ("halt", "reset"):
            self.target_halted = True
        elif cmd_lower in ("continue", "go", "resume"):
            self.target_halted = False
        return "".join(lines)

    # ------------------------------------------------------------------
    # raw reads
    # ------------------------------------------------------------------

    def read_registers(self) -> RegisterDump:
        """DBG-006 — ``-data-list-register-names`` + ``-data-list-register-values x``."""
        self._require_halted()
        timeout_s = _debug_default(self.ctx, "read_timeout_s", 10.0)
        names = self._gdb.send_mi(
            "-data-list-register-names", timeout_s=timeout_s
        )
        values = self._gdb.send_mi(
            "-data-list-register-values x", timeout_s=timeout_s
        )
        return parse_register_dump(values, names)

    def read_memory(self, address: str, size: int) -> MemoryReadResult:
        """Raw memory read via ``-data-read-memory-bytes <addr> <size>``.

        Timeout scales with size: ``debug.read_memory_base_s`` +
        ``debug.read_memory_per_mb_s`` per MB requested.
        """
        if size <= 0:
            raise ValueError(f"read_memory size must be positive; got {size}")
        self._require_halted()
        timeout_s = _debug_default(
            self.ctx, "read_memory_base_s", 5.0
        ) + _debug_default(self.ctx, "read_memory_per_mb_s", 5.0) * (
            size / 1_048_576
        )
        record = self._gdb.send_mi(
            f"-data-read-memory-bytes {mi_quote(address)} {size}",
            timeout_s=timeout_s,
        )
        raw = parse_memory_read(record)
        if len(raw) < size:
            self._log.warning(
                "read_memory: requested %d bytes at %s, got %d "
                "(unreadable hole in the range — see bytes_read)",
                size,
                address,
                len(raw),
            )
        hex_dump = _render_hex_dump(raw, start_address=address)
        suspicious = bool(raw) and all(b == 0xFF for b in raw)
        return MemoryReadResult(
            address=address,
            size=size,
            bytes_read=len(raw),
            hex_dump=hex_dump,
            raw_bytes=raw,
            suspicious_unmapped=suspicious,
        )

    def read_peripheral(
        self, name: str, instance: str | None = None
    ) -> PeripheralDump:
        """DBG-007 — SVD lookup + memory read + bitfield decode.

        ``name`` is the peripheral name (e.g. ``"USART1"``); ``instance``
        defaults to the same value. Substrate looks up the SVD via
        ``ctx.svd_db.get_peripheral(device_name, name)`` where
        ``device_name`` is the currently-attached chip (resolved lazily —
        v1 simple-now uses the ELF path's stem as a fallback when no
        banner is available; future revs may probe via gdb directly).
        """
        self._require_halted()
        instance = instance or name
        svd_db = self.ctx.svd_db
        if svd_db is None:
            raise GDBError(
                message="ctx.svd_db is unset; cannot decode peripheral",
                gdb_marker="svd-not-found",
                hint="ensure SubstrateContext.from_environment() populates svd_db",
            )
        device_name = self._device_name_hint()
        periph = svd_db.get_peripheral(device_name, name)

        # Compute the peripheral memory window — read enough bytes to
        # cover the highest-offset register.
        if not periph.registers:
            return PeripheralDump(
                peripheral=name,
                instance=instance,
                base_address=f"0x{periph.base_address:08X}",
                registers={},
            )
        max_offset = max(
            reg.address_offset + (reg.width_bits // 8)
            for reg in periph.registers.values()
        )
        record = self._gdb.send_mi(
            f"-data-read-memory-bytes 0x{periph.base_address:08x} {max_offset}",
            timeout_s=_debug_default(self.ctx, "read_timeout_s", 10.0),
        )
        raw = parse_memory_read(record)
        suspicious = bool(raw) and all(b == 0xFF for b in raw)

        decoded: dict[str, RegisterValue] = {}
        for reg_name, reg in periph.registers.items():
            width_bytes = reg.width_bits // 8
            start = reg.address_offset
            end = start + width_bytes
            if end > len(raw):
                continue
            chunk = raw[start:end]
            raw_value = int.from_bytes(chunk, byteorder="little", signed=False)
            decoded[reg_name] = svd_db.decode_register(reg, raw_value)

        return PeripheralDump(
            peripheral=name,
            instance=instance,
            base_address=f"0x{periph.base_address:08X}",
            registers=decoded,
            raw_bytes=raw,
            suspicious_unmapped=suspicious,
        )

    def callstack(self, *, full: bool = False) -> CallStack:
        """``-stack-list-frames`` + ``-thread-info`` for thread state.

        ``full=True`` additionally runs ``-stack-list-arguments 1`` and
        merges the per-frame arguments into ``StackFrame.args`` by level.
        (It must not *replace* the frames command — the args command's
        ``stack-args`` payload carries no addr/func/file.)
        """
        self._require_halted()
        stack = self._gdb.send_mi(
            "-stack-list-frames --no-frame-filters", timeout_s=5.0
        )
        args_record = None
        if full:
            args_record = self._gdb.send_mi(
                "-stack-list-arguments --no-frame-filters 1", timeout_s=5.0
            )
        threads = self._gdb.send_mi("-thread-info", timeout_s=5.0)
        return parse_stack_list_frames(stack, threads, args_record=args_record)

    # ------------------------------------------------------------------
    # snapshot (DIAG-021)
    # ------------------------------------------------------------------

    def snapshot(
        self,
        *,
        include_peripherals: list[str] | None = None,
        on_progress: "ProgressCallback | None" = None,
    ) -> DebugSnapshot:
        """DIAG-021 — composition of raw reads only.

        ``include_peripherals`` lets the caller pick which peripherals to
        dump. ``None`` defaults to just ``"SCB"`` (the canonical
        fault-decode peripheral); add more for richer snapshots.

        The whole capture is bounded by ``debug.snapshot_timeout_s``
        (default 60 s, HIL no-long-waits): once the budget is exhausted,
        remaining peripherals are skipped with a warning rather than
        extending the wait — the result reflects what was captured.
        """
        self._require_halted()
        deadline = time.monotonic() + _debug_default(
            self.ctx, "snapshot_timeout_s", 60.0
        )
        registers = self.read_registers()
        try:
            cs = self.callstack()
        except GDBError as ex:
            # A faulted target — DIAG-001's primary subject — often has
            # an unwindable stack (corrupt SP is a canonical fault
            # cause, and ST-LINK gdbserver can garble reads while
            # unwinding the exception frame). Degrade like the
            # per-peripheral legs: empty callstack + warning beats
            # losing the registers/fault state the snapshot is for.
            self._log.warning(
                "snapshot: callstack unwind failed: %s", ex.message
            )
            cs = CallStack(frames=[], threads=[])
        disasm = self._capture_disasm_around_pc()

        peripherals = include_peripherals or ["SCB"]
        dumps: list[PeripheralDump] = []
        for name in peripherals:
            if time.monotonic() >= deadline:
                self._log.warning(
                    "snapshot: debug.snapshot_timeout_s budget exhausted; "
                    "skipping remaining peripherals starting at %s",
                    name,
                )
                break
            try:
                dumps.append(self.read_peripheral(name))
            except GDBError as ex:
                # Don't fail the whole snapshot for one missing peripheral;
                # callers see the missing entries in the result.
                self._log.warning(
                    "snapshot: read_peripheral(%s) failed: %s", name, ex.message
                )

        return DebugSnapshot(
            registers=registers,
            callstack=cs,
            threads=tuple(cs.threads),
            disasm_around_pc=disasm,
            peripheral_dumps=tuple(dumps),
            capture_time=datetime.now(timezone.utc).isoformat(),
            session=self.session_handle(),
        )

    def session_handle(self) -> SessionHandle:
        """Construct a ``SessionHandle`` snapshot of the live state."""
        return SessionHandle(
            gdbserver_pid=self.gdbserver_pid,
            gdb_pid=self.gdb_pid,
            gdb_port=self.gdb_port,
            target_halted=self.target_halted,
            target_state="halted" if self.target_halted else "running",
            elf_path=self.elf_path,
            n6_dev_mode_confirmed=self.n6_dev_mode_confirmed,
        )

    # ------------------------------------------------------------------
    # breakpoint workflow (DBG-004 / DBG-005)
    # ------------------------------------------------------------------

    def set_breakpoint(self, location: str) -> Breakpoint:
        """``-break-insert <location>`` (location MI-quoted per IMP-15)."""
        record = self._gdb.send_mi(
            f"-break-insert {mi_quote(location)}", timeout_s=5.0
        )
        bp = parse_breakpoint_insert(record)
        self._breakpoints[bp.number] = bp
        self._log.info("breakpoint #%d at %s", bp.number, bp.location)
        return bp

    def remove_breakpoint(self, bp: Breakpoint) -> None:
        """``-break-delete <number>``."""
        self._gdb.send_mi(f"-break-delete {bp.number}", timeout_s=5.0)
        self._breakpoints.pop(bp.number, None)

    def run_until_breakpoint(
        self, timeout_s: float | None = None
    ) -> RunResult:
        """``-exec-continue`` then wait for ``*stopped`` up to ``timeout_s``.

        Returns ``RunResult(breakpoint_hit=False, halt_reason="timeout")``
        when the timeout fires; sends ``-exec-interrupt`` to leave gdb
        in a known state. ``timeout_s=None`` falls back to
        ``debug.breakpoint_wait_timeout_s`` (default 30 s).
        """
        if timeout_s is None:
            timeout_s = _debug_default(
                self.ctx, "breakpoint_wait_timeout_s", 30.0
            )
        start = time.monotonic()
        self._gdb.send_mi("-exec-continue", timeout_s=5.0)
        self.target_halted = False
        self._gdb_believes_running = True
        stop = self._gdb.wait_for_stopped(timeout_s=timeout_s)
        duration = time.monotonic() - start
        if stop is None:
            # Bring target back to known state. -exec-interrupt is the
            # right mechanism here — gdb knows the target is running.
            try:
                self._gdb.send_mi("-exec-interrupt", timeout_s=5.0)
                self.target_halted = True
                self._gdb_believes_running = False
            except GDBError:
                pass
            return RunResult(
                breakpoint_hit=False,
                breakpoint=None,
                target_halted=self.target_halted,
                halt_reason="timeout",
                duration_s=duration,
            )

        self.target_halted = True
        self._gdb_believes_running = False
        bp = (
            self._breakpoints.get(stop.breakpoint_number)
            if stop.breakpoint_number is not None
            else None
        )
        reason_map = {
            "breakpoint-hit": "breakpoint",
            "signal-received": "signal",
            "exited-normally": "exited",
            "exited-signalled": "exited",
            "exited": "exited",
        }
        return RunResult(
            breakpoint_hit=stop.reason == "breakpoint-hit",
            breakpoint=bp,
            target_halted=True,
            halt_reason=reason_map.get(stop.reason, "unknown"),  # type: ignore[arg-type]
            duration_s=duration,
        )

    def read_variable(self, name: str) -> VariableValue:
        """``-data-evaluate-expression <name>`` (expression MI-quoted per IMP-15)."""
        self._require_halted()
        record = self._gdb.send_mi(
            f"-data-evaluate-expression {mi_quote(name)}", timeout_s=5.0
        )
        value = parse_evaluate_expression(record)
        # Inject the name (parser doesn't know it).
        from dataclasses import replace

        return replace(value, name=name)

    def compare_variable(
        self,
        name: str,
        expected: int | str,
        *,
        mask: int | None = None,
    ) -> ComparisonResult:
        """DBG-004 — read the variable, compare against ``expected``
        (with optional bitmask)."""
        observed = self.read_variable(name)
        matches = _compare(observed.integer_value, observed.raw, expected, mask)
        return ComparisonResult(
            name=name,
            observed=observed.integer_value if observed.integer_value is not None else observed.raw,
            expected=expected,
            mask=mask,
            matches=matches,
            raw=observed,
        )

    def compare_register(
        self,
        name: str,
        expected: int,
        *,
        mask: int | None = None,
    ) -> ComparisonResult:
        """DBG-005 — read the named CPU register, compare against ``expected``."""
        dump = self.read_registers()
        observed = dump.values.get(name)
        if observed is None:
            raise GDBError(
                message=f"register {name!r} not present in dump",
                gdb_marker="register-not-in-dump",
                hint=f"available: {sorted(dump.values.keys())}",
            )
        matches = _compare(observed, None, expected, mask)
        return ComparisonResult(
            name=name,
            observed=observed,
            expected=expected,
            mask=mask,
            matches=matches,
            register_raw=observed,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_halted(self) -> None:
        if not self.target_halted:
            raise TargetNotHalted(
                message="method requires target_halted=True",
                gdb_marker="target-not-halted",
                hint="halt the target with session.halt() before retrying",
                recoverable=True,
                target_state="running",
            )

    def _capture_disasm_around_pc(self) -> str:
        """Run ``disassemble`` near the current PC; return verbatim output.

        v1 simple-now: use ``disassemble /m $pc-32, $pc+32`` (about
        ±8 instructions). gdb returns the disassembly on the console
        stream so we route through ``send_console`` and join.
        """
        try:
            lines = self._gdb.send_console(
                "disassemble /m $pc-32, $pc+32", timeout_s=5.0
            )
            return "".join(lines)
        except GDBError as ex:
            self._log.warning("disassembly capture failed: %s", ex.message)
            return ""

    def _device_name_hint(self) -> str:
        """Best-effort device-name for SVD lookup.

        Prefers the descriptor's chip (``ctx.project.board.mcu`` — e.g.
        ``STM32L476RGTx``); ``SvdDb`` strips the ordering-code suffix to
        the family SVD (``STM32L476.svd``). Falls back to the ELF
        filename stem only when no descriptor chip is set — that heuristic
        breaks for real projects whose artifact is named after the
        application (``GPIO_IOToggle.elf``, ``BLINKY.elf``), not the chip,
        so the descriptor is the reliable source when present.

        TODO: probe the target's ``DBGMCU.IDCODE`` register / cached
        CubeProgrammer banner for the definitive device when neither
        source resolves.
        """
        descriptor = self.ctx.project
        board = getattr(descriptor, "board", None) if descriptor else None
        mcu = getattr(board, "mcu", None) if board else None
        if mcu:
            return str(mcu)
        return self.elf_path.stem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compare(
    observed_int: int | None,
    observed_raw: str | None,
    expected: int | str,
    mask: int | None,
) -> bool:
    """Compare observed (int or string) against expected with optional mask.

    - Numeric expected + numeric observed → int compare (mask-aware).
    - String expected → string compare against the raw rendering.
    """
    if isinstance(expected, int) and observed_int is not None:
        if mask is not None:
            return (observed_int & mask) == (expected & mask)
        return observed_int == expected
    if isinstance(expected, str) and observed_raw is not None:
        return observed_raw.strip() == expected.strip()
    return False


def _render_hex_dump(data: bytes, *, start_address: str) -> str:
    """Canonical width-16 hex + ASCII (mirrors cubeprogrammer.parsers)."""
    if not data:
        return ""
    try:
        base = int(start_address, 16)
    except ValueError:
        base = 0
    width = 16
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(width * 3 - 1)
        ascii_part = "".join(
            chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk
        )
        lines.append(f"0x{base + offset:08x}: {hex_part}  |{ascii_part}|")
    return "\n".join(lines) + "\n"
