#!/usr/bin/env python3
"""Capture real arm-none-eabi-gdb MI3 records into the F-MI fixture corpus.

Per ``tests/fixtures/debug/fixture-spec.md`` § "gdb-MI record fixtures"
(TST-01): population strategy is *mostly recorded* — run arm-gdb in MI3
mode against an F-PROJ project on the bench and capture the raw records;
synthetic captures only for error shapes hard to force (F-MI-MALFORMED,
optimized-out).

Usage (bench, NUCLEO-L476RG attached, BLINKY.elf built):

    .venv/bin/python tools/capture-mi-records.py

Writes one ``<name>.mi`` file per capture under
``tests/fixtures/debug/mi-records/F-MI-*/``. File format per the fixture
spec: ``#``-prefixed comment lines carry the MI command (and provenance);
every non-comment line is one verbatim MI record as emitted by gdb.
Existing files are overwritten (re-capture is the point of the script).

The gdbserver is spawned through the substrate (port fallback, -cp arg);
arm-gdb is driven directly over stdin/stdout so the *raw* line stream can
be recorded before any substrate parsing touches it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from embedagents.stm32.context import SubstrateContext  # noqa: E402
from embedagents.stm32.debug.gdbserver import (  # noqa: E402
    GDBServerOptions,
    spawn_gdbserver,
)
from embedagents.stm32.debug.pipereader import PipeLineReader  # noqa: E402

FPROJ = REPO / "tests/fixtures/projects/F-PROJ-NUCLEO-L476RG"
BLINKY_ELF = (
    FPROJ
    / "Projects/NUCLEO-L476RG/Examples/GPIO/BLINKY/STM32CubeIDE/Debug/BLINKY.elf"
)
OUT_ROOT = REPO / "tests/fixtures/debug/mi-records"

CAPTURE_STAMP = "captured 2026-06-11, arm-none-eabi-gdb MI3, NUCLEO-L476RG, BLINKY.elf"


class RawMI:
    """Minimal raw-line MI driver: token-stamped writes, verbatim reads.

    Reads through the substrate's ``PipeLineReader`` daemon thread —
    ``select()`` on a buffered TextIO stalls when lines already sit in
    Python's internal buffer, and a bare ``readline()`` blocks past any
    deadline (the A-011 bug class).
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.token = 0
        assert proc.stdout is not None
        self.reader = PipeLineReader(proc.stdout, name="capture-mi")

    def send(
        self, command: str, *, settle_s: float = 0.0, timeout_s: float = 10.0
    ) -> tuple[int, list[str]]:
        """Write ``<token><command>``; read verbatim lines until the
        matching ``<token>^`` result record (+ optional settle window for
        trailing async records). Returns (token, lines)."""
        self.token += 1
        token = self.token
        assert self.proc.stdin is not None
        self.proc.stdin.write(f"{token}{command}\n")
        self.proc.stdin.flush()
        lines = self._read_until(lambda l: l.startswith(f"{token}^"), timeout_s)
        if settle_s:
            lines += self._read_for(settle_s)
        return token, lines

    def _read_until(self, pred, timeout_s: float) -> list[str]:
        deadline = time.monotonic() + timeout_s
        out: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = self.reader.read_line(timeout_s=0.1)
            except EOFError:
                break
            if line is None:
                continue
            line = line.rstrip("\r\n")
            if line.strip() == "(gdb)" or not line:
                continue
            out.append(line)
            if pred(line):
                return out
        raise TimeoutError(f"no matching record within {timeout_s}s; got {out!r}")

    def _read_for(self, duration_s: float) -> list[str]:
        """Drain whatever arrives within ``duration_s`` (async records)."""
        deadline = time.monotonic() + duration_s
        out: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = self.reader.read_line(
                    timeout_s=max(deadline - time.monotonic(), 0.05)
                )
            except EOFError:
                break
            if line is None:
                break
            line = line.rstrip("\r\n")
            if line and line.strip() != "(gdb)":
                out.append(line)
        return out


def write_fixture(fixture: str, name: str, command: str, lines: list[str]) -> None:
    d = OUT_ROOT / fixture
    d.mkdir(parents=True, exist_ok=True)
    body = [f"# {command}", f"# {CAPTURE_STAMP}"] + lines
    (d / f"{name}.mi").write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"  {fixture}/{name}.mi  ({len(lines)} record line(s))")


def pick(lines: list[str], prefix: str) -> list[str]:
    return [l for l in lines if l.startswith(prefix)]


def main() -> int:
    if not BLINKY_ELF.is_file():
        print(f"BLINKY.elf not built at {BLINKY_ELF}", file=sys.stderr)
        return 1

    ctx = SubstrateContext.from_environment(project_path=FPROJ)
    options = GDBServerOptions(
        port=61234,
        cube_programmer_cli_dir=ctx.tools.cube_programmer_cli.parent,
        halt_on_attach=True,
        persistent=True,
        stlink_serial=ctx.default_probe_sn,
    )
    gdbserver = spawn_gdbserver(ctx=ctx, options=options)
    print(f"gdbserver up on port {gdbserver.port}")

    proc = subprocess.Popen(
        [
            str(ctx.tools.arm_gdb),
            "--interpreter=mi3",
            "--quiet",
            str(BLINKY_ELF),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    mi = RawMI(proc)

    try:
        # mi-async on BEFORE connecting: in sync mode gdb stops reading
        # MI input while the target runs — -exec-interrupt (halt) would
        # never be processed.
        mi.send("-gdb-set mi-async on")
        _, connect_lines = mi.send(
            f"-target-select extended-remote localhost:{gdbserver.port}",
            settle_s=0.3,
            timeout_s=30.0,
        )
        # Plain `monitor reset`: ST-LINK gdbserver halts at Reset_Handler
        # while attached (RES-041; `reset halt` is OpenOCD syntax → ^error).
        mi.send('-interpreter-exec console "monitor reset"')

        # ---- F-MI-RESULT-DONE ------------------------------------------
        for name, cmd in [
            ("register-names", "-data-list-register-names"),
            ("register-values", "-data-list-register-values x"),
            ("break-insert", "-break-insert main"),
            ("stack-list-frames", "-stack-list-frames --no-frame-filters"),
            ("thread-info", "-thread-info"),
        ]:
            _, lines = mi.send(cmd)
            write_fixture("F-MI-RESULT-DONE", name, cmd, pick(lines, f"{mi.token}^done"))
        mi.send("-break-delete")

        # ---- F-MI-MEMORY-READ ------------------------------------------
        for name, cmd in [
            ("word-aligned-4b", "-data-read-memory-bytes 0x20000000 4"),
            ("block-64b", "-data-read-memory-bytes 0x20000000 64"),
            # High flash page on the 1MB L476 — erased flash reads 0xFF
            # (the suspicious_unmapped shape).
            ("erased-flash-ff", "-data-read-memory-bytes 0x080FF000 16"),
        ]:
            _, lines = mi.send(cmd)
            write_fixture("F-MI-MEMORY-READ", name, cmd, pick(lines, f"{mi.token}^done"))

        # ---- F-MI-DATA-EVALUATE ----------------------------------------
        for name, cmd in [
            ("integer", "-data-evaluate-expression uwTick"),
            ("struct", '-data-evaluate-expression "*(GPIO_TypeDef *)0x48000000"'),
            ("array", '-data-evaluate-expression "*(unsigned int (*)[4])0x20000000"'),
        ]:
            _, lines = mi.send(cmd)
            write_fixture("F-MI-DATA-EVALUATE", name, cmd, pick(lines, f"{mi.token}^done"))
        # Variable-not-in-scope error belongs to this fixture per spec.
        _, lines = mi.send("-data-evaluate-expression no_such_variable_xyz")
        write_fixture(
            "F-MI-DATA-EVALUATE",
            "not-in-scope-error",
            "-data-evaluate-expression no_such_variable_xyz",
            pick(lines, f"{mi.token}^error"),
        )

        # ---- F-MI-RESULT-ERROR -----------------------------------------
        _, lines = mi.send("-break-insert no_such_function_xyz")
        write_fixture(
            "F-MI-RESULT-ERROR",
            "bad-breakpoint-location",
            "-break-insert no_such_function_xyz",
            pick(lines, f"{mi.token}^error"),
        )
        _, lines = mi.send("-data-evaluate-expression no_such_variable_xyz")
        write_fixture(
            "F-MI-RESULT-ERROR",
            "expression-evaluation",
            "-data-evaluate-expression no_such_variable_xyz",
            pick(lines, f"{mi.token}^error"),
        )

        # ---- F-MI-STREAM-CONSOLE ---------------------------------------
        _, lines = mi.send('-interpreter-exec console "info registers pc"')
        write_fixture(
            "F-MI-STREAM-CONSOLE",
            "info-registers",
            '-interpreter-exec console "info registers pc"',
            [l for l in lines if l.startswith(("~", "@", "&"))]
            + pick(lines, f"{mi.token}^done"),
        )

        # ---- F-MI-ASYNC-RUNNING + target-running ^error -----------------
        _, lines = mi.send("-exec-continue", settle_s=0.4)
        write_fixture(
            "F-MI-ASYNC-RUNNING",
            "exec-continue",
            "-exec-continue",
            pick(lines, f"{mi.token}^running") + pick(lines, "*running"),
        )
        # ---- F-MI-ASYNC-STOPPED: signal-received (SIGINT) ---------------
        _, lines = mi.send("-exec-interrupt", settle_s=1.0)
        stopped = pick(lines, "*stopped")
        write_fixture(
            "F-MI-ASYNC-STOPPED",
            "signal-received-sigint",
            "-exec-interrupt  (interrupt a running target)",
            stopped,
        )

        # ---- F-MI-RESULT-ERROR: the RES-041 Rcmd rejection (real) -------
        # ST-LINK gdbserver rejects the OpenOCD `reset halt` form — the
        # exact error the substrate used to swallow pre-IMP-02.
        _, lines = mi.send(
            '-interpreter-exec console "monitor reset halt"', settle_s=0.3
        )
        err = pick(lines, f"{mi.token}^error")
        if err:
            write_fixture(
                "F-MI-RESULT-ERROR",
                "rcmd-protocol-error",
                'monitor reset halt  (OpenOCD form; ST-LINK gdbserver rejects — RES-041)',
                err,
            )
        # gdb gives NO reply to sync commands while the target runs (the
        # command queues until stop), so the target-not-halted shape is
        # synthetic per the GDB manual.
        write_fixture(
            "F-MI-RESULT-ERROR",
            "target-not-halted-synthetic",
            "SYNTHETIC — gdb queues sync commands while running; shape per GDB manual",
            ['31^error,msg="Cannot execute this command while the selected thread is running."'],
        )

        # ---- F-MI-ASYNC-STOPPED: breakpoint-hit + INTERLEAVED -----------
        # HAL_Delay runs continuously in BLINKY's loop — a breakpoint
        # there is hit within milliseconds of -exec-continue.
        mi.send("-break-insert HAL_Delay")
        token, lines = mi.send("-exec-continue", settle_s=2.0)
        write_fixture(
            "F-MI-ASYNC-STOPPED",
            "breakpoint-hit",
            "-break-insert HAL_Delay; -exec-continue",
            pick(lines, "*stopped"),
        )
        # The full verbatim transcript IS the interleaving fixture:
        # ^running, *running, then *stopped arriving asynchronously.
        write_fixture(
            "F-MI-INTERLEAVED",
            "continue-to-breakpoint",
            "-exec-continue  (with a breakpoint armed at HAL_Delay)",
            lines,
        )

        # ---- F-MI-ASYNC-STOPPED: end-stepping-range ---------------------
        _, lines = mi.send("-exec-next", settle_s=2.0)
        write_fixture(
            "F-MI-ASYNC-STOPPED",
            "end-stepping-range",
            "-exec-next  (stopped at the HAL_Delay breakpoint)",
            pick(lines, "*stopped"),
        )
        mi.send("-break-delete")

        # ---- Synthetic captures (spec: hard to force on bare metal) -----
        write_fixture(
            "F-MI-ASYNC-STOPPED",
            "exited-normally-synthetic",
            "SYNTHETIC — bare-metal targets never exit; shape per GDB manual §27.6",
            ['*stopped,reason="exited-normally"'],
        )
        write_fixture(
            "F-MI-DATA-EVALUATE",
            "optimized-out-synthetic",
            "SYNTHETIC — -O0 HAL builds keep locals; shape per GDB manual",
            ['99^done,value="<optimized out>"'],
        )
        write_fixture(
            "F-MI-MALFORMED",
            "truncated-result",
            "SYNTHETIC — truncated ^done mid-tuple (protocol-violation)",
            ['7^done,memory=[{begin="0x20000000",contents="dead'],
        )
        write_fixture(
            "F-MI-MALFORMED",
            "unterminated-string",
            "SYNTHETIC — kv blob with an unbalanced bracket",
            ['8^done,stack=[frame={level="0",addr="0x08000'],
        )

        # ---- connect transcript doubles as a STREAM-CONSOLE sample ------
        streams = [l for l in connect_lines if l.startswith(("~", "@", "&"))]
        if streams:
            write_fixture(
                "F-MI-STREAM-CONSOLE",
                "target-select-chatter",
                "-target-select extended-remote localhost:<port>",
                streams,
            )

        mi.send('-interpreter-exec console "monitor reset"')
        proc.stdin.write("-gdb-exit\n")
        proc.stdin.flush()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        gdbserver.close()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
