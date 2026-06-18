"""C1b tests — cubeide/workspace.py."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeide import workspace
from embedagents.stm32.errors import WorkspaceLockedError


# ---------------------------------------------------------------------------
# detect_workspace_lock — uses platform.is_lock_held
# ---------------------------------------------------------------------------


class TestDetectWorkspaceLock:
    def test_no_metadata_dir(self, tmp_path: Path) -> None:
        assert workspace.detect_workspace_lock(tmp_path) is False

    def test_unheld_lock_returns_false(self, tmp_path: Path) -> None:
        meta = tmp_path / ".metadata"
        meta.mkdir()
        (meta / ".lock").touch()
        assert workspace.detect_workspace_lock(tmp_path) is False

    def test_held_lock_returns_true(self, tmp_path: Path) -> None:
        meta = tmp_path / ".metadata"
        meta.mkdir()
        lock_path = meta / ".lock"

        # Spawn a sibling process that holds the lock; verify probe sees it.
        proc = _spawn_lock_holder(lock_path, hold_seconds=3.0)
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if workspace.detect_workspace_lock(tmp_path):
                    break
                time.sleep(0.02)
            assert workspace.detect_workspace_lock(tmp_path) is True
        finally:
            proc.terminate()
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# detect_project_imported
# ---------------------------------------------------------------------------


class TestDetectProjectImported:
    def test_not_imported_returns_none(self, tmp_path: Path) -> None:
        assert workspace.detect_project_imported(tmp_path, "demo") is None

    def test_decodes_file_uri(self, tmp_path: Path) -> None:
        # Eclipse's .location blob — minimal shape: leading bytes, then
        # ``URI//file:<path>``, then control-byte trailer.
        loc = (
            tmp_path
            / ".metadata"
            / ".plugins"
            / "org.eclipse.core.resources"
            / ".projects"
            / "demo"
            / ".location"
        )
        loc.parent.mkdir(parents=True)
        # Construct a realistic-ish blob with a NUL trailer.
        path = "/home/anup/projects/demo"
        loc.write_bytes(
            b"\x40\x40\x00\x14\x10URI//file:" + path.encode() + b"\x00\x80"
        )
        result = workspace.detect_project_imported(tmp_path, "demo")
        assert result == Path(path)

    def test_handles_uri_encoded_spaces(self, tmp_path: Path) -> None:
        loc = (
            tmp_path
            / ".metadata"
            / ".plugins"
            / "org.eclipse.core.resources"
            / ".projects"
            / "demo"
            / ".location"
        )
        loc.parent.mkdir(parents=True)
        loc.write_bytes(b"x\x00file:/home/anup/my%20projects/demo\x00y")
        result = workspace.detect_project_imported(tmp_path, "demo")
        assert result == Path("/home/anup/my projects/demo")

    def test_decodes_windows_drive_uri(self, tmp_path: Path) -> None:
        """IMP-06: Eclipse on Windows writes ``file:/C:/...`` — the URI
        leading slash is not part of the path. Undecoded, the comparison
        against the descriptor path never matched and every build ran
        cleanup_stale_project, purging live workspace metadata."""
        loc = (
            tmp_path
            / ".metadata"
            / ".plugins"
            / "org.eclipse.core.resources"
            / ".projects"
            / "demo"
            / ".location"
        )
        loc.parent.mkdir(parents=True)
        loc.write_bytes(b"x\x00file:/C:/Users/dev/proj/demo\x00y")
        result = workspace.detect_project_imported(tmp_path, "demo")
        assert result == Path("C:/Users/dev/proj/demo")

    def test_decodes_triple_slash_windows_uri(self, tmp_path: Path) -> None:
        loc = (
            tmp_path
            / ".metadata"
            / ".plugins"
            / "org.eclipse.core.resources"
            / ".projects"
            / "demo"
            / ".location"
        )
        loc.parent.mkdir(parents=True)
        loc.write_bytes(b"x\x00file:///C:/ws/demo\x00y")
        result = workspace.detect_project_imported(tmp_path, "demo")
        assert result == Path("C:/ws/demo")

    def test_missing_file_marker_returns_none(self, tmp_path: Path) -> None:
        loc = (
            tmp_path
            / ".metadata"
            / ".plugins"
            / "x"
            / ".projects"
            / "demo"
            / ".location"
        )
        loc.parent.mkdir(parents=True)
        loc.write_bytes(b"garbage with no file URI")
        result = workspace.detect_project_imported(tmp_path, "demo")
        assert result is None


# ---------------------------------------------------------------------------
# cleanup_stale_project
# ---------------------------------------------------------------------------


class TestCleanupStaleProject:
    def test_no_state_is_noop(self, tmp_path: Path) -> None:
        workspace.cleanup_stale_project(tmp_path, "demo")

    def test_removes_project_tree(self, tmp_path: Path) -> None:
        proj = tmp_path / "demo"
        proj.mkdir()
        (proj / "src").mkdir()
        (proj / "src" / "main.c").write_text("")
        workspace.cleanup_stale_project(tmp_path, "demo")
        assert not proj.exists()

    def test_removes_all_plugin_projects_entries(self, tmp_path: Path) -> None:
        plugins = tmp_path / ".metadata" / ".plugins"
        for plugin in ("org.eclipse.core.resources", "another.plugin"):
            stale = plugins / plugin / ".projects" / "demo"
            stale.mkdir(parents=True)
            (stale / ".location").write_text("")
        workspace.cleanup_stale_project(tmp_path, "demo")
        assert not (plugins / "org.eclipse.core.resources" / ".projects" / "demo").exists()
        assert not (plugins / "another.plugin" / ".projects" / "demo").exists()

    def test_removes_unheld_metadata_lock(self, tmp_path: Path) -> None:
        meta = tmp_path / ".metadata"
        meta.mkdir()
        lock = meta / ".lock"
        lock.touch()
        workspace.cleanup_stale_project(tmp_path, "demo")
        assert not lock.exists()

    def test_other_projects_untouched(self, tmp_path: Path) -> None:
        other = tmp_path / "other-project"
        other.mkdir()
        plugins = tmp_path / ".metadata" / ".plugins"
        other_stale = plugins / "org.eclipse.core.resources" / ".projects" / "other-project"
        other_stale.mkdir(parents=True)
        (other_stale / ".location").write_text("")

        workspace.cleanup_stale_project(tmp_path, "demo")

        # Other project's state survives.
        assert other.exists()
        assert other_stale.exists()

    def test_warning_log_enumerates_deletions(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "demo").mkdir()
        with caplog.at_level(logging.WARNING):
            workspace.cleanup_stale_project(tmp_path, "demo")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings
        assert "demo" in warnings[0].message
        assert "removing" in warnings[0].message


# ---------------------------------------------------------------------------
# acquire_workspace_lock
# ---------------------------------------------------------------------------


class TestAcquireWorkspaceLock:
    def test_uncontended_succeeds(self, tmp_path: Path) -> None:
        with workspace.acquire_workspace_lock(tmp_path):
            pass

    def test_creates_metadata_dir(self, tmp_path: Path) -> None:
        with workspace.acquire_workspace_lock(tmp_path):
            assert (tmp_path / ".metadata" / ".substrate-lock").exists()

    def test_contended_raises_workspace_locked(self, tmp_path: Path) -> None:
        # Sibling process holds the substrate-lock.
        lock_path = tmp_path / ".metadata" / ".substrate-lock"
        lock_path.parent.mkdir(parents=True)
        proc = _spawn_lock_holder(lock_path, hold_seconds=3.0)
        try:
            # Wait for the sibling to actually hold the lock.
            from embedagents.stm32.platform import is_lock_held

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if is_lock_held(lock_path):
                    break
                time.sleep(0.02)
            with pytest.raises(WorkspaceLockedError) as excinfo:
                with workspace.acquire_workspace_lock(tmp_path):
                    pass
            err = excinfo.value
            assert err.workspace_path == tmp_path
            assert err.cubeide_marker == "workspace-locked"
            assert err.hint is not None
            assert "in progress" in err.hint or "wait" in err.hint
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    def test_sequential_calls_in_same_process(self, tmp_path: Path) -> None:
        """No deadlock on re-entry within the same process."""
        with workspace.acquire_workspace_lock(tmp_path):
            pass
        with workspace.acquire_workspace_lock(tmp_path):
            pass


# ---------------------------------------------------------------------------
# headless_log_path
# ---------------------------------------------------------------------------


class TestHeadlessLogPath:
    def test_default_follows_workspace(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        ws = tmp_path / "ws"
        path = workspace.headless_log_path(ctx, workspace=ws)
        assert path.parent == ws / "logs"
        assert path.name.startswith("build-")
        assert path.name.endswith(".log")
        assert path.parent.is_dir()

    def test_default_without_workspace_falls_back_under_cwd(
        self, tmp_path: Path
    ) -> None:
        # No workspace passed and no configured log_dir → legacy in-cwd path.
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        path = workspace.headless_log_path(ctx)
        assert path.parent == tmp_path / ".stm32-substrate-workspace" / "logs"

    def test_custom_log_dir_from_defaults(
        self, tmp_path: Path
    ) -> None:
        import json

        custom = tmp_path / "build-logs"
        defaults = {
            "version": 1,
            "cubeide": {"log_dir": str(custom)},
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        path = workspace.headless_log_path(ctx)
        assert path.parent == custom
        assert custom.is_dir()

    def test_timestamped_filenames_unique(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        p1 = workspace.headless_log_path(ctx)
        # Force a different second on the timestamp.
        time.sleep(1.05)
        p2 = workspace.headless_log_path(ctx)
        assert p1 != p2


# ---------------------------------------------------------------------------
# default_workspace_root / workspace_nested_in_project (RES-050)
# ---------------------------------------------------------------------------


class TestDefaultWorkspace:
    def test_out_of_tree_under_user_cache(self, tmp_path: Path) -> None:
        from embedagents.stm32.platform import user_cache_root

        proj = tmp_path / "STM32CubeIDE"
        proj.mkdir()
        ws = workspace.default_workspace_root(proj)
        # Lives under the per-OS user cache, never inside the project tree.
        assert user_cache_root() in ws.parents
        assert not workspace.workspace_nested_in_project(ws, proj)
        assert proj not in ws.parents

    def test_deterministic_and_project_keyed(self, tmp_path: Path) -> None:
        a = tmp_path / "a" / "STM32CubeIDE"
        b = tmp_path / "b" / "STM32CubeIDE"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        # Same project path → same workspace across calls (stable cache key).
        assert workspace.default_workspace_root(a) == workspace.default_workspace_root(a)
        # Different project paths (same basename) → distinct workspaces.
        assert workspace.default_workspace_root(a) != workspace.default_workspace_root(b)

    def test_name_segment_is_basename_and_hash(self, tmp_path: Path) -> None:
        proj = tmp_path / "STM32CubeIDE"
        proj.mkdir()
        name = workspace.default_workspace_root(proj).name
        assert name.startswith("STM32CubeIDE-")
        # 8 lowercase-hex digest suffix.
        suffix = name.rsplit("-", 1)[1]
        assert len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix)


class TestWorkspaceNesting:
    def test_descendant_is_nested(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        assert workspace.workspace_nested_in_project(proj / ".ws", proj)
        assert workspace.workspace_nested_in_project(proj, proj)  # equal

    def test_sibling_not_nested(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        # A sibling whose name shares a prefix must NOT count as nested.
        assert not workspace.workspace_nested_in_project(tmp_path / "proj-ws", proj)
        assert not workspace.workspace_nested_in_project(tmp_path / "other", proj)

    def test_case_insensitive_match_on_normcase(self) -> None:
        # Logic-level proof that the compare goes through os.path.normcase:
        # on a case-insensitive OS the two spellings collapse; on a
        # case-sensitive OS they stay distinct. Either way the helper must
        # agree with normcase rather than do a raw case-sensitive compare.
        import os

        proj = Path("/Tmp/Proj")
        ws = Path("/tmp/proj/.ws")
        expected = os.path.normcase("/tmp/proj/.ws").startswith(
            os.path.normcase("/Tmp/Proj") + os.sep
        )
        assert workspace.workspace_nested_in_project(ws, proj) == expected


# ---------------------------------------------------------------------------
# CubeIDE._resolve_workspace — default + explicit build.workspace (RES-050)
# ---------------------------------------------------------------------------


def _ctx_with_workspace(root: Path, configured: str | None) -> "SubstrateContext":
    import json

    descriptor: dict = {"version": 1, "build": {}}
    if configured is not None:
        descriptor["build"]["workspace"] = configured
    (root / "stm32-project.jsonc").write_text(json.dumps(descriptor))
    return SubstrateContext.from_environment(project_path=root)


class TestResolveWorkspace:
    def test_default_is_out_of_tree(self, tmp_path: Path) -> None:
        from embedagents.stm32.cubeide.client import CubeIDE

        proj = tmp_path / "STM32CubeIDE"
        proj.mkdir()
        ctx = _ctx_with_workspace(proj, configured=None)
        ws = CubeIDE(ctx)._resolve_workspace(proj)
        assert ws == workspace.default_workspace_root(proj)
        assert not workspace.workspace_nested_in_project(ws, proj)

    def test_explicit_in_tree_raises(self, tmp_path: Path) -> None:
        from embedagents.stm32.cubeide.client import CubeIDE
        from embedagents.stm32.errors import ConfigurationError

        proj = tmp_path / "STM32CubeIDE"
        proj.mkdir()
        # Relative "." anchors to the project root → equals project_path.
        ctx = _ctx_with_workspace(proj, configured=".")
        with pytest.raises(
            ConfigurationError, match=r"inside the project tree"
        ) as excinfo:
            CubeIDE(ctx)._resolve_workspace(proj)
        assert "OUTSIDE the project" in (excinfo.value.hint or "")

    def test_explicit_out_of_tree_honored(self, tmp_path: Path) -> None:
        from embedagents.stm32.cubeide.client import CubeIDE

        proj = tmp_path / "STM32CubeIDE"
        proj.mkdir()
        external = tmp_path / "external_ws"
        ctx = _ctx_with_workspace(proj, configured=str(external))
        ws = CubeIDE(ctx)._resolve_workspace(proj)
        assert ws == external.resolve()


# ---------------------------------------------------------------------------
# Helpers — cross-OS sibling lock holder.
# Mirrors test_platform.py's helper (msvcrt on Windows, fcntl on Linux).
# Kept local rather than imported across test modules to avoid a shared
# test-helper package; only ~15 lines per copy.
# ---------------------------------------------------------------------------


def _spawn_lock_holder(lock_path: Path, hold_seconds: float) -> subprocess.Popen:
    """Spawn a sibling process that grabs an exclusive lock on
    ``lock_path`` and sleeps ``hold_seconds``.

    Linux: ``fcntl.flock(LOCK_EX | LOCK_NB)``.
    Windows: ``msvcrt.locking(LK_NBLCK, 1)`` over a 1-byte region; the
    file is seeded with a byte first since msvcrt-locking refuses empty
    regions.
    """
    if sys.platform == "win32":
        # NB: the seed byte must NOT be NUL — Windows CreateProcess
        # rejects an argv containing an embedded null character, and a
        # NUL in the script source would propagate through.
        script = f"""
import msvcrt, time, pathlib
p = pathlib.Path({str(lock_path)!r})
p.parent.mkdir(parents=True, exist_ok=True)
if not p.exists() or p.stat().st_size == 0:
    with p.open('ab') as seed:
        seed.write(b'X')
f = p.open('r+b')
f.seek(0)
msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
time.sleep({hold_seconds!r})
"""
    else:
        script = f"""
import fcntl, time, pathlib
p = pathlib.Path({str(lock_path)!r})
p.touch()
f = p.open('a+')
fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
time.sleep({hold_seconds!r})
"""
    return subprocess.Popen([sys.executable, "-c", script])
