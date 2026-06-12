"""``stm32 debug`` CLI subcommand group — recipe-flow per RES-026 (2026-05-21).

Every ``stm32 debug ...`` invocation is one-shot: spawns a fresh
``ST-LINK_gdbserver`` + ``arm-none-eabi-gdb``, performs a complete
composed operation, tears down, emits JSON. No cross-invocation session
continuity — debug state never persists across CLI calls.

For stateful multi-step workflows (set N breakpoints, run, hit, inspect,
set more, continue), drop into Python against the ``DebugSession``
context manager. The CLI exists for Claude's fix-loop consumption
(B-021, DIAG-001/019/020, DBG-008/9/11, CP-003/007/013) and one-shot
human queries — not for interactive debugging (use CubeIDE's GUI).

Subcommand surface (per ``v1/debug-api.md`` § "CLI subcommand surface"):

- ``start ELF [--port] [--no-halt] [--n6-dev-mode]`` — lifecycle only:
  spawn + handshake + emit ``SessionHandle`` + tear down. DBG-001 /
  DBG-003 / DBG-012.
- ``svd-path DEVICE`` — pure lookup, no subprocess. D-008 input.
- ``check-variable --at LOC --var NAME --expected V [--mask M]`` —
  DBG-004 — start + set_breakpoint + run_until_breakpoint +
  compare_variable + close → ``ComparisonResult``.
- ``check-register --at LOC --reg NAME --expected V [--mask M]`` —
  DBG-005 — same with ``compare_register``.
- ``read-registers`` — DBG-006 — start (halted) + read CPU registers +
  close → ``RegisterDump``.
- ``read-peripheral NAME [INSTANCE]`` — DBG-007 — start (halted) +
  SVD-decoded peripheral dump + close → ``PeripheralDump``.
- ``read-memory --address 0x... --size N`` — start (halted) + read
  memory + close → ``MemoryReadResult``.
- ``callstack [--full]`` — start (halted) + callstack + close →
  ``CallStack``.
- ``snapshot [--include-peripheral NAME]...`` — DIAG-021 — start
  (halted) + composite snapshot + close → ``DebugSnapshot``.
- ``decode-hardfault`` — DIAG-001 gdb path — start (halted) + compose the
  raw fault bundle (SCB peripheral dump + registers + callstack) + close →
  ``DebugSnapshot``. Substrate composes; **Claude classifies the fault** —
  there is no decode rule in this module (HARD RULE 2 / ADR-004: substrate
  captures, doesn't interpret). Complement to cubeprogrammer's
  ``analyze_hardfault`` (the ``-hf`` binary path), which keeps its typed
  ``HardFaultDecode`` because that's parsing the vendor analyzer's own
  output, not composing a verdict, per M-012 dual-tool routing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from embedagents.stm32.cli._serialize import (
    dumps,
    serialise_error,
    serialise_unexpected,
)
from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.debug import Debug
from embedagents.stm32.debug.session import DebugSession
from embedagents.stm32.errors import GDBError, SubstrateError


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``debug`` group on the top-level parser."""
    parser = subparsers.add_parser(
        "debug",
        help="ST-LINK gdbserver + arm-none-eabi-gdb recipe subcommands.",
    )
    sub = parser.add_subparsers(
        dest="debug_subcommand",
        required=True,
        metavar="<subcommand>",
    )

    _add_start(sub)
    _add_svd_path(sub)
    _add_check_variable(sub)
    _add_check_register(sub)
    _add_read_registers(sub)
    _add_read_peripheral(sub)
    _add_read_memory(sub)
    _add_callstack(sub)
    _add_snapshot(sub)
    _add_decode_hardfault(sub)


def dispatch(args: argparse.Namespace) -> int:
    handler = args.debug_fn
    try:
        ctx = SubstrateContext.from_environment()
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    try:
        result = handler(args, ctx)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    if result is not None:
        sys.stdout.write(dumps(result, pretty=getattr(args, "pretty", False)) + "\n")
    return 0


# ---------------------------------------------------------------------------
# Parser registrars
# ---------------------------------------------------------------------------


def _add_session_args(p: argparse.ArgumentParser) -> None:
    """Add the common session-start flags shared by every recipe.

    ``elf`` is positional + optional (``nargs="?"``) so a recipe invoked
    from a project cwd can autodiscover the ELF from the descriptor per
    R-002. ``start`` keeps ``elf`` required for lifecycle clarity.
    """
    p.add_argument(
        "elf",
        nargs="?",
        type=Path,
        default=None,
        help="path to the ELF artifact (autodiscovered from descriptor if omitted)",
    )
    p.add_argument(
        "--port", type=int, default=None, help="gdb port (default 61234)"
    )
    p.add_argument(
        "--n6-dev-mode",
        action="store_true",
        help="N6 BOOT-switch dev-mode (DBG-012)",
    )


def _add_start(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "start",
        help="DBG-001 / DBG-003 / DBG-012 — spawn gdbserver + arm-gdb and emit the SessionHandle.",
    )
    p.add_argument(
        "elf",
        nargs="?",
        type=Path,
        default=None,
        help="path to the ELF artifact (autodiscovered from descriptor if omitted)",
    )
    p.add_argument("--port", type=int, default=None, help="gdb port (default 61234)")
    p.add_argument(
        "--no-halt",
        action="store_true",
        help="attach without halting (DBG-003 'attach running')",
    )
    p.add_argument(
        "--n6-dev-mode",
        action="store_true",
        help="N6 BOOT-switch dev-mode (DBG-012)",
    )
    p.set_defaults(debug_fn=_cmd_start)


def _add_svd_path(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "svd-path",
        help="D-008 input — resolve the SVD file path for a device name via ctx.svd_db.",
    )
    p.add_argument("device_name", help="banner device_name (e.g. STM32L476RG)")
    p.set_defaults(debug_fn=_cmd_svd_path)


def _add_check_variable(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "check-variable",
        help="DBG-004 — set breakpoint, run until hit, read variable, compare against expected.",
    )
    p.add_argument(
        "--at", dest="at_location", required=True,
        help="gdb location for set_breakpoint (e.g. main, main.c:84, *0x080012ac)",
    )
    p.add_argument(
        "--var", dest="var_name", required=True,
        help="variable name (passed to gdb -data-evaluate-expression)",
    )
    p.add_argument(
        "--expected", required=True,
        help="expected value (int hex/dec auto-parsed; otherwise string)",
    )
    p.add_argument(
        "--mask",
        type=lambda s: int(s, 0),
        default=None,
        help="optional bitmask applied before compare (hex/dec)",
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_check_variable)


def _add_check_register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "check-register",
        help="DBG-005 — set breakpoint, run until hit, read register, compare against expected.",
    )
    p.add_argument(
        "--at", dest="at_location", required=True,
        help="gdb location for set_breakpoint",
    )
    p.add_argument(
        "--reg", dest="reg_name", required=True,
        help="register name (CPU reg like r0/pc/sp/xpsr or peripheral.field)",
    )
    p.add_argument(
        "--expected",
        type=lambda s: int(s, 0),
        required=True,
        help="expected integer value (hex/dec)",
    )
    p.add_argument(
        "--mask",
        type=lambda s: int(s, 0),
        default=None,
        help="optional bitmask applied before compare (hex/dec)",
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_check_register)


def _add_read_registers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "read-registers",
        help="DBG-006 — read CPU registers from a halted target.",
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_read_registers)


def _add_read_peripheral(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "read-peripheral",
        help="DBG-007 — SVD-decoded peripheral dump from a halted target.",
    )
    p.add_argument("peripheral_name", help="peripheral name (e.g. RCC, GPIOA, USART1, SCB)")
    p.add_argument(
        "instance",
        nargs="?",
        default=None,
        help="optional instance suffix (e.g. SPI1 vs SPI2)",
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_read_peripheral)


def _add_read_memory(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "read-memory",
        help="Read N bytes from address against a halted target.",
    )
    p.add_argument("--address", required=True, help="start address (hex string)")
    p.add_argument(
        "--size", type=int, required=True, help="number of bytes to read"
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_read_memory)


def _add_callstack(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "callstack",
        help="gdb callstack of a halted target.",
    )
    p.add_argument(
        "--full",
        action="store_true",
        help="include per-frame function arguments",
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_callstack)


def _add_snapshot(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "snapshot",
        help="DIAG-021 — composite registers + callstack + named peripherals + disasm.",
    )
    p.add_argument(
        "--include-peripheral",
        action="append",
        default=None,
        dest="include_peripheral",
        help="peripheral name to include (repeatable; default: substrate's standard set)",
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_snapshot)


def _add_decode_hardfault(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "decode-hardfault",
        help=(
            "DIAG-001 gdb path — compose the raw SCB + registers + callstack "
            "bundle (a DebugSnapshot); Claude classifies the fault."
        ),
    )
    _add_session_args(p)
    p.set_defaults(debug_fn=_cmd_decode_hardfault)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    debug = Debug(ctx)
    session = debug.start_session(
        args.elf,
        halt=not args.no_halt,
        port=args.port,
        n6_dev_mode=args.n6_dev_mode,
        on_n6_boot_confirm=(_console_confirm if args.n6_dev_mode else None),
    )
    try:
        return session.session_handle()
    finally:
        session.close()


def _cmd_svd_path(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    svd_db = ctx.svd_db
    if svd_db is None:
        return {
            "device_name": args.device_name,
            "svd_path": None,
            "configured_sources": [],
        }
    path = svd_db.find_for(args.device_name)
    return {
        "device_name": args.device_name,
        "svd_path": str(path) if path else None,
        "configured_sources": list(svd_db.roots.configured()),
    }


def _cmd_check_variable(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    def op(session: DebugSession) -> Any:
        session.set_breakpoint(args.at_location)
        run = session.run_until_breakpoint()
        if not run.breakpoint_hit:
            raise GDBError(
                message=(
                    f"breakpoint at {args.at_location!r} was not hit within timeout"
                ),
                gdb_marker="breakpoint-not-hit",
                hint=(
                    "verify the location is reachable in normal execution, "
                    "or raise debug.breakpoint_wait_timeout_s"
                ),
            )
        if not session.target_halted:
            session.halt()
        return session.compare_variable(
            args.var_name, _parse_expected(args.expected), mask=args.mask
        )

    return _with_fresh_session(ctx, args, op, reset=True)


def _cmd_check_register(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    def op(session: DebugSession) -> Any:
        session.set_breakpoint(args.at_location)
        run = session.run_until_breakpoint()
        if not run.breakpoint_hit:
            raise GDBError(
                message=(
                    f"breakpoint at {args.at_location!r} was not hit within timeout"
                ),
                gdb_marker="breakpoint-not-hit",
                hint=(
                    "verify the location is reachable in normal execution, "
                    "or raise debug.breakpoint_wait_timeout_s"
                ),
            )
        if not session.target_halted:
            session.halt()
        return session.compare_register(
            args.reg_name, args.expected, mask=args.mask
        )

    return _with_fresh_session(ctx, args, op, reset=True)


def _cmd_read_registers(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    return _with_fresh_session(ctx, args, lambda s: s.read_registers())


def _cmd_read_peripheral(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    return _with_fresh_session(
        ctx,
        args,
        lambda s: s.read_peripheral(args.peripheral_name, args.instance),
    )


def _cmd_read_memory(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    return _with_fresh_session(
        ctx, args, lambda s: s.read_memory(args.address, args.size)
    )


def _cmd_callstack(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    return _with_fresh_session(ctx, args, lambda s: s.callstack(full=args.full))


def _cmd_snapshot(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    include = args.include_peripheral or None
    return _with_fresh_session(
        ctx, args, lambda s: s.snapshot(include_peripherals=include)
    )


def _cmd_decode_hardfault(args: argparse.Namespace, ctx: SubstrateContext) -> Any:
    # DIAG-001 gdb path: substrate COMPOSES the raw fault bundle (SCB
    # peripheral dump + registers + callstack, via snapshot), and Claude
    # CLASSIFIES the fault from it. No Cortex-M decode rule lives here —
    # HARD RULE 2 / ADR-004 (substrate captures, doesn't interpret). The
    # cubeprogrammer `-hf` path keeps its typed HardFaultDecode because that
    # parses the vendor analyzer's own output rather than composing a verdict.
    return _with_fresh_session(
        ctx, args, lambda s: s.snapshot(include_peripherals=["SCB"])
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _with_fresh_session(
    ctx: SubstrateContext,
    args: argparse.Namespace,
    fn: Callable[[DebugSession], Any],
    *,
    reset: bool = False,
) -> Any:
    """Spawn a one-shot DebugSession, run ``fn(session)``, tear down.

    All recipes go through this helper. ``elf`` is autodiscovered from
    the project descriptor when ``args.elf`` is None per R-002.

    ``reset=False`` (the read recipes): attach to the running target
    without reset (gdbserver ``-g``) and halt it in place — the state
    being read is the firmware's live state. ``halt=True`` would send
    ``monitor reset``, wiping the sticky fault registers (CFSR / HFSR
    clear on reset) and returning every peripheral to its power-on
    defaults — DIAG-001 then always saw a clean fault state (A-003).

    ``reset=True`` (check-variable / check-register): reset-and-halt so
    the breakpoint is armed before execution reaches the location —
    DBG-004/005 semantics require running *through* the program from
    the top.
    """
    debug = Debug(ctx)
    n6 = getattr(args, "n6_dev_mode", False)
    session = debug.start_session(
        getattr(args, "elf", None),
        halt=reset,
        port=getattr(args, "port", None),
        n6_dev_mode=n6,
        on_n6_boot_confirm=(_console_confirm if n6 else None),
    )
    try:
        if not reset and not session.target_halted:
            try:
                session.halt()
            except GDBError as ex:
                # Some gdb/gdbserver combos stop the core during the
                # attach itself; -exec-interrupt then errors. The reads
                # below still verify halt state and fail loud if the
                # target is genuinely running.
                if ex.gdb_marker != "command-error":
                    raise
                session.target_halted = True
        return fn(session)
    finally:
        session.close()


def _parse_expected(value: str) -> Any:
    """Coerce ``--expected`` from string to int when possible.

    ``compare_variable`` accepts ``int | str``. Strings that parse as
    hex/dec become ints; others pass through verbatim (string compare).
    """
    if value is None:
        return None
    try:
        return int(value, 0)
    except (TypeError, ValueError):
        return value


def _console_confirm() -> bool:
    """Prompt the user to confirm BOOT-switch position from the CLI.

    Returns ``True`` when the user types ``y`` / ``yes``. Used as the
    ``on_n6_boot_confirm`` callback when ``--n6-dev-mode`` is passed.
    """
    sys.stderr.write(
        "n6_dev_mode: confirm BOOT switch is in DEV position (y/N): "
    )
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
