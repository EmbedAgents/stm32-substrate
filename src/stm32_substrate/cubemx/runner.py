"""Async-completion running-loop for ``CubeMX.generate()``.

Per the CubeMX API spec § "Running-loop algorithm". Substrate spawns
``STM32CubeMX -q <script>`` and observes three external signals — the
marker file (``<output>/.cproject``), CubeMX's own log mtime, and the
subprocess exit — to decide success / failure. No log content is
parsed (wrapper-principle).

The runner exposes underscore-prefixed hook parameters (``_now``,
``_spawn``, ``_sleep``) so unit tests can inject deterministic
clocks + fake subprocesses without monkey-patching internals.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from stm32_substrate.cubemx.results import CubeMXResult, ProgressEvent

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.progress import ProgressCallback


@dataclass(frozen=True)
class RunnerPolicy:
    """Timing knobs assembled from ``ctx.defaults.cubemx.*``.

    Built by ``policy_from_ctx`` so the runner stays decoupled from
    SubstrateContext for testability.
    """

    initial_budget_s: float
    extension_delta_s: float
    max_extensions: int
    poll_interval_s: float
    liveness_threshold_s: float
    post_exit_grace_s: float
    cubemx_log_path: Path
    log_dir: Path


def policy_from_ctx(ctx: "SubstrateContext") -> RunnerPolicy:
    """Pull ``cubemx.*`` defaults off the SubstrateContext with fallbacks."""
    cubemx = getattr(ctx.defaults, "cubemx", None)

    def _knob(name: str, default: float | str) -> float | str:
        if cubemx is None:
            return default
        return getattr(cubemx, name, default)

    log_dir_raw = _knob("log_dir", str(ctx.cwd / ".stm32-substrate-workspace" / "logs"))
    cubemx_log_raw = _knob("log_path", str(Path.home() / ".stm32cubemx" / "STM32CubeMX.log"))

    return RunnerPolicy(
        initial_budget_s=float(_knob("long_call_s", 300)),
        extension_delta_s=float(_knob("long_call_extension_s", 60)),
        max_extensions=int(_knob("long_call_max_extensions", 3)),
        poll_interval_s=float(_knob("poll_interval_s", 2)),
        liveness_threshold_s=float(_knob("liveness_threshold_s", 10)),
        post_exit_grace_s=float(_knob("post_exit_grace_s", 3)),
        cubemx_log_path=Path(str(cubemx_log_raw)),
        log_dir=Path(str(log_dir_raw)),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_cubemx(
    *,
    launcher: Path,
    script_text: str,
    expected_marker: Path,
    output_dir: Path | None,
    ctx: "SubstrateContext",
    timeout_s: float | None = None,
    on_progress: "ProgressCallback | None" = None,
    _now: Callable[[], float] = time.monotonic,
    _spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    _sleep: Callable[[float], None] = time.sleep,
) -> CubeMXResult:
    """Spawn STM32CubeMX, poll for completion, return ``CubeMXResult``.

    The ``_now`` / ``_spawn`` / ``_sleep`` parameters are test hooks —
    they default to the real implementations and let unit tests replace
    them with deterministic stand-ins.
    """
    policy = policy_from_ctx(ctx)
    log = ctx.logger.getChild("cubemx.runner")

    initial_budget = float(timeout_s) if timeout_s is not None else policy.initial_budget_s

    # Pre-call state: did marker / log already exist?
    marker_existed = expected_marker.is_file()
    pre_marker_mtime = expected_marker.stat().st_mtime if marker_existed else 0.0
    cubemx_log_existed = policy.cubemx_log_path.is_file()
    pre_cubemx_log_mtime = (
        policy.cubemx_log_path.stat().st_mtime if cubemx_log_existed else 0.0
    )

    # Substrate's own log file (captures subprocess stdout + stderr).
    policy.log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = policy.log_dir / f"cubemx-{ts}.log"

    # Write the script to a temp file so the launcher can read it.
    script_file = _write_script(script_text, output_dir)

    log.info("launching %s -q %s", launcher, script_file)
    log_fh = log_path.open("w", encoding="utf-8")
    try:
        proc = _spawn(
            [str(launcher), "-q", str(script_file)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        log_fh.close()
        raise

    start = _now()
    deadline = start + initial_budget
    extensions_used = 0
    last_cubemx_log_mtime = pre_cubemx_log_mtime
    last_log_activity = start
    success = False
    timed_out = False
    terminated_after_marker = False

    try:
        while True:
            now = _now()
            elapsed = now - start

            if on_progress is not None:
                on_progress(
                    ProgressEvent(
                        stage="cubemx_running",
                        duration_s=elapsed,
                        deadline_s=deadline - start,
                        extensions_used=extensions_used,
                    )
                )

            # (a) Marker check.
            if _marker_appeared(
                expected_marker, marker_existed=marker_existed, pre_mtime=pre_marker_mtime
            ):
                success = True
                if proc.poll() is None:
                    log.warning("terminating subprocess after marker appearance")
                    _terminate_proc(proc)
                    terminated_after_marker = True
                break

            # (b) Log-activity tracking.
            if policy.cubemx_log_path.is_file():
                cur = policy.cubemx_log_path.stat().st_mtime
                if cur > last_cubemx_log_mtime:
                    last_cubemx_log_mtime = cur
                    last_log_activity = now

            # (c) Subprocess exit handling.
            if proc.poll() is not None:
                # No-op regen: marker exists pre + subprocess exit 0 → success.
                if proc.returncode == 0 and expected_marker.is_file():
                    success = True
                    break
                # Post-exit grace with log-activity extension.
                #
                # On Windows, ``STM32CubeMX.exe`` is a Java-launcher
                # bootstrap whose subprocess can exit before the JVM
                # child finishes writing the project tree (observed:
                # the substrate sees subprocess exit several seconds
                # before CubeMX's log shows "Exiting application").
                # A fixed-window grace is brittle here, so the loop
                # mirrors the main loop's liveness-based extension:
                # initial grace, then extend by ``extension_delta_s``
                # up to ``max_extensions`` times as long as CubeMX's
                # log file is still being updated within
                # ``liveness_threshold_s``.
                grace_deadline = _now() + policy.post_exit_grace_s
                grace_extensions = 0
                while True:
                    if expected_marker.is_file():
                        success = True
                        break
                    if policy.cubemx_log_path.is_file():
                        cur = policy.cubemx_log_path.stat().st_mtime
                        if cur > last_cubemx_log_mtime:
                            last_cubemx_log_mtime = cur
                            last_log_activity = _now()
                    if _now() >= grace_deadline:
                        if (
                            (_now() - last_log_activity)
                            <= policy.liveness_threshold_s
                            and grace_extensions < policy.max_extensions
                        ):
                            grace_deadline = _now() + policy.extension_delta_s
                            grace_extensions += 1
                            log.info(
                                "post-exit grace extended (%d/%d; "
                                "log still active)",
                                grace_extensions,
                                policy.max_extensions,
                            )
                        else:
                            break
                    _sleep(min(policy.poll_interval_s, 0.1))
                break

            # (d) Deadline check.
            if now >= deadline:
                if (
                    (now - last_log_activity) <= policy.liveness_threshold_s
                    and extensions_used < policy.max_extensions
                ):
                    deadline += policy.extension_delta_s
                    extensions_used += 1
                    log.info(
                        "budget extended by %.0fs (extension %d/%d; log activity detected)",
                        policy.extension_delta_s,
                        extensions_used,
                        policy.max_extensions,
                    )
                else:
                    timed_out = True
                    _terminate_proc(proc)
                    break

            _sleep(policy.poll_interval_s)

        # Compute final exit_code.
        exit_code: int | None
        if terminated_after_marker:
            exit_code = None  # signal-derived; not informative
        else:
            exit_code = proc.poll()
            if exit_code is None:
                # Subprocess still alive at the end of timeout — wait briefly.
                try:
                    exit_code = proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    exit_code = -1
    finally:
        log_fh.close()
        try:
            script_file.unlink()
        except OSError:
            pass

    duration_s = _now() - start
    cubemx_log_to_report: Path | None = (
        policy.cubemx_log_path if not success and policy.cubemx_log_path.is_file() else None
    )
    if success:
        log.info(
            "generate completed in %.1fs; output_dir=%s; extensions_used=%d",
            duration_s,
            output_dir,
            extensions_used,
        )

    return CubeMXResult(
        success=success,
        exit_code=exit_code,
        duration_s=duration_s,
        timed_out=timed_out,
        extensions_used=extensions_used,
        output_dir=output_dir,
        log_path=log_path,
        cubemx_log_path=cubemx_log_to_report,
        script_text=script_text,
        terminated_after_marker=terminated_after_marker,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _marker_appeared(
    marker: Path, *, marker_existed: bool, pre_mtime: float
) -> bool:
    """Return ``True`` when the marker should count as "appeared / regenerated"."""
    if not marker.is_file():
        return False
    if not marker_existed:
        return True
    return marker.stat().st_mtime > pre_mtime


def _terminate_proc(proc: subprocess.Popen) -> None:
    """SIGTERM → 0.5s grace → SIGKILL. Idempotent if already exited."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            logging.getLogger("stm32_substrate.cubemx.runner").warning(
                "cubemx subprocess pid=%s did not die after SIGKILL", proc.pid
            )
    except ProcessLookupError:
        return


def _write_script(text: str, output_dir: Path | None) -> Path:
    """Write the inline script to a stable tmp file the launcher can read."""
    base_dir: Path
    if output_dir is not None:
        base_dir = output_dir
        base_dir.mkdir(parents=True, exist_ok=True)
    else:
        base_dir = Path(tempfile.gettempdir())
    fd, path_str = tempfile.mkstemp(
        prefix="cubemx-script-", suffix=".txt", dir=str(base_dir)
    )
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")
    return Path(path_str)
