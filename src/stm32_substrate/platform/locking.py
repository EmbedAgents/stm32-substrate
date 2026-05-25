"""Exclusive file locking — Linux: ``fcntl.flock``; Windows: ``msvcrt.locking``.

Both implementations use non-blocking semantics. ``acquire_exclusive_lock``
raises ``BlockingIOError`` immediately on contention rather than waiting —
the HIL principle (M-019) forbids long waits. Callers (e.g. cubeide)
translate to their tool-specific error type.

Per ADR-007 (supersedes ADR-005), Linux + Windows are both first-class in
v1. ``fcntl`` and ``msvcrt`` imports are guarded by ``sys.platform`` so
the unused module is never imported on the wrong OS.
"""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def acquire_exclusive_lock(path: Path) -> AbstractContextManager[None]:
    """Non-blocking exclusive lock; raises ``BlockingIOError`` on contention.

    Creates the lock file if it does not exist. The lock is released when
    the context exits — the file is left on disk (cheap; OS releases the
    advisory lock automatically when the fd closes).
    """
    if sys.platform == "win32":
        return _windows_lock(path)
    return _linux_lock(path)


def is_lock_held(path: Path) -> bool:
    """Return ``True`` when another holder has an exclusive lock on ``path``.

    Probe is non-destructive: opens the file, attempts a non-blocking
    exclusive lock, immediately releases on success. If the file does not
    exist, returns ``False`` (nothing to hold).
    """
    if sys.platform == "win32":
        return _windows_is_held(path)
    return _linux_is_held(path)


# ---------------------------------------------------------------------------
# Linux implementation
# ---------------------------------------------------------------------------


@contextmanager
def _linux_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = path.open("a+")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
            raise
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def _linux_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    fd = path.open("a+")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fd.close()


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------


@contextmanager
def _windows_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``msvcrt.locking`` requires a non-empty region to lock; ensure the
    # file has at least one byte so ``LK_NBLCK`` has something to grab.
    if not path.exists() or path.stat().st_size == 0:
        with path.open("ab") as seed:
            seed.write(b"\0")
    fd = path.open("r+b")
    try:
        fd.seek(0)
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fd.close()
            raise BlockingIOError(
                f"lock contention on {path}: {exc}"
            ) from exc
        try:
            yield
        finally:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                # Best-effort unlock; closing the fd releases the lock too.
                pass
    finally:
        fd.close()


def _windows_is_held(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    fd = path.open("r+b")
    try:
        fd.seek(0)
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return False
    finally:
        fd.close()
