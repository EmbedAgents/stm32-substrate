"""C4g Debug.start_session orchestration tests + svd_for_attached
unblocking."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Any

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.debug import Debug, DebugSession
from embedagents.stm32.errors import ConfigurationError, GDBError
from embedagents.stm32.subprocess_runner import ToolRunResult


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    for env_var, name in (
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _ctx_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug_defaults: dict
) -> SubstrateContext:
    """Build a ctx whose ``defaults.debug`` carries the given knobs."""
    for env_var, name in (
        ("STM32_PROGRAMMER_CLI", "STM32_Programmer_CLI"),
        ("STLINK_GDB_SERVER", "ST-LINK_gdbserver"),
        ("ARM_NONE_EABI_GDB", "arm-none-eabi-gdb"),
    ):
        b = tmp_path / name
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        monkeypatch.setenv(env_var, str(b))
    (tmp_path / "stm32-runtime-defaults.jsonc").write_text(
        json.dumps({"version": 1, "debug": debug_defaults})
    )
    return SubstrateContext.from_environment(project_path=tmp_path)


# ---------------------------------------------------------------------------
# ELF resolution
# ---------------------------------------------------------------------------


class TestElfResolution:
    def test_explicit_elf(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        debug = Debug(ctx)
        # We test only the resolve helper here; full spawn lives below.
        elf = debug._resolve_elf(tmp_path / "demo.elf")
        assert elf == (tmp_path / "demo.elf").resolve()

    def test_no_elf_no_descriptor_raises(self, ctx: SubstrateContext) -> None:
        debug = Debug(ctx)
        with pytest.raises(ConfigurationError):
            debug._resolve_elf(None)


# ---------------------------------------------------------------------------
# Active-session check
# ---------------------------------------------------------------------------


class TestActiveSessionCheck:
    def test_existing_session_raises(self, ctx: SubstrateContext) -> None:
        debug = Debug(ctx)
        ctx.session_state.active_debug_session = MagicMock()
        with pytest.raises(GDBError) as excinfo:
            debug.start_session(Path("/tmp/demo.elf"))
        assert excinfo.value.gdb_marker == "session-already-active"


# ---------------------------------------------------------------------------
# N6 dev-mode confirmation
# ---------------------------------------------------------------------------


class TestN6DevMode:
    def test_no_callback_raises(self, ctx: SubstrateContext, tmp_path: Path) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        with pytest.raises(GDBError) as excinfo:
            debug.start_session(elf, n6_dev_mode=True)
        assert excinfo.value.gdb_marker == "n6-boot-not-confirmed"

    def test_callback_returning_false_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        with pytest.raises(GDBError) as excinfo:
            debug.start_session(
                elf,
                n6_dev_mode=True,
                on_n6_boot_confirm=lambda: False,
            )
        assert excinfo.value.gdb_marker == "n6-boot-not-confirmed"


# ---------------------------------------------------------------------------
# Port iteration
# ---------------------------------------------------------------------------


class TestPortIteration:
    def test_explicit_port_only(self, ctx: SubstrateContext) -> None:
        debug = Debug(ctx)
        ports = list(debug._port_iter(7777))
        assert ports == [7777]

    def test_default_port_range(self, ctx: SubstrateContext) -> None:
        debug = Debug(ctx)
        ports = list(debug._port_iter(None))
        assert len(ports) == 10
        assert ports[0] == 61234
        assert ports[-1] == 61243

    def test_custom_range_from_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx_with_defaults(
            tmp_path,
            monkeypatch,
            {
                "gdb_port": 50000,
                "gdb_port_fallback_range": [50000, 50001, 50002],
            },
        )
        debug = Debug(ctx)
        ports = list(debug._port_iter(None))
        assert ports == [50000, 50001, 50002]

    def test_gdb_port_knob_tried_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A-012: debug.gdb_port leads the walk even when it sits
        # mid-range (spec: try gdb_port, then the fallback range).
        ctx = _ctx_with_defaults(
            tmp_path,
            monkeypatch,
            {
                "gdb_port": 50001,
                "gdb_port_fallback_range": [50000, 50001, 50002],
            },
        )
        ports = list(Debug(ctx)._port_iter(None))
        assert ports == [50001, 50000, 50002]

    def test_gdb_port_knob_without_range_prepends_default_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx_with_defaults(tmp_path, monkeypatch, {"gdb_port": 50005})
        ports = list(Debug(ctx)._port_iter(None))
        assert ports[0] == 50005
        assert ports[1:] == list(range(61234, 61244))


# ---------------------------------------------------------------------------
# Full spawn flow with mocked spawners
# ---------------------------------------------------------------------------


class TestSpawnHappyPath:
    def test_starts_session_with_gdbserver_then_gdb(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)

        gdbserver_mock = MagicMock()
        gdbserver_mock.pid = 11111
        gdbserver_mock.port = 61234

        gdb_mock = MagicMock()
        gdb_mock.pid = 22222

        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver",
            return_value=gdbserver_mock,
        ) as spawn_gdbs, patch(
            "embedagents.stm32.debug.client.spawn_gdb",
            return_value=gdb_mock,
        ) as spawn_gdb_mock:
            session = debug.start_session(elf, halt=True)
        assert isinstance(session, DebugSession)
        assert session.gdb_port == 61234
        # gdbserver spawned once.
        assert spawn_gdbs.call_count == 1
        # gdb spawned once.
        assert spawn_gdb_mock.call_count == 1
        # send_console called for "monitor reset" since halt=True
        # (RES-041: gdbserver halts at Reset_Handler while attached;
        # the OpenOCD "reset halt" form is rejected with ^error).
        gdb_mock.send_console.assert_called_once()
        cmd_arg = gdb_mock.send_console.call_args[0][0]
        assert "monitor reset" == cmd_arg
        # Session registered in ctx.session_state.
        assert ctx.session_state.active_debug_session is session

    def test_attach_running_skips_reset_halt(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)

        gdbserver_mock = MagicMock()
        gdbserver_mock.pid = 1
        gdbserver_mock.port = 61234
        gdb_mock = MagicMock()
        gdb_mock.pid = 2

        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver",
            return_value=gdbserver_mock,
        ), patch(
            "embedagents.stm32.debug.client.spawn_gdb",
            return_value=gdb_mock,
        ):
            session = debug.attach_running(elf)
        gdb_mock.send_console.assert_not_called()
        assert session.target_halted is False


# ---------------------------------------------------------------------------
# Port fallback
# ---------------------------------------------------------------------------


class TestPortFallback:
    def test_port_busy_walks_to_next(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)

        port_busy_err = GDBError(
            message="port busy",
            gdb_marker="port-busy",
            recoverable=True,
        )

        spawn_gdbs = MagicMock(
            side_effect=[port_busy_err, port_busy_err, MagicMock(pid=1, port=61236)]
        )
        gdb_mock = MagicMock(pid=2)

        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver", spawn_gdbs
        ), patch(
            "embedagents.stm32.debug.client.spawn_gdb", return_value=gdb_mock
        ):
            session = debug.start_session(elf, halt=False)
        assert session.gdb_port == 61236
        assert spawn_gdbs.call_count == 3

    def test_all_ports_busy_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)

        spawn_gdbs = MagicMock(
            side_effect=[
                GDBError(message="busy", gdb_marker="port-busy")
                for _ in range(20)
            ]
        )
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver", spawn_gdbs
        ):
            with pytest.raises(GDBError) as excinfo:
                debug.start_session(elf)
        assert excinfo.value.gdb_marker == "no-free-gdb-port"

    def test_non_port_error_propagates(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        """Non-port errors (probe-not-found / gdbserver-spawn-failed)
        propagate without retry."""
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)

        spawn_gdbs = MagicMock(
            side_effect=GDBError(message="no probe", gdb_marker="probe-not-found")
        )
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver", spawn_gdbs
        ):
            with pytest.raises(GDBError) as excinfo:
                debug.start_session(elf)
        assert excinfo.value.gdb_marker == "probe-not-found"
        assert spawn_gdbs.call_count == 1


# ---------------------------------------------------------------------------
# teardown on unexpected spawn errors (IMP-11)
# ---------------------------------------------------------------------------


class TestSpawnTeardown:
    def test_non_gdberror_from_spawn_gdb_closes_gdbserver(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        gdbserver_mock = MagicMock(pid=1, port=61234)
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver",
            return_value=gdbserver_mock,
        ), patch(
            "embedagents.stm32.debug.client.spawn_gdb",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError):
                debug.start_session(elf)
        gdbserver_mock.close.assert_called_once()

    def test_non_gdberror_from_reset_halt_closes_both(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        gdbserver_mock = MagicMock(pid=1, port=61234)
        gdb_mock = MagicMock(pid=2)
        gdb_mock.send_console.side_effect = RuntimeError("boom")
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver",
            return_value=gdbserver_mock,
        ), patch(
            "embedagents.stm32.debug.client.spawn_gdb", return_value=gdb_mock
        ):
            with pytest.raises(RuntimeError):
                debug.start_session(elf, halt=True)
        gdb_mock.close.assert_called_once()
        gdbserver_mock.close.assert_called_once()


# ---------------------------------------------------------------------------
# session-start timeout knobs (A-012)
# ---------------------------------------------------------------------------


class TestSessionStartTimeoutKnobs:
    def test_handshake_knob_passed_to_spawn_gdbserver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx_with_defaults(
            tmp_path, monkeypatch, {"gdbserver_spawn_timeout_s": 3}
        )
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        gdbserver_mock = MagicMock(pid=1, port=61234)
        gdb_mock = MagicMock(pid=2)
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver",
            return_value=gdbserver_mock,
        ) as spawn_gdbs, patch(
            "embedagents.stm32.debug.client.spawn_gdb", return_value=gdb_mock
        ):
            debug.start_session(elf, halt=False)
        assert spawn_gdbs.call_args.kwargs["handshake_timeout_s"] == 3.0

    def test_connect_timeout_passed_to_spawn_gdb(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        gdbserver_mock = MagicMock(pid=1, port=61234)
        gdb_mock = MagicMock(pid=2)
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver",
            return_value=gdbserver_mock,
        ), patch(
            "embedagents.stm32.debug.client.spawn_gdb", return_value=gdb_mock
        ) as spawn_gdb_mock:
            debug.start_session(elf, halt=False)
        # Per-step cap of 10 s, clipped to the (barely-touched) 30 s
        # session budget → effectively 10.
        connect_timeout = spawn_gdb_mock.call_args.kwargs["connect_timeout_s"]
        assert 0 < connect_timeout <= 10.0

    def test_session_budget_exhausted_during_port_walk_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        ctx = _ctx_with_defaults(
            tmp_path, monkeypatch, {"session_start_timeout_s": 1}
        )
        elf = tmp_path / "demo.elf"
        elf.write_text("")
        debug = Debug(ctx)
        # Each monotonic() call advances 0.6 s: deadline lands at 1.0;
        # the second loop iteration sees the budget blown.
        clock = iter([0.0, 0.6, 1.2, 1.8, 2.4, 3.0])
        monkeypatch.setattr(
            "embedagents.stm32.debug.client.time",
            SimpleNamespace(monotonic=lambda: next(clock)),
        )
        spawn_gdbs = MagicMock(
            side_effect=GDBError(message="busy", gdb_marker="port-busy")
        )
        with patch(
            "embedagents.stm32.debug.client.spawn_gdbserver", spawn_gdbs
        ):
            with pytest.raises(GDBError) as excinfo:
                debug.start_session(elf)
        assert excinfo.value.gdb_marker == "command-timeout"
        assert "session_start_timeout_s" in (excinfo.value.hint or "")
        # One spawn attempt happened before the budget blew.
        assert spawn_gdbs.call_count == 1


# ---------------------------------------------------------------------------
# svd_for_attached unblock (cubeprogrammer D-008)
# ---------------------------------------------------------------------------


class TestSvdForAttached:
    def test_finds_path_via_ctx_svd_db(
        self,
        ctx: SubstrateContext,
        tmp_path: Path,
    ) -> None:
        # Plant a fake CubeIDE plugin SVD tree so ctx.svd_db has a root.
        plugin = (
            tmp_path
            / "stm32cubeide"
            / "plugins"
            / "com.st.stm32cube.ide.mcu.productdb.debug_2.0.0"
            / "resources"
            / "cmsis"
            / "STMicroelectronics_CMSIS_SVD"
        )
        plugin.mkdir(parents=True)
        (plugin / "STM32L476.svd").write_text("<device/>")

        # Rebuild ctx with a CubeIDE path pointing at the plugin tree.
        # The plugin glob looks at <cubeide>/plugins/...; we point cubeide
        # at the directory above 'plugins'.
        # Use monkeypatch to point STM32CUBEIDE at the install root.
        import os

        cubeide_root = tmp_path / "stm32cubeide"
        cubeide_root.mkdir(exist_ok=True)
        os.environ["STM32CUBEIDE"] = str(cubeide_root)
        try:
            ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
            client = CubeProgrammer(ctx2)
            # Mock connect() to return a banner with device_name=STM32L476RG.
            from embedagents.stm32.cubeprogrammer.results import BannerResult

            fake_banner = BannerResult(
                stlink_sn="X",
                stlink_fw="V",
                board_name="NUCLEO-L476RG",
                voltage_v=3.28,
                swd_freq_khz=4000,
                device_id="0x415",
                device_name="STM32L476RG",
                device_type="MCU",
                device_cpu="Cortex-M4",
                flash_size_kb=1024,
            )
            from unittest.mock import patch

            with patch.object(client, "connect", return_value=fake_banner):
                result = client.svd_for_attached()
            assert result.svd_path is not None
            assert result.svd_path.name == "STM32L476.svd"
            assert result.device_name == "STM32L476RG"
        finally:
            del os.environ["STM32CUBEIDE"]

    def test_family_wildcard_banner_falls_back_to_descriptor_mcu(
        self, tmp_path: Path
    ) -> None:
        # The STM32U0 banner is a family-only wildcard (``STM32U0xx``) that
        # find_for can't pin to a device SVD; svd_for_attached must fall
        # back to the descriptor's exact chip (board.mcu). Regression guard
        # for the U083RC bring-up.
        import os
        from unittest.mock import patch

        from embedagents.stm32.cubeprogrammer.results import BannerResult

        plugin = (
            tmp_path
            / "stm32cubeide"
            / "plugins"
            / "com.st.stm32cube.ide.mcu.productdb.debug_2.0.0"
            / "resources"
            / "cmsis"
            / "STMicroelectronics_CMSIS_SVD"
        )
        plugin.mkdir(parents=True)
        (plugin / "STM32U083.svd").write_text("<device/>")
        (tmp_path / "stm32-project.jsonc").write_text(
            '{"version": 1, "board": {"mcu": "STM32U083RCTx"}}'
        )

        cubeide_root = tmp_path / "stm32cubeide"
        os.environ["STM32CUBEIDE"] = str(cubeide_root)
        try:
            ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
            client = CubeProgrammer(ctx2)
            fake_banner = BannerResult(
                stlink_sn="X",
                stlink_fw="V",
                board_name="NUCLEO-U083RC",
                voltage_v=3.3,
                swd_freq_khz=4000,
                device_id="0x489",
                device_name="STM32U0xx",
                device_type="MCU",
                device_cpu="Cortex-M0+",
                flash_size_kb=256,
            )
            with patch.object(client, "connect", return_value=fake_banner):
                result = client.svd_for_attached()
            assert result.svd_path is not None
            assert result.svd_path.name == "STM32U083.svd"
            # device_name stays the verbatim banner string.
            assert result.device_name == "STM32U0xx"
        finally:
            del os.environ["STM32CUBEIDE"]

    def test_no_svd_db_raises(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        # Force ctx.svd_db to None.
        object.__setattr__(ctx, "svd_db", None)
        client = CubeProgrammer(ctx)
        with pytest.raises(ConfigurationError) as excinfo:
            client.svd_for_attached()
        assert "svd" in excinfo.value.message.lower()

    def test_no_path_found_returns_none(
        self, ctx: SubstrateContext, tmp_path: Path
    ) -> None:
        # ctx.svd_db is non-None (default empty roots) but find_for returns None.
        client = CubeProgrammer(ctx)
        from embedagents.stm32.cubeprogrammer.results import BannerResult
        from unittest.mock import patch

        fake_banner = BannerResult(
            stlink_sn="X",
            stlink_fw="V",
            board_name="X",
            voltage_v=3.0,
            swd_freq_khz=1,
            device_id="0x0",
            device_name="STM32UNKNOWN",
            device_type="MCU",
            device_cpu="Cortex",
            flash_size_kb=0,
        )
        with patch.object(client, "connect", return_value=fake_banner):
            result = client.svd_for_attached()
        assert result.svd_path is None
        assert result.device_name == "STM32UNKNOWN"


# ---------------------------------------------------------------------------
# CLI: stm32 debug start + svd-path
# ---------------------------------------------------------------------------


class TestCLI:
    def test_svd_path_no_match(
        self, ctx: SubstrateContext, capsys: pytest.CaptureFixture
    ) -> None:
        from embedagents.stm32.cli import main

        code = main(["debug", "svd-path", "STM32UNKNOWN"])
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["device_name"] == "STM32UNKNOWN"
        assert payload["svd_path"] is None

    def test_help_lists_subcommands(
        self, ctx: SubstrateContext, capsys: pytest.CaptureFixture
    ) -> None:
        from embedagents.stm32.cli import main

        with pytest.raises(SystemExit):
            main(["debug", "--help"])
        out = capsys.readouterr().out
        assert "start" in out
        assert "svd-path" in out
