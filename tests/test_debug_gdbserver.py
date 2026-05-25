"""C4c gdbserver lifecycle tests.

Uses a fake Popen-like object that scripts its merged output stream
line-by-line plus poll() returns, exercising each path: listener-ready
/ port-busy / probe-not-found / handshake-timeout / spawn failure.

Substrate merges gdbserver stderr into stdout per ADR-007's
cross-OS handling (Linux emits announcement on stderr; Windows
v7.13.0 emits on stdout). Tests feed lines via the ``output_lines``
field which lands on the fake stdout stream."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.debug.gdbserver import (
    GDBServerOptions,
    GDBServerProcess,
    spawn_gdbserver,
)
from stm32_substrate.errors import GDBError


@dataclass
class _FakeStream:
    """Iterator over a canned list of output lines.

    Returns ``""`` once exhausted so the caller can detect EOF without
    blocking (matches Popen.std{out,err}.readline behaviour on a
    closed pipe).
    """

    lines: list[str] = field(default_factory=list)
    idx: int = 0

    def readline(self) -> str:
        if self.idx >= len(self.lines):
            return ""
        line = self.lines[self.idx]
        self.idx += 1
        return line

    def read(self) -> str:
        rest = "".join(self.lines[self.idx :])
        self.idx = len(self.lines)
        return rest


@dataclass
class FakePopen:
    """Stand-in for subprocess.Popen with scripted poll + merged output.

    ``output_lines`` populates the fake stdout stream (substrate merges
    gdbserver stderr into stdout when spawning, so this matches the
    real read path)."""

    output_lines: list[str] = field(default_factory=list)
    poll_results: list[int | None] = field(default_factory=lambda: [None])
    pid: int = 12345
    returncode: int | None = None
    _terminate_called: bool = False
    _kill_called: bool = False
    _exited: bool = False

    def __post_init__(self) -> None:
        self.stdout = _FakeStream(lines=list(self.output_lines))
        self.stderr = _FakeStream()

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
        self._terminate_called = True
        if not self._exited:
            self.returncode = -15
            self._exited = True
            self.poll_results = [-15]

    def kill(self) -> None:
        self._kill_called = True
        if not self._exited:
            self.returncode = -9
            self._exited = True
            self.poll_results = [-9]

    def wait(self, timeout: float | None = None) -> int | None:
        if not self._exited:
            self.returncode = 0
            self._exited = True
            self.poll_results = [0]
        return self.returncode


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    """Resolves ST-LINK_gdbserver + STM32_Programmer_CLI to fake binaries."""
    for env_var, name in (
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _options(tmp_path: Path, **overrides: Any) -> GDBServerOptions:
    defaults = {
        "port": 61234,
        "cube_programmer_cli_dir": tmp_path / "cp",
        "halt_on_attach": True,
    }
    defaults.update(overrides)
    return GDBServerOptions(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path: listener-ready
# ---------------------------------------------------------------------------


class TestSpawnHappyPath:
    def test_listener_ready_returns_process(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        fake = FakePopen(
            output_lines=[
                "STMicroelectronics ST-LINK GDB server.\n",
                "Listening for incoming connections...\n",
                "Waiting for debugger connection on port 61234...\n",
            ]
        )

        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            return fake

        result = spawn_gdbserver(
            ctx=ctx,
            options=_options(tmp_path),
            _spawn=spawn,
        )
        assert isinstance(result, GDBServerProcess)
        assert result.port == 61234

    def test_argv_shape(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        captured_argv: list[list[str]] = []

        def spawn(argv: list[str], **kwargs: Any) -> FakePopen:
            captured_argv.append(argv)
            return FakePopen(
                output_lines=["Waiting for debugger connection on port 61234\n"]
            )

        cp_dir = tmp_path / "cp"
        spawn_gdbserver(
            ctx=ctx,
            options=_options(
                tmp_path,
                cube_programmer_cli_dir=cp_dir,
                halt_on_attach=True,
                persistent=True,
                stlink_serial="066BFFSN",
            ),
            _spawn=spawn,
        )
        argv = captured_argv[0]
        assert argv[1:3] == ["-d", "-p"]
        assert argv[3] == "61234"
        assert "-cp" in argv
        assert str(cp_dir) in argv
        assert "-e" in argv
        assert "-i" in argv
        assert "066BFFSN" in argv
        # halt_on_attach=True → no -g flag.
        assert "-g" not in argv

    def test_attach_running_uses_dash_g(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        captured: list[list[str]] = []

        def spawn(argv: list[str], **kwargs: Any) -> FakePopen:
            captured.append(argv)
            return FakePopen(
                output_lines=["Waiting for debugger connection on port 61234\n"]
            )

        spawn_gdbserver(
            ctx=ctx,
            options=_options(tmp_path, halt_on_attach=False),
            _spawn=spawn,
        )
        assert "-g" in captured[0]

    def test_n6_dev_mode_forces_dash_g_even_when_halting(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """DBG-012: the flashless N6 can't be reset-and-halted by the
        gdbserver ("Target not halted after reset" → exit 9); it must be
        attached as a running target. n6_dev_mode forces `-g` even with
        halt_on_attach=True. Verified on bench 2026-05-24: `-g` reaches
        listener-ready on the STM32N6570-DK."""
        captured: list[list[str]] = []

        def spawn(argv: list[str], **kwargs: Any) -> FakePopen:
            captured.append(argv)
            return FakePopen(
                output_lines=["Waiting for debugger connection on port 61234\n"]
            )

        spawn_gdbserver(
            ctx=ctx,
            options=_options(tmp_path, halt_on_attach=True, n6_dev_mode=True),
            _spawn=spawn,
        )
        assert "-g" in captured[0]

    def test_remapped_port_returned(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """gdbserver may bind a different port than requested; substrate
        trusts the announcement."""
        fake = FakePopen(
            output_lines=["Waiting for debugger connection on port 61999\n"]
        )

        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            return fake

        result = spawn_gdbserver(
            ctx=ctx,
            options=_options(tmp_path, port=61234),
            _spawn=spawn,
        )
        assert result.port == 61999


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestSpawnErrors:
    def test_unconfigured_gdbserver_raises_loud(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("STLINK_GDB_SERVER", raising=False)
        monkeypatch.setenv("PATH", "")
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        with pytest.raises(GDBError) as excinfo:
            spawn_gdbserver(ctx=ctx, options=_options(tmp_path))
        assert excinfo.value.gdb_marker == "gdbserver-spawn-failed"

    def test_spawn_oserror_surfaces(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            raise FileNotFoundError("no such file")

        with pytest.raises(GDBError) as excinfo:
            spawn_gdbserver(
                ctx=ctx, options=_options(tmp_path), _spawn=spawn
            )
        assert excinfo.value.gdb_marker == "gdbserver-spawn-failed"

    def test_port_busy_stderr_pattern(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        fake = FakePopen(
            output_lines=["Error: Address already in use\n"]
        )

        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            return fake

        with pytest.raises(GDBError) as excinfo:
            spawn_gdbserver(
                ctx=ctx, options=_options(tmp_path), _spawn=spawn
            )
        err = excinfo.value
        assert err.gdb_marker == "port-busy"
        assert err.recoverable is True

    def test_probe_not_found_stderr_pattern(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        fake = FakePopen(
            output_lines=["Error: No ST-LINK detected\n"],
            poll_results=[None, 1],
        )

        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            return fake

        with pytest.raises(GDBError) as excinfo:
            spawn_gdbserver(
                ctx=ctx, options=_options(tmp_path), _spawn=spawn
            )
        assert excinfo.value.gdb_marker == "probe-not-found"

    def test_handshake_timeout(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """No listener-ready line + no error pattern + no exit →
        substrate bails after handshake_timeout_s with command-timeout."""

        clock_values = iter([0.0, 0.0, 0.5, 1.0, 100.0, 100.0, 100.0])

        def fake_now() -> float:
            return next(clock_values, 200.0)

        fake = FakePopen(output_lines=[])  # empty stderr forever

        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            return fake

        with pytest.raises(GDBError) as excinfo:
            spawn_gdbserver(
                ctx=ctx,
                options=_options(tmp_path),
                handshake_timeout_s=5.0,
                _spawn=spawn,
                _now=fake_now,
                _sleep=lambda _s: None,
            )
        assert excinfo.value.gdb_marker == "command-timeout"

    def test_early_exit_classified(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """Subprocess exits before announcing — substrate drains the
        remaining merged output to classify the error."""

        # Sequence: first poll returns None (still alive), next iteration
        # readline yields nothing, poll returns 1 (exited). The error
        # text sits on the merged-output stream (stdout, since substrate
        # merges stderr into stdout at spawn).
        fake = FakePopen(
            output_lines=[],
            poll_results=[None, 1],
        )
        fake.stdout.lines = ["Error: Address already in use\n"]

        def spawn(*args: Any, **kwargs: Any) -> FakePopen:
            return fake

        with pytest.raises(GDBError) as excinfo:
            spawn_gdbserver(
                ctx=ctx, options=_options(tmp_path), _spawn=spawn,
                _sleep=lambda _s: None,
            )
        err = excinfo.value
        # Spawned, output drained, classified as port-busy.
        assert err.gdb_marker == "port-busy"


# ---------------------------------------------------------------------------
# GDBServerProcess.close
# ---------------------------------------------------------------------------


class TestProcessClose:
    def test_close_terminates(self) -> None:
        fake = FakePopen()
        process = GDBServerProcess(proc=fake, port=61234)  # type: ignore[arg-type]
        process.close()
        assert fake._terminate_called is True

    def test_close_idempotent(self) -> None:
        fake = FakePopen()
        process = GDBServerProcess(proc=fake, port=61234)  # type: ignore[arg-type]
        process.close()
        # Second close — no double-terminate.
        terminate_count_after_first = 1 if fake._terminate_called else 0
        fake._terminate_called = False
        process.close()
        assert fake._terminate_called is False
        assert terminate_count_after_first == 1

    def test_close_on_already_dead_skips_signal(self) -> None:
        fake = FakePopen(poll_results=[0])
        # Trigger poll to set _exited.
        fake.poll()
        process = GDBServerProcess(proc=fake, port=61234)  # type: ignore[arg-type]
        process.close()
        assert fake._terminate_called is False
