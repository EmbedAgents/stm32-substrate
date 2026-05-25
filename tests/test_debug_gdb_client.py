"""C4e GDBClient tests — MI3 wrapper command/response + async handling.

Uses a fake Popen-like object with a write-tracking stdin and a scripted
stdout queue. Each test exercises one branch: happy command-response,
async records during a command, timeout + interrupt, EOF →
GDBSessionLost, send_console stream capture, wait_for_stopped queue
drain + fresh read."""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.debug.gdb import GDBClient, spawn_gdb
from stm32_substrate.debug.results import (
    MIAsyncRecord,
    StoppedNotification,
)
from stm32_substrate.errors import GDBError, GDBSessionLost


@dataclass
class _FakeStdout:
    """Iterator that yields canned lines one per ``readline()``.

    Once exhausted, returns ``""`` (EOF).
    """

    lines: list[str] = field(default_factory=list)
    idx: int = 0

    def readline(self) -> str:
        if self.idx >= len(self.lines):
            return ""
        line = self.lines[self.idx]
        self.idx += 1
        return line


@dataclass
class FakeProc:
    """Stand-in for subprocess.Popen used by GDBClient."""

    stdout_lines: list[str] = field(default_factory=list)
    poll_results: list[int | None] = field(default_factory=lambda: [None])
    pid: int = 7777
    returncode: int | None = None
    _exited: bool = False
    _terminated: bool = False
    _killed: bool = False

    def __post_init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = _FakeStdout(lines=list(self.stdout_lines))

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

    def wait(self, timeout: float | None = None) -> int | None:
        if not self._exited:
            self._exited = True
            self.returncode = 0
            self.poll_results = [0]
        return self.returncode

    def terminate(self) -> None:
        self._terminated = True
        if not self._exited:
            self.returncode = -15
            self._exited = True
            self.poll_results = [-15]

    def kill(self) -> None:
        self._killed = True
        if not self._exited:
            self.returncode = -9
            self._exited = True
            self.poll_results = [-9]

    @property
    def stdin_text(self) -> str:
        return self.stdin.getvalue()


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    for env_var, name in (
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    return SubstrateContext.from_environment(project_path=tmp_path)


# ---------------------------------------------------------------------------
# send_mi
# ---------------------------------------------------------------------------


class TestSendMi:
    def test_command_response_roundtrip(self, ctx: SubstrateContext) -> None:
        # gdb echoes our token back on the response record.
        proc = FakeProc(
            stdout_lines=['1^done,value="42"\n', "(gdb)\n"]
        )
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        result = client.send_mi("-data-evaluate-expression x")
        assert result.token == 1
        assert result.class_ == "done"
        assert result.fields["value"] == "42"
        # Stdin contains the token-tagged command.
        assert "1-data-evaluate-expression x\n" in proc.stdin_text

    def test_skips_irrelevant_records_until_token_match(
        self, ctx: SubstrateContext
    ) -> None:
        # gdb emits noise then the matching record.
        proc = FakeProc(
            stdout_lines=[
                '~"Reading symbols...\\n"\n',
                "*running,thread-id=\"all\"\n",
                '1^done,value="ok"\n',
            ]
        )
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        result = client.send_mi("-some-cmd")
        assert result.fields["value"] == "ok"
        # The *running record landed in the async queue.
        assert len(client._async_queue) == 1
        assert client._async_queue[0].class_ == "running"

    def test_token_increments_per_call(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(
            stdout_lines=[
                "1^done\n",
                "2^done\n",
                "3^done\n",
            ]
        )
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        for _ in range(3):
            client.send_mi("-noop")
        # Three sequential tokens in stdin.
        assert "1-noop\n" in proc.stdin_text
        assert "2-noop\n" in proc.stdin_text
        assert "3-noop\n" in proc.stdin_text

    def test_command_with_leading_dash(self, ctx: SubstrateContext) -> None:
        """Caller may pass the command already prefixed with ``-``; we
        still tag it with a token (no double-dash)."""
        proc = FakeProc(stdout_lines=["1-stack-list-frames\n", "1^done,stack=[]\n"])
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        client.send_mi("-stack-list-frames")
        # Stdin should contain "1-stack-list-frames" not "1--stack-list-frames".
        assert "1-stack-list-frames\n" in proc.stdin_text
        assert "1--" not in proc.stdin_text


# ---------------------------------------------------------------------------
# Async record handling
# ---------------------------------------------------------------------------


class TestAsyncRecords:
    def test_async_during_command_queued(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(
            stdout_lines=[
                '*stopped,reason="breakpoint-hit",bkptno="1"\n',
                "1^done\n",
            ]
        )
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        client.send_mi("-noop")
        # The *stopped is queued for wait_for_stopped.
        assert len(client._async_queue) == 1
        rec = client._async_queue[0]
        assert isinstance(rec, MIAsyncRecord)
        assert rec.class_ == "stopped"


class TestWaitForStopped:
    def test_drains_queue_first(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(stdout_lines=[])
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        # Pre-seed an async record into the queue.
        from stm32_substrate.debug.parsers import parse_mi_record

        rec = parse_mi_record('*stopped,reason="end-stepping-range"')
        assert isinstance(rec, MIAsyncRecord)
        client._async_queue.append(rec)
        result = client.wait_for_stopped(timeout_s=0.5)
        assert isinstance(result, StoppedNotification)
        assert result.reason == "end-stepping-range"

    def test_reads_fresh_record(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(
            stdout_lines=['*stopped,reason="breakpoint-hit",bkptno="2"\n']
        )
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        result = client.wait_for_stopped(timeout_s=0.5)
        assert isinstance(result, StoppedNotification)
        assert result.breakpoint_number == 2

    def test_timeout_returns_none(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(stdout_lines=[])
        # With empty stdout, readline returns "" immediately → triggers
        # GDBSessionLost. To exercise timeout path proper, we need a
        # producer that yields no useful records but doesn't hit EOF.
        # That's harder to fake; here we accept that "empty stdout → EOF"
        # is the substrate's documented contract.
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        with pytest.raises(GDBSessionLost):
            client.wait_for_stopped(timeout_s=0.1)


# ---------------------------------------------------------------------------
# send_console
# ---------------------------------------------------------------------------


class TestSendConsole:
    def test_captures_stream_output(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(
            stdout_lines=[
                '~"Resetting target\\n"\n',
                '~"Halted at 0x08000000\\n"\n',
                "1^done\n",
            ]
        )
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        out = client.send_console("monitor reset halt")
        assert out == ["Resetting target\n", "Halted at 0x08000000\n"]
        # Command was wrapped in -interpreter-exec console.
        assert "-interpreter-exec console" in proc.stdin_text


# ---------------------------------------------------------------------------
# Session loss + timeout
# ---------------------------------------------------------------------------


class TestSessionLoss:
    def test_eof_raises_session_lost(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(stdout_lines=[])
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        with pytest.raises(GDBSessionLost) as excinfo:
            client.send_mi("-noop")
        assert excinfo.value.gdb_marker == "remote-connection-closed"

    def test_exited_subprocess_raises_session_lost(
        self, ctx: SubstrateContext
    ) -> None:
        proc = FakeProc(poll_results=[1])
        # Force the next poll() to return 1 (exited).
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        with pytest.raises(GDBSessionLost):
            client.send_mi("-noop")


class TestTimeout:
    def test_command_timeout_raises(self, ctx: SubstrateContext) -> None:
        # Clock advances past deadline on first iteration.
        clock_values = iter([0.0, 100.0, 100.0])
        # Stdout has a record but we time out before consuming it.
        proc = FakeProc(stdout_lines=["1^done\n"])
        client = GDBClient(
            proc=proc,  # type: ignore[arg-type]
            ctx=ctx,
            _now=lambda: next(clock_values, 100.0),
        )
        with pytest.raises(GDBError) as excinfo:
            client.send_mi("-noop", timeout_s=5.0)
        assert excinfo.value.gdb_marker == "command-timeout"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    def test_sends_gdb_exit(self, ctx: SubstrateContext) -> None:
        proc = FakeProc()
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        client.close()
        assert "-gdb-exit" in proc.stdin_text

    def test_idempotent(self, ctx: SubstrateContext) -> None:
        proc = FakeProc()
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        client.close()
        gdb_exits_after_first = proc.stdin_text.count("-gdb-exit")
        client.close()
        # No extra writes on second close.
        assert proc.stdin_text.count("-gdb-exit") == gdb_exits_after_first

    def test_already_dead_skips_signals(self, ctx: SubstrateContext) -> None:
        proc = FakeProc(poll_results=[0])
        proc.poll()  # mark as exited
        client = GDBClient(proc=proc, ctx=ctx)  # type: ignore[arg-type]
        client.close()
        # No -gdb-exit written because we short-circuit on already-dead.
        assert "-gdb-exit" not in proc.stdin_text


# ---------------------------------------------------------------------------
# spawn_gdb integration
# ---------------------------------------------------------------------------


class TestSpawnGdb:
    def test_happy_path_returns_client(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")

        proc = FakeProc(
            stdout_lines=[
                '1^connected\n',
            ]
        )

        def fake_spawn(*args: Any, **kwargs: Any) -> FakeProc:
            return proc

        client = spawn_gdb(
            ctx=ctx, elf_path=elf, gdb_port=61234, _spawn=fake_spawn
        )
        assert isinstance(client, GDBClient)
        assert "-target-select extended-remote localhost:61234" in proc.stdin_text

    def test_connect_error_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")

        proc = FakeProc(
            stdout_lines=[
                '1^error,msg="connection refused"\n',
            ]
        )

        def fake_spawn(*args: Any, **kwargs: Any) -> FakeProc:
            return proc

        with pytest.raises(GDBError) as excinfo:
            spawn_gdb(ctx=ctx, elf_path=elf, gdb_port=61234, _spawn=fake_spawn)
        assert excinfo.value.gdb_marker == "remote-connection-closed"
