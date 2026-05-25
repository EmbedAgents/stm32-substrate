"""``arm-none-eabi-gdb`` MI3 client wrapper.

Per the debug API spec § "gdb.py — MI3 client wrapper". One outstanding
command at a time; async records (``*running`` / ``*stopped`` / etc.)
queued for later consumption by ``wait_for_stopped``.

Public surface:

- ``GDBClient`` — long-lived subprocess wrapper.
- ``spawn_gdb(...)`` — high-level entry: spawn + initial connect-remote.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from stm32_substrate.debug.parsers import parse_mi_record
from stm32_substrate.debug.results import (
    MIAsyncRecord,
    MIResultRecord,
    MIStreamRecord,
    StoppedNotification,
)
from stm32_substrate.debug.parsers import parse_stopped
from stm32_substrate.errors import GDBError, GDBSessionLost

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext


class GDBClient:
    """Long-lived arm-none-eabi-gdb subprocess in MI3 mode.

    Single-threaded: each ``send_mi()`` writes a tagged command to stdin,
    reads stdout line-by-line until the matching ``^done`` / ``^error``
    arrives, and queues any async records seen during the read.
    ``wait_for_stopped()`` drains the queue first then reads more lines.

    Hook parameters (``_spawn`` / ``_now`` / ``_sleep``) let unit tests
    inject deterministic doubles.
    """

    def __init__(
        self,
        *,
        proc: subprocess.Popen,
        ctx: "SubstrateContext",
        _now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._proc = proc
        self._ctx = ctx
        self._now = _now
        self._log = ctx.logger.getChild("debug.gdb")
        self._token = 0
        self._async_queue: deque[MIAsyncRecord] = deque()
        self._stream_buffer: list[MIStreamRecord] = []
        self._closed = False
        self.pid = proc.pid

    # ------------------------------------------------------------------
    # send_mi — one outstanding command at a time
    # ------------------------------------------------------------------

    def send_mi(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
    ) -> MIResultRecord:
        """Write ``<token><command>`` then read until ``^class`` with
        matching token. Returns the result record.

        Async records seen during the read are pushed onto
        ``_async_queue`` for ``wait_for_stopped`` to consume later.
        Stream records (``~`` / ``@`` / ``&``) are kept in
        ``_stream_buffer`` so callers that follow up with
        ``send_console`` can see what gdb emitted.

        Raises:
          - ``GDBError(gdb_marker="command-timeout")`` if ``timeout_s``
            elapses; substrate sends ``-exec-interrupt`` to recover.
          - ``GDBSessionLost`` if gdb exits or its stdout closes.
          - ``GDBError(gdb_marker="protocol-violation")`` if no record
            matches the token (shouldn't happen under normal gdb).
        """
        self._require_alive()
        self._token += 1
        token = self._token
        line = f"{token}-{command}\n" if not command.startswith("-") else f"{token}{command}\n"
        self._write_line(line)

        deadline = self._now() + timeout_s if timeout_s is not None else None
        while True:
            if deadline is not None and self._now() >= deadline:
                self._interrupt_silently()
                raise GDBError(
                    message=(
                        f"gdb command {command!r} did not return within "
                        f"{timeout_s}s"
                    ),
                    gdb_marker="command-timeout",
                    hint="raise the per-call timeout or check target liveness",
                )
            line = self._read_line(deadline=deadline)
            if line is None:
                continue
            record = parse_mi_record(line)
            if record is None:
                continue
            if isinstance(record, MIResultRecord) and record.token == token:
                return record
            self._classify_other_record(record)

    # ------------------------------------------------------------------
    # send_console — for raw `monitor X` and similar
    # ------------------------------------------------------------------

    def send_console(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
    ) -> list[str]:
        """Run an MI ``-interpreter-exec console`` wrapper and return the
        accumulated ~stream / @stream output lines.

        Substrate uses this for ``monitor reset`` / ``monitor halt`` and
        anything else that doesn't have a first-class MI verb. Returns
        the captured stream-record texts in order.
        """
        # Drain any pre-existing stream records so this call's output
        # is the only thing in the buffer.
        self._stream_buffer.clear()
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        self.send_mi(
            f'-interpreter-exec console "{escaped}"', timeout_s=timeout_s
        )
        out = [rec.text for rec in self._stream_buffer if rec.stream in ("console", "target")]
        self._stream_buffer.clear()
        return out

    # ------------------------------------------------------------------
    # wait_for_stopped — drain async queue, then read more
    # ------------------------------------------------------------------

    def wait_for_stopped(
        self, *, timeout_s: float
    ) -> StoppedNotification | None:
        """Return the next ``*stopped`` notification, or ``None`` on
        timeout. Drains the queue first then reads new lines."""
        # Drain queue.
        while self._async_queue:
            rec = self._async_queue.popleft()
            if rec.class_ == "stopped":
                return parse_stopped(rec)

        # Then read fresh.
        deadline = self._now() + timeout_s
        while self._now() < deadline:
            line = self._read_line(deadline=deadline)
            if line is None:
                continue
            record = parse_mi_record(line)
            if record is None:
                continue
            if isinstance(record, MIAsyncRecord) and record.class_ == "stopped":
                return parse_stopped(record)
            # Anything else gets queued or buffered as usual.
            self._classify_other_record(record)
        return None

    # ------------------------------------------------------------------
    # close
    # ------------------------------------------------------------------

    def close(self, *, exit_grace_s: float = 2.0, kill_grace_s: float = 5.0) -> None:
        """Polite ``-gdb-exit`` then SIGTERM then SIGKILL. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._proc.poll() is not None:
            return
        # Polite -gdb-exit (no timeout; if gdb is unresponsive we fall
        # through to SIGTERM).
        try:
            self._write_line("-gdb-exit\n")
        except (OSError, GDBSessionLost):
            pass
        try:
            self._proc.wait(timeout=exit_grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=kill_grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        except ProcessLookupError:
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_alive(self) -> None:
        if self._proc.poll() is not None:
            raise GDBSessionLost(
                message="gdb subprocess exited before command",
                gdb_marker="remote-connection-closed",
                gdb_exit_code=self._proc.returncode,
            )

    def _write_line(self, text: str) -> None:
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(text)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as ex:
            raise GDBSessionLost(
                message=f"gdb stdin broken: {ex}",
                gdb_marker="remote-connection-closed",
            ) from ex

    def _read_line(self, *, deadline: float | None) -> str | None:
        """Read one line from gdb stdout. Returns the line on success,
        ``None`` when there's nothing immediately available (caller loops
        and re-checks the deadline). Raises ``GDBSessionLost`` on EOF."""
        try:
            assert self._proc.stdout is not None
            line = self._proc.stdout.readline()
        except (OSError, ValueError) as ex:
            raise GDBSessionLost(
                message=f"gdb stdout read failed: {ex}",
                gdb_marker="remote-connection-closed",
            ) from ex
        if line == "" or line is None:
            # EOF.
            raise GDBSessionLost(
                message="gdb stdout closed unexpectedly",
                gdb_marker="remote-connection-closed",
                gdb_exit_code=self._proc.poll(),
            )
        return line

    def _classify_other_record(
        self, record: MIResultRecord | MIAsyncRecord | MIStreamRecord
    ) -> None:
        if isinstance(record, MIAsyncRecord):
            self._async_queue.append(record)
        elif isinstance(record, MIStreamRecord):
            self._stream_buffer.append(record)
        # else: MIResultRecord with non-matching token — uncommon; drop.

    def _interrupt_silently(self) -> None:
        """Send ``-exec-interrupt`` without waiting for the response.

        Used after a command timeout to keep gdb in a known state.
        Swallows errors — caller already raised a ``command-timeout``.
        """
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write("99999-exec-interrupt\n")
            self._proc.stdin.flush()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Spawn helper
# ---------------------------------------------------------------------------


def spawn_gdb(
    *,
    ctx: "SubstrateContext",
    elf_path: Path,
    gdb_port: int,
    _spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> GDBClient:
    """Spawn ``arm-none-eabi-gdb --interpreter=mi3 --quiet <elf>`` and
    connect it to the gdbserver at ``localhost:<gdb_port>``.

    Returns a ``GDBClient`` after the initial ``-target-select extended-remote``
    completes successfully.
    """
    log = ctx.logger.getChild("debug.gdb")
    gdb_bin = ctx.tools.arm_gdb
    if gdb_bin is None:
        raise GDBError(
            message="arm-none-eabi-gdb path not configured",
            gdb_marker="gdbserver-spawn-failed",  # generic spawn failure marker
            hint=(
                "Set debug.arm_gdb in .claude/stm32-tools.local.jsonc or "
                "ARM_NONE_EABI_GDB env var."
            ),
        )

    argv = [
        str(gdb_bin),
        "--interpreter=mi3",
        "--quiet",
        str(elf_path),
    ]
    log.info("spawning arm-gdb argv=%s", argv)
    try:
        proc = _spawn(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            bufsize=1,
        )
    except (OSError, FileNotFoundError) as ex:
        raise GDBError(
            message=f"arm-gdb spawn failed: {ex}",
            gdb_marker="gdbserver-spawn-failed",
            hint="check that arm-none-eabi-gdb path exists and is executable",
        ) from ex

    client = GDBClient(proc=proc, ctx=ctx)
    # Connect to gdbserver. extended-remote keeps the session alive on
    # detach (DBG-003-style attach) without restarting the subprocess.
    result = client.send_mi(
        f"-target-select extended-remote localhost:{gdb_port}",
        timeout_s=10.0,
    )
    if result.class_ == "error":
        client.close()
        raise GDBError(
            message=(
                f"gdb could not connect to gdbserver on port {gdb_port}: "
                f"{result.fields.get('msg', '?')}"
            ),
            gdb_marker="remote-connection-closed",
            hint=(
                "check that gdbserver is listening on the expected port "
                "and the probe is not held by another tool"
            ),
        )
    return client
