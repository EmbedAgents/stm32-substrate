"""``Debug`` — lifecycle entry point.

Constructs ``DebugSession`` instances; long-lived state lives on the
session (context manager). Validates ELF / port / active-session /
n6_dev_mode prerequisites before spawning subprocesses per HIL.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from stm32_substrate.debug.gdb import spawn_gdb
from stm32_substrate.debug.gdbserver import GDBServerOptions, spawn_gdbserver
from stm32_substrate.debug.session import DebugSession, _debug_default
from stm32_substrate.errors import ConfigurationError, GDBError
from stm32_substrate.resolution import resolve_file

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.progress import ProgressCallback


_DEFAULT_GDB_PORT = 61234
_DEFAULT_PORT_FALLBACK_RANGE: tuple[int, ...] = (
    61234, 61235, 61236, 61237, 61238, 61239, 61240, 61241, 61242, 61243,
)


class Debug:
    """Lifecycle entry point. Returns ``DebugSession`` context managers."""

    def __init__(self, ctx: "SubstrateContext") -> None:
        self.ctx = ctx
        self._gdbserver_bin = ctx.tools.stlink_gdbserver
        self._gdb_bin = ctx.tools.arm_gdb
        self._log = ctx.logger.getChild("debug")

    def start_session(
        self,
        elf_path: Path | None = None,
        *,
        halt: bool = True,
        port: int | None = None,
        n6_dev_mode: bool = False,
        on_n6_boot_confirm: Callable[[], bool] | None = None,
        on_progress: "ProgressCallback | None" = None,
    ) -> DebugSession:
        """Spawn gdbserver + arm-gdb; return a ``DebugSession``.

        Validation per ``v1/debug-api.md``:

        - ELF resolution per R-002 (kwarg → descriptor → ``ConfigurationError``).
        - Active-session check: ``ctx.session_state.active_debug_session
          is None`` else ``GDBError(gdb_marker="session-already-active")``.
        - Port resolution: explicit ``port`` if free, else walk the
          fallback range until a free port is found, else
          ``GDBError(gdb_marker="no-free-gdb-port")``.
        - N6 dev-mode: ``on_n6_boot_confirm`` must return True when
          ``n6_dev_mode=True`` (RES-020); else
          ``GDBError(gdb_marker="n6-boot-not-confirmed")``.
        """
        elf = self._resolve_elf(elf_path)
        self._check_active_session()
        self._check_n6_descriptor_requires_flag(n6_dev_mode)
        if n6_dev_mode:
            self._check_n6_boot(on_n6_boot_confirm)

        cube_programmer_cli_dir = self._resolve_cube_programmer_cli_dir()

        # Overall lifecycle budget (debug.session_start_timeout_s):
        # gdbserver spawn + handshake + arm-gdb spawn + target-select +
        # optional `monitor reset halt`. The gdbserver handshake has its
        # own sub-budget (debug.gdbserver_spawn_timeout_s) inside it.
        handshake_budget_s = _debug_default(
            self.ctx, "gdbserver_spawn_timeout_s", 5.0
        )
        deadline = time.monotonic() + _debug_default(
            self.ctx, "session_start_timeout_s", 30.0
        )

        gdbserver = None
        last_port_error: GDBError | None = None
        for candidate_port in self._port_iter(port):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GDBError(
                    message=(
                        "start_session exceeded debug.session_start_timeout_s "
                        "while walking the gdb port fallback range"
                    ),
                    gdb_marker="command-timeout",
                    hint="raise debug.session_start_timeout_s",
                ) from last_port_error
            options = GDBServerOptions(
                port=candidate_port,
                cube_programmer_cli_dir=cube_programmer_cli_dir,
                halt_on_attach=halt,
                persistent=True,
                stlink_serial=self.ctx.default_probe_sn,
                n6_dev_mode=n6_dev_mode,
            )
            try:
                gdbserver = spawn_gdbserver(
                    ctx=self.ctx,
                    options=options,
                    handshake_timeout_s=min(handshake_budget_s, remaining),
                )
                break
            except GDBError as ex:
                if ex.gdb_marker == "port-busy":
                    last_port_error = ex
                    self._log.info(
                        "port %d busy; trying next candidate", candidate_port
                    )
                    continue
                raise

        if gdbserver is None:
            raise GDBError(
                message="no free gdb port in fallback range",
                gdb_marker="no-free-gdb-port",
                hint=(
                    "close conflicting processes or widen "
                    "debug.gdb_port_fallback_range"
                ),
            ) from last_port_error

        # IMP-11: catch BaseException, not just GDBError — an unexpected
        # error type leaking past here would orphan the persistent
        # gdbserver, which keeps holding the singleton ST-LINK probe and
        # fails every subsequent operation.
        try:
            gdb = spawn_gdb(
                ctx=self.ctx,
                elf_path=elf,
                gdb_port=gdbserver.port,
                connect_timeout_s=self._remaining_step_budget(deadline, 10.0),
            )
        except BaseException:
            gdbserver.close()
            raise

        if halt:
            try:
                # ST-LINK gdbserver Rcmd syntax (bench-verified v7.13.0,
                # RES-041): plain `monitor reset` system-resets AND holds
                # the core halted at Reset_Handler while a debugger is
                # attached. The OpenOCD-flavored `monitor reset halt` is
                # REJECTED ("Unknown reset option" → ^error) — it never
                # actually reset anything and raises since IMP-02.
                gdb.send_console(
                    "monitor reset",
                    timeout_s=self._remaining_step_budget(deadline, 10.0),
                )
            except BaseException:
                gdb.close()
                gdbserver.close()
                raise

        session = DebugSession(
            ctx=self.ctx,
            gdbserver=gdbserver,
            gdb=gdb,
            elf_path=elf,
            n6_dev_mode_confirmed=n6_dev_mode,
        )
        session.target_halted = halt
        # Register immediately so cross-module callers see the live
        # session even before the user's ``with`` block fires __enter__.
        self.ctx.session_state.active_debug_session = session
        self._log.info(
            "start_session pid_gdbserver=%s pid_gdb=%s port=%d halt=%s",
            session.gdbserver_pid,
            session.gdb_pid,
            session.gdb_port,
            halt,
        )
        return session

    def attach_running(
        self,
        elf_path: Path | None = None,
        *,
        port: int | None = None,
    ) -> DebugSession:
        """Alias for ``start_session(halt=False)``."""
        return self.start_session(elf_path, halt=False, port=port)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_elf(self, elf_path: "Path | str | None") -> Path:
        # R-002 via resolution.resolve_file: str|Path tolerated; a
        # relative descriptor elf_path anchors to ctx.cwd, not the
        # process CWD (IMP-22 / IMP-23).
        resolved = resolve_file(
            elf_path,
            ctx=self.ctx,
            descriptor_field="debug.elf_path",
            arg_name="elf_path",
        )
        assert resolved is not None  # required=True
        return resolved

    def _check_active_session(self) -> None:
        if self.ctx.session_state.active_debug_session is not None:
            raise GDBError(
                message="another debug session is already active on this workspace",
                gdb_marker="session-already-active",
                hint=(
                    "close the existing session first; v1 disallows "
                    "concurrent sessions per workspace"
                ),
            )

    def _check_n6_descriptor_requires_flag(self, n6_dev_mode: bool) -> None:
        """If descriptor declares STM32N6 family, require ``n6_dev_mode=True``.

        N6 silicon needs the BOOT switch in dev mode before debug can attach
        (UM2576). The flag is the user's acknowledgement that the switch is
        set; substrate refuses to spawn gdbserver without it when the
        descriptor identifies the target as N6.
        """
        if n6_dev_mode:
            return
        descriptor = self.ctx.project
        firmware = getattr(descriptor, "firmware", None) if descriptor else None
        device_family = (
            getattr(firmware, "device_family", None) if firmware else None
        )
        if device_family and str(device_family).startswith("STM32N6"):
            raise ConfigurationError(
                message=(
                    f"descriptor declares STM32N6 family ({device_family!r}); "
                    "N6 debug requires --n6-dev-mode"
                ),
                hint=(
                    "set the BOOT switch to dev mode, then re-invoke with "
                    "--n6-dev-mode to confirm"
                ),
            )

    def _check_n6_boot(
        self, on_n6_boot_confirm: Callable[[], bool] | None
    ) -> None:
        if on_n6_boot_confirm is None or not on_n6_boot_confirm():
            raise GDBError(
                message=(
                    "n6_dev_mode=True requires on_n6_boot_confirm() to return "
                    "True (confirming BOOT switch in dev position)"
                ),
                gdb_marker="n6-boot-not-confirmed",
                hint=(
                    "pass on_n6_boot_confirm=Callable[[], bool] returning "
                    "True after the user confirms the BOOT switch position"
                ),
                recoverable=True,
            )

    def _resolve_cube_programmer_cli_dir(self) -> Path:
        cli = self.ctx.tools.cube_programmer_cli
        if cli is None:
            raise ConfigurationError(
                message=(
                    "STM32_Programmer_CLI path required by gdbserver -cp arg"
                ),
                hint=(
                    "Set programmer.cube_programmer_path in "
                    ".claude/stm32-tools.local.jsonc; gdbserver delegates "
                    "flash writes through CubeProgrammer (UM2576 §1)."
                ),
            )
        return cli.parent

    @staticmethod
    def _remaining_step_budget(deadline: float, step_cap_s: float) -> float:
        """Per-step timeout: the step's own cap, clipped to what's left of
        the ``debug.session_start_timeout_s`` deadline (floored at 0.1 s so
        an already-expired budget still surfaces as a step timeout rather
        than an invalid non-positive timeout)."""
        return max(min(step_cap_s, deadline - time.monotonic()), 0.1)

    def _port_iter(self, explicit: int | None):
        """Yield candidate gdb ports.

        - Explicit ``port=N`` → only try N.
        - Default → try ``debug.gdb_port`` first when configured, then
          walk ``debug.gdb_port_fallback_range`` (list of ints declared
          by the schema), falling back to the canonical 61234..61243
          range when the descriptor doesn't override.
        """
        if explicit is not None:
            yield explicit
            return
        debug_defaults = getattr(self.ctx.defaults, "debug", None)
        initial = (
            getattr(debug_defaults, "gdb_port", None)
            if debug_defaults
            else None
        )
        configured_range = (
            getattr(debug_defaults, "gdb_port_fallback_range", None)
            if debug_defaults
            else None
        )
        candidates = [
            int(p)
            for p in (
                configured_range
                if configured_range
                else _DEFAULT_PORT_FALLBACK_RANGE
            )
        ]
        if initial is not None:
            yield int(initial)
            candidates = [p for p in candidates if p != int(initial)]
        yield from candidates
