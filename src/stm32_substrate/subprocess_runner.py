"""Single subprocess helper used by every tool wrapper.

Per the API conventions § "Process management — ``run_tool()``" and
M-017 (no silent retries). Centralises timeout, signal handling, output
capture, logging. Bare ``subprocess.run`` calls are not allowed inside
business-logic modules — they all funnel through here.

Signature follows the v1 spec:

    run_tool(binary, args, *, ctx, timeout_s=None, cwd=None, stdin=None,
             log_path=None, raise_on_nonzero=True) -> ToolRunResult

Behaviour:

- Logs argv at INFO before launching; logs exit code + duration_s at INFO
  on return. Full stdout / stderr at DEBUG.
- Optional ``log_path`` writes captured stdout + stderr to the named file
  after the subprocess exits (one of the v1 success_signal contracts —
  see ``BuildResult.log_path``).
- On ``subprocess.TimeoutExpired``: terminate the child cleanly (SIGTERM,
  grace, then SIGKILL), then raise ``ToolError(timed_out=True)``.
- On non-zero exit: raise ``ToolError`` by default; callers that expect
  non-zero pass ``raise_on_nonzero=False`` (e.g. cubeprogrammer error-code
  classification, build steps that capture failures via ``BuildResult``).
- ``on_progress`` line-streaming is TODO; v1 simple-now (M-018) returns
  full stdout / stderr from ``Popen.communicate``.

The ``KeyboardInterrupt`` rule from the spec: substrate propagates the
exception after terminating the child, so callers see ``KeyboardInterrupt``
rather than a half-captured ``ToolRunResult``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from stm32_substrate.errors import ToolError

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext

_log = logging.getLogger("stm32_substrate.subprocess_runner")

# Grace period before SIGKILL when terminating a child after timeout. Short
# by HIL design — long waits violate M-019.
_TIMEOUT_GRACE_S = 0.5


@dataclass(frozen=True)
class ToolRunResult:
    """Captured outcome of a single vendor-tool invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


def run_tool(
    binary: Path,
    args: Sequence[str],
    *,
    ctx: "SubstrateContext",
    timeout_s: float | None = None,
    cwd: Path | None = None,
    stdin: str | None = None,
    log_path: Path | None = None,
    raise_on_nonzero: bool = True,
) -> ToolRunResult:
    """Run ``binary`` with ``args``; return the captured outcome.

    Raises ``ToolError`` on subprocess timeout or on non-zero exit when
    ``raise_on_nonzero`` is True. Callers wrap the raised ``ToolError``
    into a per-tool subclass with the right ``<tool>_marker`` field.

    Args:
        binary: validated path to the executable.
        args: argument list (no shell expansion).
        ctx: substrate context (used for logger only today; future:
             default timeout knobs from ``ctx.defaults``).
        timeout_s: hard timeout. ``None`` means no timeout — only safe for
                   ``--version`` / ``--help`` probes; production calls must
                   pass an explicit value.
        cwd: working directory for the subprocess.
        stdin: text written to the child's stdin then closed; ``None`` for
               no stdin.
        log_path: if set, the captured stdout + stderr are written here
                  after exit (used by ``BuildResult.log_path``).
        raise_on_nonzero: when True (default), non-zero exit raises
                          ``ToolError``; when False, the result is returned
                          as-is and the caller inspects ``exit_code``.
    """
    argv = [str(binary), *args]
    _log.info("run_tool argv=%s timeout_s=%s cwd=%s", argv, timeout_s, cwd)

    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )

    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired as ex:
        timed_out = True
        _terminate(proc)
        stdout = _coerce_partial(ex.stdout)
        stderr = _coerce_partial(ex.stderr)
    except KeyboardInterrupt:
        _terminate(proc)
        raise

    duration_s = time.monotonic() - start
    exit_code = proc.returncode if proc.returncode is not None else -1

    _log.info(
        "run_tool exit code=%s duration_s=%.3f timed_out=%s",
        exit_code,
        duration_s,
        timed_out,
    )
    _log.debug("run_tool stdout (%s bytes):\n%s", len(stdout), stdout)
    _log.debug("run_tool stderr (%s bytes):\n%s", len(stderr), stderr)

    if log_path is not None:
        _write_log(log_path, argv, exit_code, duration_s, timed_out, stdout, stderr)

    result = ToolRunResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_s=duration_s,
        timed_out=timed_out,
    )

    if timed_out:
        raise ToolError(
            message=f"{binary.name} timed out after {timeout_s}s",
            code="timeout",
            tool_output=_join_for_error(stdout, stderr),
            hint="raise the timeout knob or check the device responsiveness",
            recoverable=False,
        )
    if exit_code != 0 and raise_on_nonzero:
        raise ToolError(
            message=f"{binary.name} exited with code {exit_code}",
            code=exit_code,
            tool_output=_join_for_error(stdout, stderr),
            hint="inspect the captured stderr for the vendor diagnostic",
            recoverable=False,
        )
    return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate ``proc`` with the standard SIGTERM-then-SIGKILL grace.

    Uses ``Popen.terminate`` / ``Popen.kill`` (subprocess's own signalling)
    rather than ``os.kill`` directly, satisfying the ADR-005 rule that
    business-logic code does not import ``signal``.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=_TIMEOUT_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=_TIMEOUT_GRACE_S)
        except subprocess.TimeoutExpired:
            _log.warning("subprocess pid=%s did not die after SIGKILL", proc.pid)
    except ProcessLookupError:
        return


def _coerce_partial(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _join_for_error(stdout: str, stderr: str) -> str:
    if not stdout and not stderr:
        return ""
    if not stderr:
        return stdout
    if not stdout:
        return stderr
    return f"{stdout}\n--- stderr ---\n{stderr}"


def _write_log(
    log_path: Path,
    argv: list[str],
    exit_code: int,
    duration_s: float,
    timed_out: bool,
    stdout: str,
    stderr: str,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# argv: {argv}\n"
        f"# exit_code: {exit_code}\n"
        f"# duration_s: {duration_s:.3f}\n"
        f"# timed_out: {timed_out}\n"
        f"# --- stdout ---\n"
    )
    log_path.write_text(header + stdout + "\n# --- stderr ---\n" + stderr, encoding="utf-8")
