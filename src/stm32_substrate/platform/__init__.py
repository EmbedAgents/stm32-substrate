"""OS-specific operation wrappers.

Per ADR-007 (supersedes ADR-005), ``os.kill`` / ``signal`` / ``fcntl`` /
``msvcrt`` / ``ctypes.windll`` must NOT appear outside this subpackage.
Call sites import the wrapper, never the underlying primitive. v1 ships
Linux and Windows; macOS is deferred pending marketplace demand.

Public surface:

- ``acquire_exclusive_lock(path)`` — non-blocking exclusive lock context
  manager (raises ``BlockingIOError`` on contention).
- ``is_lock_held(path)`` — non-destructive probe; ``True`` when another
  holder has the exclusive lock.
- ``process_alive(pid)`` — liveness probe; never raises.
- ``terminate_process(pid, grace_s)`` — grace then forcible kill. On
  Linux this is SIGTERM → SIGKILL; on Windows there is no SIGTERM
  equivalent so the grace window is a natural-exit wait followed by
  ``TerminateProcess``.
- ``terminate_process_tree(pid, grace_s=)`` — same ladder applied to the
  whole process tree (Linux: ``killpg`` on the ``start_new_session``
  group; Windows: ``taskkill /T``). For vendor bootstrap launchers whose
  JVM child does the real work.
"""

from __future__ import annotations

from stm32_substrate.platform.locking import (
    acquire_exclusive_lock,
    is_lock_held,
)
from stm32_substrate.platform.process import (
    process_alive,
    terminate_process,
    terminate_process_tree,
)

__all__ = [
    "acquire_exclusive_lock",
    "is_lock_held",
    "process_alive",
    "terminate_process",
    "terminate_process_tree",
]
