"""C3b tests — cubemx running-loop algorithm.

The runner exposes ``_now`` / ``_spawn`` / ``_sleep`` hook parameters
specifically so these tests can drive it deterministically. Each test
constructs a fake Popen + iterates a clock to hit one specific branch."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubemx import ProgressEvent
from stm32_substrate.cubemx.runner import (
    RunnerPolicy,
    policy_from_ctx,
    run_cubemx,
)


# ---------------------------------------------------------------------------
# Fake Popen — exposes the surface our runner actually uses.
# ---------------------------------------------------------------------------


@dataclass
class FakePopen:
    """Stand-in for subprocess.Popen that scripts its own poll() return.

    ``poll_results`` is consumed left-to-right; once exhausted, ``poll``
    keeps returning the last value.
    """

    poll_results: list[int | None] = field(default_factory=lambda: [None])
    pid: int = 12345
    returncode: int | None = None
    terminate_called: bool = False
    kill_called: bool = False
    _exited: bool = False

    def poll(self) -> int | None:
        result = (
            self.poll_results[0]
            if len(self.poll_results) == 1
            else self.poll_results.pop(0)
        )
        if result is not None:
            self.returncode = result
            self._exited = True
        return result

    def terminate(self) -> None:
        self.terminate_called = True
        if not self._exited:
            self.returncode = -15
            self._exited = True
            # Subsequent polls now report exited.
            self.poll_results = [-15]

    def kill(self) -> None:
        self.kill_called = True
        if not self._exited:
            self.returncode = -9
            self._exited = True
            self.poll_results = [-9]

    def wait(self, timeout: float | None = None) -> int | None:
        if not self._exited:
            self._exited = True
            self.returncode = 0
            self.poll_results = [0]
        return self.returncode


@dataclass
class ClockController:
    """Iterates an explicit list of monotonic values for `_now`."""

    values: list[float]
    idx: int = 0
    last: float = 0.0

    def __call__(self) -> float:
        if self.idx < len(self.values):
            self.last = self.values[self.idx]
            self.idx += 1
            return self.last
        return self.last


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake = tmp_path / "STM32CubeMX"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("STM32CUBEMX_PATH", str(fake))
    return SubstrateContext.from_environment(project_path=tmp_path)


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    d = tmp_path / "generated"
    d.mkdir()
    return d


def _spawn_factory(fake: FakePopen):
    def _spawn(*args: Any, **kwargs: Any) -> FakePopen:
        return fake

    return _spawn


# ---------------------------------------------------------------------------
# policy_from_ctx
# ---------------------------------------------------------------------------


class TestPolicy:
    def test_defaults_when_block_missing(self, ctx: SubstrateContext) -> None:
        policy = policy_from_ctx(ctx)
        assert policy.initial_budget_s == 300.0
        assert policy.extension_delta_s == 60.0
        assert policy.max_extensions == 3
        assert policy.poll_interval_s == 2.0
        assert policy.liveness_threshold_s == 10.0
        assert policy.post_exit_grace_s == 3.0

    def test_overrides_via_runtime_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "STM32CubeMX"
        fake.write_text("")
        fake.chmod(0o755)
        monkeypatch.setenv("STM32CUBEMX_PATH", str(fake))
        defaults = {
            "version": 1,
            "cubemx": {
                "long_call_s": 120,
                "long_call_extension_s": 30,
                "long_call_max_extensions": 5,
                "poll_interval_s": 1,
                "liveness_threshold_s": 5,
                "post_exit_grace_s": 2,
            },
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
        policy = policy_from_ctx(ctx2)
        assert policy.initial_budget_s == 120.0
        assert policy.max_extensions == 5


# ---------------------------------------------------------------------------
# Branch: marker appears mid-run → success + terminate subprocess
# ---------------------------------------------------------------------------


class TestMarkerAppearsMidRun:
    def test_success_terminates_subprocess(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"
        # marker does NOT exist at T0.

        fake = FakePopen(poll_results=[None, None, None, None])
        # Iterate a clock that triggers the loop a few times.
        clock = ClockController(values=[0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0])
        polls = {"n": 0}

        def sleep_then_mark(_seconds: float) -> None:
            polls["n"] += 1
            if polls["n"] == 2:
                marker.write_text("<cproject/>")

        result = run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=sleep_then_mark,
        )
        assert result.success is True
        assert result.terminated_after_marker is True
        assert fake.terminate_called is True
        assert result.exit_code is None  # signal-derived; suppressed
        assert result.cubemx_log_path is None  # log "useless" on success


# ---------------------------------------------------------------------------
# Branch: subprocess exits + marker exists → success (no-op regen / settled)
# ---------------------------------------------------------------------------


class TestSubprocessExitWithMarker:
    def test_no_op_regen_succeeds(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"
        marker.write_text("<cproject/>")  # pre-existing

        # Subprocess exits 0 on the first poll without touching marker.
        fake = FakePopen(poll_results=[0])
        clock = ClockController(values=[0.0, 0.0, 0.5, 0.5])

        result = run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=lambda _s: None,
        )
        assert result.success is True
        assert result.terminated_after_marker is False
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Branch: subprocess exits without marker, marker appears in grace → success
# ---------------------------------------------------------------------------


class TestPostExitGraceSuccess:
    def test_marker_within_grace(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"

        fake = FakePopen(poll_results=[0])  # exited 0 immediately.
        clock = ClockController(
            values=[0.0, 0.0, 0.5, 0.5, 1.0, 1.5, 2.0]  # generous
        )
        sleeps = {"n": 0}

        def grace_sleep(_s: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 1:
                marker.write_text("<cproject/>")

        result = run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=grace_sleep,
        )
        assert result.success is True
        assert result.terminated_after_marker is False


# ---------------------------------------------------------------------------
# Branch: subprocess exits without marker, grace expires → failure
# ---------------------------------------------------------------------------


class TestPostExitGraceFailure:
    def test_no_marker_after_grace(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"

        fake = FakePopen(poll_results=[1])  # exited non-zero.
        # Clock advances past grace deadline quickly.
        clock = ClockController(
            values=[0.0, 0.0, 0.5, 0.5, 100.0, 100.0, 100.0]
        )

        result = run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=lambda _s: None,
        )
        assert result.success is False
        assert result.timed_out is False  # not a timeout — subprocess exited
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Branch: deadline + log activity → extension fires
# ---------------------------------------------------------------------------


class TestDeadlineExtension:
    def test_extension_fires_with_recent_log_activity(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_launcher = tmp_path / "STM32CubeMX"
        fake_launcher.write_text("")
        fake_launcher.chmod(0o755)
        monkeypatch.setenv("STM32CUBEMX_PATH", str(fake_launcher))

        # Set tight policy + cubemx log path so the test can simulate activity.
        cubemx_log = tmp_path / "cubemx.log"
        defaults = {
            "version": 1,
            "cubemx": {
                "long_call_s": 1,
                "long_call_extension_s": 1,
                "long_call_max_extensions": 2,
                "liveness_threshold_s": 5,
                "post_exit_grace_s": 0,
                "log_path": str(cubemx_log),
            },
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)

        marker = output_dir / ".cproject"
        fake = FakePopen(poll_results=[None] * 10)

        # Clock walks: T0=0, then 0.5, 1.5 (past deadline → extension),
        # 2.5 (past extended deadline → extension), 3.5 (max reached →
        # timeout).
        clock = ClockController(
            values=[0.0, 0.0, 0.5, 0.5, 1.5, 1.5, 2.5, 2.5, 3.5, 3.5, 4.5]
        )

        polls = {"n": 0}

        def sleep_and_bump_log(_s: float) -> None:
            polls["n"] += 1
            # Touch the cubemx log to register "activity".
            cubemx_log.write_text(f"poll {polls['n']}")

        result = run_cubemx(
            launcher=fake_launcher,
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx2,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=sleep_and_bump_log,
        )
        # Both extensions fired before max reached → final state is
        # timed_out=True (no marker).
        assert result.extensions_used == 2
        assert result.timed_out is True
        assert result.success is False


# ---------------------------------------------------------------------------
# Branch: deadline + no log activity → immediate timeout
# ---------------------------------------------------------------------------


class TestDeadlineNoActivity:
    def test_timeout_without_extension(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_launcher = tmp_path / "STM32CubeMX"
        fake_launcher.write_text("")
        fake_launcher.chmod(0o755)
        monkeypatch.setenv("STM32CUBEMX_PATH", str(fake_launcher))

        defaults = {
            "version": 1,
            "cubemx": {
                "long_call_s": 1,
                "long_call_extension_s": 1,
                "long_call_max_extensions": 3,
                "liveness_threshold_s": 1,
                "post_exit_grace_s": 0,
            },
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)

        marker = output_dir / ".cproject"
        fake = FakePopen(poll_results=[None, None, None])
        # T0=0; next call → 100 (past deadline + past liveness threshold).
        clock = ClockController(values=[0.0, 0.0, 100.0, 100.0, 100.0])

        result = run_cubemx(
            launcher=fake_launcher,
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx2,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=lambda _s: None,
        )
        assert result.success is False
        assert result.timed_out is True
        assert result.extensions_used == 0
        assert fake.terminate_called is True


# ---------------------------------------------------------------------------
# on_progress callback fires per poll-tick
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_one_event_per_poll(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"
        events: list[ProgressEvent] = []

        fake = FakePopen(poll_results=[None, None, None])
        clock = ClockController(values=[0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0])

        polls = {"n": 0}

        def sleep_then_mark(_s: float) -> None:
            polls["n"] += 1
            if polls["n"] == 2:
                marker.write_text("<cproject/>")

        run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            on_progress=events.append,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=sleep_then_mark,
        )
        assert len(events) >= 2
        for e in events:
            assert e.stage == "cubemx_running"
            assert e.duration_s >= 0
            assert e.deadline_s > 0


# ---------------------------------------------------------------------------
# Script file lifecycle
# ---------------------------------------------------------------------------


class TestScriptFileLifecycle:
    def test_script_file_cleaned_up(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"
        marker.write_text("<cproject/>")
        fake = FakePopen(poll_results=[0])
        clock = ClockController(values=[0.0, 0.0, 0.5])

        run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate\nexit_mx\n",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=lambda _s: None,
        )
        # The runner writes a temp script then unlinks it on completion.
        # Verify no stray cubemx-script-*.txt under output_dir.
        leftovers = list(output_dir.glob("cubemx-script-*.txt"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# log_path always populated; cubemx_log_path only on failure
# ---------------------------------------------------------------------------


class TestLogPaths:
    def test_log_path_populated_on_success(
        self, ctx: SubstrateContext, output_dir: Path
    ) -> None:
        marker = output_dir / ".cproject"
        marker.write_text("<cproject/>")
        fake = FakePopen(poll_results=[0])
        clock = ClockController(values=[0.0, 0.0, 0.5])

        result = run_cubemx(
            launcher=Path("/fake/STM32CubeMX"),
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=lambda _s: None,
        )
        assert result.log_path.is_file()
        assert result.cubemx_log_path is None  # success → cubemx log not reported

    def test_cubemx_log_path_populated_on_failure(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cubemx_log = tmp_path / "cubemx.log"
        cubemx_log.write_text("error somewhere")

        fake_launcher = tmp_path / "STM32CubeMX"
        fake_launcher.write_text("")
        fake_launcher.chmod(0o755)
        monkeypatch.setenv("STM32CUBEMX_PATH", str(fake_launcher))

        defaults = {
            "version": 1,
            "cubemx": {
                "long_call_s": 1,
                "long_call_max_extensions": 0,
                "post_exit_grace_s": 0,
                "log_path": str(cubemx_log),
            },
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(json.dumps(defaults))
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)

        marker = output_dir / ".cproject"
        fake = FakePopen(poll_results=[None, None])
        clock = ClockController(values=[0.0, 0.0, 100.0, 100.0])

        result = run_cubemx(
            launcher=fake_launcher,
            script_text="project generate",
            expected_marker=marker,
            output_dir=output_dir,
            ctx=ctx2,
            _now=clock,
            _spawn=_spawn_factory(fake),
            _sleep=lambda _s: None,
        )
        assert result.success is False
        assert result.cubemx_log_path == cubemx_log
