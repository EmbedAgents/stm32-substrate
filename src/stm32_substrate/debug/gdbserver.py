"""``ST-LINK_gdbserver`` subprocess lifecycle.

Spawn → port handshake → run → terminate. Per ``v1/debug-api.md``
§ "gdbserver.py". Pattern-matches the gdbserver's "Waiting for debugger
connection on port N..." stderr line to confirm the listener is up;
substrate then hands the port to ``arm-none-eabi-gdb`` over the
``target extended-remote`` channel.

Public surface:

- ``GDBServerOptions`` — spawn args.
- ``GDBServerProcess`` — long-lived subprocess handle with ``close()``.
- ``spawn_gdbserver(...)`` — high-level entry point with port-fallback
  loop and typed error mapping.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING, Sequence

from stm32_substrate.debug.pipereader import PipeLineReader
from stm32_substrate.errors import GDBError

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext


# Listener-ready pattern emitted by ST-LINK_gdbserver once it's bound
# the requested port. Wording drifts across builds:
#   - Linux gdbserver (older): "Waiting for debugger connection on port N"
#   - Windows gdbserver v7.13.0: "Waiting for debugger connection..."
#     (no port - the port is announced earlier in the banner as
#     "Listen Port Number : N").
# The port capture group is therefore optional; when absent, callers
# fall back to ``options.port`` (the requested port).
_LISTENER_READY_RE = re.compile(
    r"Waiting for debugger connection(?:\s+on\s+(?:port|TCP port)\s+(\d+))?",
    re.IGNORECASE,
)

# Error patterns from gdbserver stderr → typed marker.
_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"address already in use", re.IGNORECASE), "port-busy"),
    (re.compile(r"port.*in use", re.IGNORECASE), "port-busy"),
    (re.compile(r"no st-?link", re.IGNORECASE), "probe-not-found"),
    (re.compile(r"no debug probe detected", re.IGNORECASE), "probe-not-found"),
    (re.compile(r"could not find.*probe", re.IGNORECASE), "probe-not-found"),
)


@dataclass(frozen=True)
class GDBServerOptions:
    """Spawn-time options for ``GDBServerProcess``."""

    port: int
    cube_programmer_cli_dir: Path
    halt_on_attach: bool
    persistent: bool = True
    stlink_serial: str | None = None
    n6_dev_mode: bool = False


class GDBServerProcess:
    """Long-lived gdbserver subprocess. Created via ``spawn_gdbserver``."""

    def __init__(
        self,
        *,
        proc: subprocess.Popen,
        port: int,
        reader: PipeLineReader | None = None,
    ) -> None:
        self._proc = proc
        self.pid = proc.pid
        self.port = port
        self._closed = False
        # Keep the handshake's drain thread alive for the process
        # lifetime: it keeps emptying the merged-output pipe so a chatty
        # gdbserver can't fill the OS pipe buffer and stall.
        self._reader = reader

    def close(self, *, grace_s: float = 3.0) -> int | None:
        """SIGTERM → grace → SIGKILL. Returns the final exit code (or
        None on already-dead)."""
        if self._closed:
            return self._proc.returncode
        self._closed = True
        if self._proc.poll() is not None:
            return self._proc.returncode
        try:
            self._proc.terminate()
            try:
                return self._proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                pass
            self._proc.kill()
            try:
                return self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                logging.getLogger("stm32_substrate.debug.gdbserver").warning(
                    "gdbserver pid=%s did not die after SIGKILL", self._proc.pid
                )
                return None
        except ProcessLookupError:
            return None

    def poll(self) -> int | None:
        return self._proc.poll()


def spawn_gdbserver(
    *,
    ctx: "SubstrateContext",
    options: GDBServerOptions,
    handshake_timeout_s: float = 5.0,
    _spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    _now: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
) -> GDBServerProcess:
    """Spawn one gdbserver instance + wait for the listener-ready line.

    Raises ``GDBError`` with typed ``gdb_marker`` on each failure path:

    - ``"gdbserver-spawn-failed"`` — binary missing / OSError on spawn.
    - ``"port-busy"`` — gdbserver's stderr indicates the port is taken.
    - ``"probe-not-found"`` — no ST-LINK enumerated.
    - ``"command-timeout"`` — handshake didn't complete inside
      ``handshake_timeout_s``.

    Hook parameters (``_spawn`` / ``_now`` / ``_sleep``) let unit tests
    drive the handshake deterministically.
    """
    log = ctx.logger.getChild("debug.gdbserver")
    bin_path = ctx.tools.stlink_gdbserver
    if bin_path is None:
        raise GDBError(
            message="ST-LINK_gdbserver path not configured",
            gdb_marker="gdbserver-spawn-failed",
            hint=(
                "Set debug.stlink_gdbserver in .claude/stm32-tools.local.jsonc "
                "or STM32_STLINK_GDBSERVER env var. The binary ships alongside "
                "STM32CubeIDE."
            ),
        )

    argv = _build_argv(bin_path, options)
    log.info("spawning gdbserver argv=%s", argv)

    try:
        proc = _spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Merge stderr into stdout so the listener-ready announcement
            # is seen regardless of which stream gdbserver picks. Linux
            # historically wrote it to stderr; Windows v7.13.0 writes it
            # to stdout. Merging keeps the readline loop OS-agnostic.
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            bufsize=1,
        )
    except (OSError, FileNotFoundError) as ex:
        raise GDBError(
            message=f"gdbserver spawn failed: {ex}",
            gdb_marker="gdbserver-spawn-failed",
            hint="check that the gdbserver path exists and is executable",
        ) from ex

    # Read the merged stream line-by-line until we see the listener-
    # ready pattern, an error pattern, or the subprocess exits. The
    # stream is drained on a daemon thread (PipeLineReader) — a bare
    # readline() here blocked forever on a live-but-silent gdbserver,
    # making the handshake deadline dead code (IMP-13).
    deadline = _now() + handshake_timeout_s
    output_buffer: list[str] = []
    bound_port: int | None = None

    assert proc.stdout is not None
    reader = PipeLineReader(proc.stdout, name=f"gdbserver-{proc.pid}")
    stream_eof = False
    while _now() < deadline:
        if proc.poll() is not None:
            # Subprocess exited before announcing — drain remaining
            # output to inspect the error message.
            output_buffer.append(_drain_remaining(reader))
            joined = "".join(output_buffer)
            marker = _classify_stderr(joined)
            raise GDBError(
                message=(
                    f"gdbserver exited before listener-ready "
                    f"(exit_code={proc.returncode}): {joined.strip()[:200]}"
                ),
                gdb_marker=marker,
                gdbserver_exit_code=proc.returncode,
                tool_output=joined,
                hint=_hint_for_marker(marker),
                recoverable=marker == "port-busy",
            )

        if stream_eof:
            # Stream closed but the process is still alive (per the
            # poll above) — keep waiting for exit or the deadline.
            _sleep(0.05)
            continue
        try:
            line = reader.read_line(timeout_s=0.05)
        except (EOFError, OSError, ValueError):
            stream_eof = True
            continue
        if not line:
            _sleep(0.0)  # quantum elapsed inside read_line
            continue
        output_buffer.append(line)

        m = _LISTENER_READY_RE.search(line)
        if m:
            captured = m.group(1)
            if captured:
                # Linux gdbserver announces "on port N"; trust that
                # number in case gdbserver remapped from options.port.
                try:
                    bound_port = int(captured)
                except (TypeError, ValueError):
                    bound_port = options.port
            else:
                # Windows gdbserver v7.13.0 emits no port in the ready
                # line - fall back to the requested port.
                bound_port = options.port
            log.info(
                "gdbserver pid=%s listening on port %s", proc.pid, bound_port
            )
            return GDBServerProcess(proc=proc, port=bound_port, reader=reader)

        # Watch for early-error patterns even while the subprocess is
        # still running — some gdbserver builds emit the error then hang
        # the listener instead of exiting.
        marker = _classify_stderr(line)
        if marker is not None:
            # Tear it down and raise.
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                proc.kill()
            raise GDBError(
                message=(
                    f"gdbserver output indicated {marker}: {line.strip()[:200]}"
                ),
                gdb_marker=marker,
                tool_output="".join(output_buffer),
                hint=_hint_for_marker(marker),
                recoverable=marker == "port-busy",
            )

    # Handshake timed out without the listener line or an error.
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        proc.kill()
    raise GDBError(
        message=(
            f"gdbserver did not announce listener within "
            f"{handshake_timeout_s}s"
        ),
        gdb_marker="command-timeout",
        tool_output="".join(output_buffer),
        hint=(
            "raise debug.gdbserver_spawn_timeout_s or check that the "
            "configured ST-LINK probe is enumerated and not held by "
            "another tool"
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_argv(bin_path: Path, options: GDBServerOptions) -> list[str]:
    """Per ``v1/debug-api.md`` § "Argv (Linux, v1)"."""
    argv: list[str] = [
        str(bin_path),
        "-d",
        "-p",
        str(options.port),
        "-cp",
        str(options.cube_programmer_cli_dir),
    ]
    if options.persistent:
        argv.append("-e")
    # `-g` (--attach): attach to a running target instead of the default
    # reset-and-halt. Used for halt=False, AND mandatory for the N6 —
    # the flashless STM32N6 boots from external memory and the gdbserver
    # can't reset-halt it ("Target not halted after reset" → exit 9). In
    # dev-mode boot the N6 must be attached as a running target. Verified
    # on bench 2026-05-24: with `-g` the gdbserver reaches listener-ready
    # on the STM32N6570-DK; without it it exits 9. (DBG-012 — resolves the
    # former "N6-specific gdbserver args" placeholder.)
    if (not options.halt_on_attach) or options.n6_dev_mode:
        argv.append("-g")
    if options.stlink_serial:
        argv.extend(["-i", options.stlink_serial])
    return argv


def _drain_remaining(reader: PipeLineReader, *, max_wait_s: float = 1.0) -> str:
    """Collect whatever the exited process left on its pipe."""
    parts: list[str] = []
    waited = 0.0
    while waited < max_wait_s:
        try:
            line = reader.read_line(timeout_s=0.05)
        except (EOFError, OSError, ValueError):
            break
        if line is None:
            waited += 0.05
            continue
        parts.append(line)
    return "".join(parts)


def _classify_stderr(text: str) -> str | None:
    for pattern, marker in _ERROR_PATTERNS:
        if pattern.search(text):
            return marker
    return None


def _hint_for_marker(marker: str | None) -> str | None:
    return {
        "port-busy": "another process is using that port; substrate will retry on the fallback range",
        "probe-not-found": (
            "no ST-LINK enumerated; check USB connection, or call "
            "cubeprogrammer.list_probes() to enumerate"
        ),
        "command-timeout": (
            "gdbserver started but didn't announce listener-ready; "
            "may indicate the probe is held by another tool"
        ),
        "gdbserver-spawn-failed": (
            "verify ST-LINK_gdbserver is installed and the configured "
            "path is correct"
        ),
    }.get(marker or "")
