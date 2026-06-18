"""Unit tests for ``embedagents.stm32.platform`` wrappers.

Process tests use ``subprocess`` to spawn a short-lived python interpreter as
the test subject. Lock-contention tests use the same pattern: a sibling
process holds the lock; the parent observes contention.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from embedagents.stm32.platform import (
    acquire_exclusive_lock,
    is_lock_held,
    process_alive,
    terminate_process,
    user_cache_root,
)


# ---------------------------------------------------------------------------
# user_cache_root — per-OS persistent cache base (RES-050)
# ---------------------------------------------------------------------------


class TestUserCacheRoot:
    def test_suffix_is_stm32_substrate(self) -> None:
        assert user_cache_root().name == "stm32-substrate"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX base")
    def test_honors_xdg_cache_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert user_cache_root() == tmp_path / "stm32-substrate"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX base")
    def test_falls_back_to_dot_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert user_cache_root() == tmp_path / ".cache" / "stm32-substrate"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows base")
    def test_honors_localappdata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert user_cache_root() == tmp_path / "stm32-substrate"


# ---------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------


class TestAcquireExclusiveLock:
    def test_acquire_creates_file_and_releases(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "subdir" / "lock"
        assert not lock_path.exists()
        with acquire_exclusive_lock(lock_path):
            assert lock_path.exists()
        # File survives on disk; OS-level lock is released.
        assert lock_path.exists()
        assert is_lock_held(lock_path) is False

    def test_acquire_serialises_within_process(self, tmp_path: Path) -> None:
        """Within the same process, the wrapper releases cleanly on exit so
        a subsequent acquisition succeeds."""
        lock_path = tmp_path / "lock"
        with acquire_exclusive_lock(lock_path):
            pass
        with acquire_exclusive_lock(lock_path):
            pass

    def test_acquire_raises_when_sibling_holds_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        holder = _spawn_lock_holder(lock_path, hold_seconds=3.0)
        try:
            _wait_until(lambda: is_lock_held(lock_path), timeout=2.0)
            with pytest.raises(BlockingIOError):
                with acquire_exclusive_lock(lock_path):
                    pass
        finally:
            terminate_process(holder.pid, grace_s=1.0)
            holder.wait(timeout=2)

    def test_lock_released_after_holder_exits(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        holder = _spawn_lock_holder(lock_path, hold_seconds=0.5)
        try:
            _wait_until(lambda: is_lock_held(lock_path), timeout=2.0)
            holder.wait(timeout=2)
            _wait_until(lambda: not is_lock_held(lock_path), timeout=2.0)
            with acquire_exclusive_lock(lock_path):
                pass
        finally:
            if holder.poll() is None:
                terminate_process(holder.pid, grace_s=1.0)
                holder.wait(timeout=2)


class TestIsLockHeld:
    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert is_lock_held(tmp_path / "nope") is False

    def test_unheld_file_returns_false(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        lock_path.touch()
        assert is_lock_held(lock_path) is False

    def test_held_file_returns_true(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "lock"
        holder = _spawn_lock_holder(lock_path, hold_seconds=3.0)
        try:
            _wait_until(lambda: is_lock_held(lock_path), timeout=2.0)
            assert is_lock_held(lock_path) is True
        finally:
            terminate_process(holder.pid, grace_s=1.0)
            holder.wait(timeout=2)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX record locks")
    def test_posix_record_lock_held_returns_true(self, tmp_path: Path) -> None:
        """IMP-07: Java NIO FileLock (the CubeIDE GUI's workspace lock)
        maps to POSIX record locks on Linux — an independent namespace
        from flock. The flock-only probe never saw it, so the GUI-held
        pre-check was dead and cleanup could delete a live workspace's
        metadata."""
        lock_path = tmp_path / ".lock"
        lock_path.touch()  # Eclipse's .lock is zero-byte
        script = f"""
import fcntl, time, pathlib
f = pathlib.Path({str(lock_path)!r}).open('a+')
fcntl.lockf(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("LOCKED", flush=True)
time.sleep(10)
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "LOCKED"
            assert is_lock_held(lock_path) is True
        finally:
            terminate_process(holder.pid, grace_s=1.0)
            holder.wait(timeout=2)
        _wait_until(lambda: not is_lock_held(lock_path), timeout=2.0)


class TestTerminateProcessTree:
    def test_kills_grandchild(self) -> None:
        """IMP-16/IMP-08: signalling only the direct child orphans the
        JVM grandchild that does the real work. The tree kill must take
        the whole group."""
        from embedagents.stm32.platform import terminate_process_tree

        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n"
            "p.wait()\n"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert leader.stdout is not None
        grandchild_pid = int(leader.stdout.readline().strip())
        assert process_alive(grandchild_pid)

        terminate_process_tree(leader.pid, grace_s=0.5)
        leader.wait(timeout=3)
        _wait_until(lambda: not process_alive(grandchild_pid), timeout=3.0)


# ---------------------------------------------------------------------------
# process
# ---------------------------------------------------------------------------


class TestProcessAlive:
    def test_returns_false_for_zero_pid(self) -> None:
        assert process_alive(0) is False

    def test_returns_false_for_negative_pid(self) -> None:
        assert process_alive(-1) is False

    def test_returns_true_for_self(self) -> None:
        import os as _os

        assert process_alive(_os.getpid()) is True

    def test_returns_true_for_live_child(self) -> None:
        proc = _spawn_sleeper(seconds=5.0)
        try:
            assert process_alive(proc.pid) is True
        finally:
            terminate_process(proc.pid, grace_s=1.0)
            proc.wait(timeout=2)

    def test_returns_false_after_child_exits(self) -> None:
        proc = _spawn_sleeper(seconds=0.1)
        proc.wait(timeout=2)
        _wait_until(lambda: not process_alive(proc.pid), timeout=2.0)
        assert process_alive(proc.pid) is False


class TestTerminateProcess:
    @pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM semantics are Linux-only")
    def test_sigterm_within_grace_linux(self) -> None:
        """A python sleeper exits via SIGTERM during the grace period.

        Exit-code check is the load-bearing assertion. ``terminate_process``
        polls via ``os.kill(pid, 0)`` which still reports zombies as alive
        until the parent reaps, so elapsed time is not a useful signal.
        """
        import signal as _signal

        proc = _spawn_sleeper(seconds=10.0)
        terminate_process(proc.pid, grace_s=2.0)
        proc.wait(timeout=3)
        assert process_alive(proc.pid) is False
        assert proc.returncode == -_signal.SIGTERM, (
            f"expected SIGTERM exit, got returncode={proc.returncode}"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL-after-SIGTERM is Linux-only")
    def test_sigkill_when_sigterm_ignored_linux(self, tmp_path: Path) -> None:
        """A SIGTERM-ignoring process is killed after the grace period."""
        import signal as _signal

        ready_path = tmp_path / "child-ready"
        proc = _spawn_sigterm_ignorer(ready_path)
        _wait_until(ready_path.exists, timeout=2.0)
        terminate_process(proc.pid, grace_s=0.3)
        proc.wait(timeout=3)
        assert process_alive(proc.pid) is False
        assert proc.returncode == -_signal.SIGKILL, (
            f"expected SIGKILL exit, got returncode={proc.returncode}"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="TerminateProcess semantics are Windows-only")
    def test_terminate_process_windows(self) -> None:
        """Windows has no SIGTERM equivalent — ``TerminateProcess`` after the
        grace window. A sleeping child is killed; exit code is the
        ``uExitCode`` argument we pass (1)."""
        proc = _spawn_sleeper(seconds=10.0)
        terminate_process(proc.pid, grace_s=0.3)
        proc.wait(timeout=3)
        assert process_alive(proc.pid) is False
        assert proc.returncode == 1, (
            f"expected TerminateProcess exit=1, got returncode={proc.returncode}"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows grace-period exit semantics")
    def test_natural_exit_during_grace_windows(self) -> None:
        """If the child exits naturally during the grace window,
        ``terminate_process`` returns without invoking TerminateProcess."""
        proc = _spawn_sleeper(seconds=0.2)
        terminate_process(proc.pid, grace_s=3.0)
        proc.wait(timeout=3)
        assert process_alive(proc.pid) is False
        # Natural exit code is 0; TerminateProcess would have set it to 1.
        assert proc.returncode == 0, (
            f"expected natural-exit returncode=0, got {proc.returncode}"
        )

    def test_already_dead_is_noop(self) -> None:
        proc = _spawn_sleeper(seconds=0.05)
        proc.wait(timeout=2)
        terminate_process(proc.pid, grace_s=1.0)  # must not raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn_lock_holder(lock_path: Path, hold_seconds: float) -> subprocess.Popen:
    """Spawn a sibling python process that holds an exclusive lock on
    ``lock_path`` for ``hold_seconds`` seconds, then exits.

    Dispatches the per-OS lock primitive — ``fcntl.flock`` on Linux,
    ``msvcrt.locking`` on Windows. The script seeds the file with a byte
    on Windows because ``msvcrt.locking`` needs a non-empty region.
    """
    if sys.platform == "win32":
        script = f"""
import msvcrt, time, pathlib
p = pathlib.Path({str(lock_path)!r})
p.parent.mkdir(parents=True, exist_ok=True)
if not p.exists() or p.stat().st_size == 0:
    with p.open('ab') as seed:
        seed.write(b'\\0')
f = p.open('r+b')
f.seek(0)
msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
time.sleep({hold_seconds!r})
"""
    else:
        script = f"""
import fcntl, time, pathlib
p = pathlib.Path({str(lock_path)!r})
p.parent.mkdir(parents=True, exist_ok=True)
f = p.open('a+')
fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
time.sleep({hold_seconds!r})
"""
    return subprocess.Popen([sys.executable, "-c", script])


def _spawn_sleeper(seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds!r})"]
    )


def _spawn_sigterm_ignorer(ready_path: Path) -> subprocess.Popen:
    """Spawn a child that ignores SIGTERM and touches ``ready_path`` once
    the handler is installed. Linux-only — Windows has no SIGTERM."""
    if sys.platform == "win32":
        raise RuntimeError(
            "_spawn_sigterm_ignorer is Linux-only; SIGTERM has no Windows "
            "equivalent. Gate Windows test paths with skipif."
        )
    script = f"""
import signal, time, pathlib
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path({str(ready_path)!r}).touch()
time.sleep(60)
"""
    return subprocess.Popen([sys.executable, "-c", script])


def _wait_until(predicate, timeout: float, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"predicate did not become true within {timeout:.2f}s")
