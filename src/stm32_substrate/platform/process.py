"""Process liveness + termination wrappers.

- **Linux** — ``os.kill(pid, 0)`` for liveness; ``SIGTERM`` then ``SIGKILL``
  for terminate with grace.
- **Windows** — ``OpenProcess + GetExitCodeProcess`` for liveness;
  ``WaitForSingleObject(grace_ms)`` then ``TerminateProcess`` for terminate.
  Windows has no ``SIGTERM`` equivalent (no API that all processes honour
  for a polite exit). The grace window covers the case where the caller
  has already initiated shutdown via the subprocess's own protocol (e.g.
  gdb ``-gdb-exit`` or CubeMX ``exit_mx``) and the process is in the
  middle of exiting cleanly. If the process is still alive at the end of
  grace, ``TerminateProcess`` hard-kills it.

Per ADR-007 (supersedes ADR-005), Linux + Windows are both first-class
in v1. ``signal`` / ``msvcrt`` / ``ctypes.windll`` imports are guarded by
``sys.platform`` so the unused module is never imported on the wrong OS.
"""

from __future__ import annotations

import os
import sys
import time

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
else:
    import signal


def process_alive(pid: int) -> bool:
    """Return ``True`` if a process with ``pid`` exists; never raises.

    Linux: ``os.kill(pid, 0)`` — ``ProcessLookupError`` → dead;
    ``PermissionError`` → alive (we lack permission to signal, but the
    process exists). Any other ``OSError`` → ``False`` (defensive).

    Windows: ``OpenProcess`` with ``PROCESS_QUERY_LIMITED_INFORMATION``;
    handle open + ``GetExitCodeProcess`` returns ``STILL_ACTIVE`` ⇒ alive.
    Handle open fails ⇒ dead or inaccessible — treat as dead.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_alive(pid)
    return _linux_alive(pid)


def terminate_process(pid: int, grace_s: float) -> None:
    """Terminate ``pid`` with a grace window; never raises on missing PID.

    Linux: ``SIGTERM`` → poll up to ``grace_s`` → ``SIGKILL`` if still alive.

    Windows: wait up to ``grace_s`` for natural exit (the caller may have
    already asked the subprocess to shut down) → ``TerminateProcess`` if
    still alive. No SIGTERM equivalent on Windows.

    Returns when the process is gone or after the grace + brief kill wait.
    No-op if the process is already dead.
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        _windows_terminate(pid, grace_s)
        return
    _linux_terminate(pid, grace_s)


# ---------------------------------------------------------------------------
# Linux implementation
# ---------------------------------------------------------------------------


def _linux_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _linux_terminate(pid: int, grace_s: float) -> None:
    if not _linux_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    poll_interval_s = 0.05
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not _linux_alive(pid):
            return
        time.sleep(poll_interval_s)

    if _linux_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        kill_deadline = time.monotonic() + 1.0
        while time.monotonic() < kill_deadline:
            if not _linux_alive(pid):
                return
            time.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------

# Win32 constants
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF


def _windows_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # OpenProcess fails for a non-existent or fully-reaped PID.
        return False
    try:
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        # TODO(v1+): disambiguate the STILL_ACTIVE=259 collision (a process
        # that genuinely exited with code 259 looks alive here). Requires
        # SYNCHRONIZE access for WaitForSingleObject — would need a second
        # OpenProcess call. Substrate's PIDs (gdbserver, CubeMX, etc.) are
        # not expected to exit with code 259, so the simple check suffices.
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_terminate(pid: int, grace_s: float) -> None:
    if not _windows_alive(pid):
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(
        _PROCESS_TERMINATE | _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD

        grace_ms = int(max(0.0, grace_s) * 1000)
        wait_result = kernel32.WaitForSingleObject(handle, grace_ms)
        if wait_result == _WAIT_OBJECT_0:
            return

        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess(handle, 1)
        kernel32.WaitForSingleObject(handle, 1000)
    finally:
        kernel32.CloseHandle(handle)
