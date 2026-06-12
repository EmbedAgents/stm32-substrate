"""``CubeProgrammer`` — STM32_Programmer_CLI wrapper.

Skeleton (B1). Every public method declares its signature + docstring and
raises ``NotImplementedError``. Subsequent B-phases (B2..B11) fill the
bodies and wire parsers / D-002 ladder / external-loader discovery / gdb
routing / CLI subcommands.

Probe selection is implicit: ``ctx.default_probe_sn`` is forwarded as
``sn=<value>`` on every CLI invocation when set. No per-call ``sn=``
override in v1.

Cross-module touchpoints (read-only from this class):

- ``ctx.tools.cube_programmer_cli`` — path; loud
  ``ConfigurationError`` raised lazily on first use when unset.
- ``ctx.session_state.active_debug_session`` — gdb routing for
  ``reset()`` / ``halt()`` / ``resume()`` (set by ``debug``).
- ``ctx.svd_db`` — read by D-008 and F-020 sr_or_dr_warning detection
  (set by ``debug``).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator, TYPE_CHECKING

from embedagents.stm32.cubeprogrammer import diagnose, external_loader, parsers
from embedagents.stm32.errors import (
    ConfigurationError,
    CubeProgrammerError,
    ProtocolError,
    ResolutionError,
    ToolError,
    UserAbortedError,
)
from embedagents.stm32.resolution import coerce_path
from embedagents.stm32.subprocess_runner import run_tool
from embedagents.stm32.cubeprogrammer.results import (
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    EraseConfirmation,
    FlashConfirmation,
    HardFaultDecode,
    ITMRecord,
    MemoryLayoutResult,
    MemoryReadResult,
    OptionByteDiffEntry,
    OptionBytesDiff,
    OptionBytesResult,
    PairFlashResult,
    ProbeRecord,
    RecoveryResult,
    ResetConfirmation,
    SVDResult,
)

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext
    from embedagents.stm32.progress import ProgressCallback


# Address regex per SC-003: hex literal with 0x prefix.
_ADDRESS_RE = re.compile(r"^0x[0-9A-Fa-f]+$")

# ST image-header magic (UM2543): STM32_SigningTool_CLI prepends a header
# whose first four bytes are ASCII "STM2" across all header versions
# (1 / 2 / 2.x). Used by flash_signed_pair(sign_unsigned=True) to decide
# which inputs still need the F-013 signing pass.
_ST_IMAGE_HEADER_MAGIC = b"STM2"


def _has_signed_header(path: Path) -> bool:
    """``True`` when ``path`` starts with the ST image-header magic."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == _ST_IMAGE_HEADER_MAGIC
    except OSError:
        return False


def _file_size_or_zero(path: Path) -> int:
    """``path.stat().st_size`` or ``0`` if the file is unreadable.

    Substrate uses the input file size as ``bytes_written`` in
    ``FlashConfirmation``. The CLI emits a ``Time elapsed`` line but the
    on-disk size is more reliable and avoids fragile output parsing
    (per the substrate-captures-doesn't-interpret rule).
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Option-byte value helpers (B8)
# ---------------------------------------------------------------------------


def _is_rdp_level_2(value: int | str | bool) -> bool:
    """Return True if ``value`` is the RDP level-2 byte (0xCC).

    Accepts the byte as ``int`` / ``"0xCC"`` / decimal-string ``"204"``.
    Booleans are not RDP values; return False.
    """
    if isinstance(value, bool):
        return False
    num = _coerce_to_int(value)
    return num == 0xCC


def _render_ob_value(value: int | str | bool) -> str:
    """Format an OB value for the CLI ``NAME=VALUE`` argument.

    - ``bool`` → ``"0x1"`` / ``"0x0"`` (canonical hex).
    - ``int`` → ``f"0x{v:x}"``.
    - ``str`` → passthrough (caller's formatting wins; e.g. ``"0xAA"``).
    """
    if isinstance(value, bool):
        return "0x1" if value else "0x0"
    if isinstance(value, int):
        return f"0x{value:x}"
    return str(value)


def _ob_values_equal(
    observed: int | str | bool | None, expected: int | str | bool
) -> bool:
    """Compare two OB values for equality after numeric normalisation.

    Normalises both sides to ``int`` when feasible so ``"0xAA"`` matches
    ``0xAA`` matches ``170``; ``True`` matches ``1``; ``False`` matches
    ``0``. Falls back to string-equality for genuinely non-numeric
    values. ``None`` (field absent from observed) is never equal.
    """
    if observed is None:
        return False
    obs_num = _coerce_to_int(observed)
    exp_num = _coerce_to_int(expected)
    if obs_num is not None and exp_num is not None:
        return obs_num == exp_num
    return str(observed) == str(expected)


def _coerce_to_int(value: int | str | bool | None) -> int | None:
    """Return ``value`` as ``int`` when possible; ``None`` otherwise.

    Booleans coerce to 0 / 1. Hex strings (``"0xAA"``) and decimal
    strings (``"170"``) parse to ``int``. Anything else returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            if value.lower().startswith("0x"):
                return int(value, 16)
            return int(value)
        except ValueError:
            return None
    return None


class CubeProgrammer:
    """Wrapper around STM32_Programmer_CLI. One per SubstrateContext.

    Methods are 1:1 with prompts D-* + F-* + the CubeProgrammer-side of
    DIAG-001 / DIAG-018 / VCP-007. T2 orchestrators (``diagnose_micro``,
    ``flash_pair``, ``flash_external``, ``download_image``,
    ``verify_option_bytes``) live in this class because they only call
    other CubeProgrammer methods. Multi-tool compounds live in
    ``src/embedagents/stm32/compound/``.
    """

    def __init__(self, ctx: "SubstrateContext") -> None:
        self.ctx = ctx
        self._cli: Path | None = ctx.tools.cube_programmer_cli
        self._log = ctx.logger.getChild("cubeprogrammer")

    # ------------------------------------------------------------------
    # helpers (used by per-method bodies in B3+)
    # ------------------------------------------------------------------

    def _require_cli(self) -> Path:
        """Return the validated CLI path or raise the loud ConfigurationError.

        Per ``v1/cubeprogrammer-api.md`` § "Substrate-shared errors raised
        by this module".
        """
        if self._cli is None:
            raise ConfigurationError(
                message="STM32_Programmer_CLI path not configured",
                hint=(
                    "Set programmer.cube_programmer_path in "
                    ".claude/stm32-tools.local.jsonc, or set the "
                    "STM32_PROGRAMMER_CLI environment variable. "
                    "Auto-discovery on PATH attempted; binary not found."
                ),
            )
        return self._cli

    def _sn_args(self) -> list[str]:
        """Return ``["sn=<value>"]`` when a default probe is configured, else ``[]``."""
        if self.ctx.default_probe_sn:
            return [f"sn={self.ctx.default_probe_sn}"]
        return []

    def _active_debug_session(self) -> object | None:
        """Cross-module read of ``ctx.session_state.active_debug_session``.

        Returns the live ``DebugSession`` instance (typed ``object`` here to
        avoid an import cycle; debug module owns the class).
        """
        return self.ctx.session_state.active_debug_session

    def _timeout_s(self, knob: str, default: float) -> float:
        """Look up a ``ctx.defaults.programmer.<knob>`` value or fall back."""
        programmer = getattr(self.ctx.defaults, "programmer", None)
        if programmer is None:
            return default
        return float(getattr(programmer, knob, default))

    def _invoke(self, args: list[str], *, timeout_s: float) -> str:
        """Run the CLI and return stdout, raising a typed
        ``CubeProgrammerError`` on non-zero exit / timeout.

        Common path for every D-* / F-* method: build args, route through
        ``run_tool``, and translate ``ToolError`` raised by the runner
        into a ``CubeProgrammerError`` carrying the parsed
        ``error_code`` + recoverability flag per ``parse_error``.
        """
        cli = self._require_cli()
        try:
            result = run_tool(cli, args, ctx=self.ctx, timeout_s=timeout_s)
        except ToolError as ex:
            raise self._translate_tool_error(ex) from ex
        return result.stdout

    @staticmethod
    def _translate_tool_error(ex: ToolError) -> CubeProgrammerError:
        """Map a runner ``ToolError`` onto a typed ``CubeProgrammerError``.

        IMP-03: a runner timeout carries ``code="timeout"`` plus a real
        message + hint — mapping it through ``parse_error`` rendered the
        misleading "exited with code -1" and dropped the hint.
        """
        if ex.code == "timeout":
            return CubeProgrammerError(
                message=ex.message,
                code="timeout",
                tool_output=ex.tool_output,
                hint=ex.hint,
                recoverable=False,
            )
        stderr = ex.tool_output or ""
        exit_code = ex.code if isinstance(ex.code, int) else -1
        return parsers.parse_error(stderr, exit_code)

    def _raw_connect(
        self,
        *,
        mode: str | None = None,
        freq_khz: int | None = None,
    ) -> BannerResult:
        """Single-attempt SWD connect with no logging or recovery.

        Building block for ``connect()`` / ``connect_under_reset()`` /
        ``ping_swd()`` and the D-002 ladder. Returns the parsed banner on
        success; raises typed ``CubeProgrammerError`` on any non-zero CLI
        exit — caller decides whether to retry, escalate, or surface.
        """
        args: list[str] = ["-c", "port=swd"]
        if mode is not None:
            args.append(f"mode={mode}")
        args += self._sn_args()
        if freq_khz is not None:
            args.append(f"freq={freq_khz}")
        timeout_s = self._timeout_s("connect_timeout_s", 30.0)
        stdout = self._invoke(args, timeout_s=timeout_s)
        return parsers.parse_banner(stdout)

    def _log_banner(self, banner: BannerResult) -> None:
        """Standard INFO + WARNING emit applied by ``connect()`` /
        ``connect_under_reset()`` (skipped by ``_raw_connect`` so the
        D-002 ladder controls its own logging cadence)."""
        self._log.info(
            "connected device=%s board=%s mode=%s freq=%dkHz",
            banner.device_name,
            banner.board_name,
            banner.mode_used,
            banner.swd_freq_khz,
        )
        if banner.voltage_suspicious:
            self._log.warning(
                "suspicious target voltage %.2fV (below 2.5V threshold)",
                banner.voltage_v,
            )

    def _validate_address(self, address: str) -> str:
        """Validate a hex flash / memory address per SC-003.

        Raises ``ValueError`` on a malformed address; returns the input
        unchanged when valid. Use at every method entrypoint that accepts
        an ``address: str`` kwarg — substrate refuses bad input rather
        than letting the CLI surface a less helpful error.
        """
        if not _ADDRESS_RE.match(address):
            raise ValueError(
                f"invalid flash address {address!r}; expected "
                "hex literal like '0x08000000' (regex ^0x[0-9A-Fa-f]+$)"
            )
        return address

    def _flash_timeout_s(self, path: Path) -> float:
        """Compute the flash timeout from ``base + per_mb * size_mb``.

        Uses ``programmer.flash_timeout_base_s`` + ``flash_timeout_per_mb_s``
        from runtime defaults (120 s base + 10 s/MB per spec) so large
        payloads don't trip the wrapper before the CLI returns.
        """
        base = self._timeout_s("flash_timeout_base_s", 120.0)
        per_mb = self._timeout_s("flash_timeout_per_mb_s", 10.0)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            # Pre-flash path checks happen later (the CLI surfaces
            # missing-file errors). Stick to base.
            return base
        size_mb = size_bytes / (1024 * 1024)
        return base + per_mb * size_mb

    # ------------------------------------------------------------------
    # discovery (D-*)
    # ------------------------------------------------------------------

    def connect(self, *, freq_khz: int | None = None) -> BannerResult:
        """D-001 — connect via SWD and return the captured banner.

        Forwards ``ctx.default_probe_sn`` as ``sn=<value>`` when set.
        Optional ``freq_khz`` overrides the device-default SWD frequency.
        Raises ``CubeProgrammerError`` on any non-zero exit; the exception
        carries the parsed ``error_code`` and recovery hint.
        """
        banner = self._raw_connect(freq_khz=freq_khz)
        self._log_banner(banner)
        return banner

    def connect_under_reset(self) -> BannerResult:
        """D-011 — connect with ``mode=UR`` (no fallback).

        Useful when the target firmware aggressively disables SWD pins
        early in boot (sets BOOT pins, disables AF on PA13/PA14, etc.).
        Holding reset asserted lets the probe latch the debug interface
        before the boot loader runs. No recovery ladder — single attempt;
        callers escalate to ``diagnose_micro()`` on failure.
        """
        banner = self._raw_connect(mode="UR")
        self._log_banner(banner)
        return banner

    def board_name(self) -> str:
        """D-003 — banner subset view; raises ``ResolutionError`` when the
        connected board reports ``Board: --`` or omits the line.

        Custom boards / ST devkits without a board-id chip have no
        catalog name. The HIL contract is "raise loudly" — callers either
        set ``firmware.board`` in the descriptor or pick a different
        prompt that does not require a catalog board name.
        """
        banner = self.connect()
        if banner.board_name is None:
            raise ResolutionError(
                message="connected board has no catalog name",
                hint=(
                    "the banner reported Board: -- (custom board or board-id "
                    "chip absent); set firmware.board in stm32-project.jsonc "
                    "to override, or use board-name-agnostic prompts"
                ),
                recoverable=False,
            )
        return banner.board_name

    def memory_layout(self) -> MemoryLayoutResult:
        """D-004 — banner subset view. ``ram_size_kb`` and ``bank_layout``
        are ``None`` in v1: the banner alone does not expose them, and
        substrate dropped the DeviceDB pathway per RES-020 + the cubemx
        scope cut. Callers handle ``None`` gracefully (typically surface
        "RAM size unknown for this device" rather than raise). TODO(v1+):
        hardcoded mini-tables once a real consumer needs precision.
        """
        banner = self.connect()
        return MemoryLayoutResult(
            flash_size_kb=banner.flash_size_kb,
            ram_size_kb=None,
            device_name=banner.device_name,
            bank_layout=None,
        )

    def cores(self) -> CoresResult:
        """D-007 — banner subset view. ``primary_core`` comes from
        ``banner.device_cpu``; ``secondary_cores`` is ``[]`` and
        ``multi_core`` is ``None`` when the banner does not expose
        multi-core hints (post-cubemx scope cut, no DeviceDB)."""
        banner = self.connect()
        return CoresResult(
            device_name=banner.device_name,
            primary_core=banner.device_cpu,
            secondary_cores=[],
            multi_core=None,
        )

    def list_probes(self) -> list[ProbeRecord]:
        """D-005 — ``STM32_Programmer_CLI -l`` enumeration.

        Empty list is a valid result (no probes attached), not an error.
        Multidrop ``target_sel`` queries are deferred — v1 always returns
        ``target_sel=None`` per ``ProbeRecord``. TODO(v1+): optional
        ``-getTargetSelList`` per probe per UM2237 §3.2.10.
        """
        cli = self._require_cli()
        timeout_s = self._timeout_s("connect_timeout_s", 30.0)
        # raise_on_nonzero=False keeps the runner from raising so the
        # exit-code policy lives here. NOTE (IMP-28, re-deferred): the
        # check below raises on ANY nonzero exit — including a
        # version-variant 'no probes detected' nonzero report, which
        # would ideally return []. Distinguishing that case safely
        # needs a captured real banner fixture for the nonzero-empty
        # variant; a lenient parse-anything fallback would mask real
        # failures as empty lists.
        try:
            result = run_tool(
                cli, ["-l"], ctx=self.ctx, timeout_s=timeout_s, raise_on_nonzero=False
            )
        except ToolError as ex:
            # ToolError here means the runner saw a hard problem
            # (timeout, subprocess kill). Translate to typed error.
            raise self._translate_tool_error(ex) from ex

        if result.exit_code != 0:
            stderr = result.stderr or result.stdout
            raise parsers.parse_error(stderr, result.exit_code)

        probes = parsers.parse_probe_list(result.stdout)
        self._log.info("list_probes detected %d probe(s)", len(probes))
        return probes

    def ping_swd(self) -> BooleanResult:
        """D-006 — fast SWD-responsiveness probe.

        Single ``mode=NORMAL`` connect; success → ``BooleanResult(True)``;
        any ``CubeProgrammerError`` is captured as the ``reason`` string
        on a ``BooleanResult(False)``. Does NOT escalate to ``diagnose_micro``
        — the caller decides whether a False result is interesting enough
        to walk the D-002 ladder.
        """
        try:
            self._raw_connect(mode="NORMAL")
        except CubeProgrammerError as ex:
            self._log.info("ping_swd: target unresponsive (%s)", ex.message)
            return BooleanResult(value=False, reason=ex.message)
        self._log.info("ping_swd: target responding")
        return BooleanResult(value=True, reason=None)

    def svd_for_attached(self) -> SVDResult:
        """D-008 — SVD lookup via ``ctx.svd_db`` for the attached device.

        Reads the device name from a fresh banner (``connect()``), then
        asks ``ctx.svd_db.find_for(device_name)``. Returns the resolved
        path + the candidate list when the lookup is ambiguous.

        CubeProgrammer 2.22 emits a multi-family glob in the ``Device
        name`` banner field — e.g. ``STM32L4x1/STM32L475xx/STM32L476xx/
        STM32L486xx``. Substrate splits on ``/`` and walks the variants
        in order; the first variant that resolves wins. The reported
        ``device_name`` keeps the verbatim banner string so the caller
        can see exactly what the CLI emitted.

        Some banners name only the family (e.g. the STM32U0's ``STM32U0xx``
        — no subfamily digit, so ``find_for`` can't pick between
        ``STM32U031/U073/U083``). When the banner doesn't resolve, fall
        back to the descriptor's exact chip (``board.mcu`` — the same
        source ``DebugSession`` trusts for peripheral reads); ``device_name``
        still reports the verbatim banner.
        """
        if self.ctx.svd_db is None:
            raise ConfigurationError(
                message="ctx.svd_db is unset; cannot resolve SVD path",
                hint=(
                    "SVD lookup is populated by SubstrateContext.from_environment(); "
                    "ensure at least one of cubeide / cube_programmer / "
                    "stm32cubeclt tool paths is configured so the SVD root is "
                    "discoverable."
                ),
            )
        banner = self.connect()
        svd_path = None
        for variant in banner.device_name.split("/"):
            variant = variant.strip()
            if not variant:
                continue
            svd_path = self.ctx.svd_db.find_for(variant)
            if svd_path is not None:
                break
        if svd_path is None:
            board = getattr(self.ctx.project, "board", None) if self.ctx.project else None
            mcu = getattr(board, "mcu", None) if board else None
            if mcu:
                svd_path = self.ctx.svd_db.find_for(str(mcu))
        self._log.info(
            "svd_for_attached device=%s → %s",
            banner.device_name,
            svd_path,
        )
        return SVDResult(
            device_name=banner.device_name,
            device_id=banner.device_id,
            svd_path=svd_path,
            svd_version=None,  # substrate doesn't parse the SVD version header
        )

    def read_option_bytes(self) -> OptionBytesResult:
        """D-009 — ``-c port=swd -ob displ``; raw key→value mapping.

        Captures every OB field surfaced by the CLI as
        ``{name: value}`` in ``observed`` (hex values parsed to int,
        plain integers parsed, everything else kept as string). The
        universal RDP byte is mapped to a 0/1/2 level on top.

        Single CLI call: stdout carries both the banner (for
        ``device_name``) and the OB section.
        """
        args: list[str] = ["-c", "port=swd"]
        args += self._sn_args()
        args += ["-ob", "displ"]
        timeout_s = self._timeout_s("connect_timeout_s", 30.0)
        stdout = self._invoke(args, timeout_s=timeout_s)
        banner = parsers.parse_banner(stdout)
        result = parsers.parse_option_bytes(stdout, device_name=banner.device_name)
        self._log.info(
            "read_option_bytes device=%s rdp_level=%s fields=%d",
            result.device_name,
            result.rdp_level,
            len(result.observed),
        )
        return result

    def diagnose_micro(self) -> RecoveryResult:
        """D-002 — SWD recovery ladder (5 modes × 4 frequencies).

        Bounded by ``programmer.diagnose_timeout_s`` (default 120 s, per
        RES-020). ``target_responsive=False`` is a valid result, not an
        exception. See ``cubeprogrammer.diagnose.run_diagnose`` for the
        full algorithm.
        """
        timeout_s = self._timeout_s("diagnose_timeout_s", 120.0)
        return diagnose.run_diagnose(self, timeout_s=timeout_s)

    # ------------------------------------------------------------------
    # atomic target control (F-001/002/016/017/018)
    # ------------------------------------------------------------------

    def _confirm_destructive_or_abort(
        self,
        confirm_destructive: Callable[[list[str]], bool] | bool,
        targets: list[str],
    ) -> None:
        """Enforce the HIL destructive-op gate (HARD RULE 1).

        Per ``expected-behaviors-v2.md`` § destructive ops, every
        irreversible substrate operation (erase / option-byte / RDP)
        requires explicit consent. ``confirm_destructive`` is a ``bool``
        (programmatic callers) or a callable ``(targets) -> bool`` (the
        slash-command path wires ``AskUserQuestion``). Falsy → raise
        ``UserAbortedError`` (recoverable). Mirrors the
        ``write_option_bytes`` gate so every destructive surface behaves
        identically.
        """
        if isinstance(confirm_destructive, bool):
            approved = confirm_destructive
        else:
            approved = bool(confirm_destructive(list(targets)))
        if not approved:
            raise UserAbortedError(
                message=(
                    f"Destructive operation declined: {', '.join(targets)}. "
                    "Pass confirm_destructive=True (or a callable returning "
                    "True) to proceed."
                ),
                hint="re-call with confirm_destructive=True once the user agrees",
                recoverable=True,
            )

    def erase_chip(
        self,
        *,
        confirm_destructive: Callable[[list[str]], bool] | bool = False,
    ) -> EraseConfirmation:
        """F-001 — ``-c port=swd -e all`` mass erase.

        Destructive — wipes the entire flash. Gated per HIL convention
        (HARD RULE 1): ``confirm_destructive`` must resolve True — a
        ``bool`` for programmatic callers, or a callable the slash-command
        layer wires to ``AskUserQuestion``. Falsy → ``UserAbortedError``.
        """
        self._confirm_destructive_or_abort(
            confirm_destructive, ["mass erase — entire flash"]
        )
        args: list[str] = ["-c", "port=swd"] + self._sn_args() + ["-e", "all"]
        timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
        start = time.monotonic()
        self._invoke(args, timeout_s=timeout_s)
        duration_s = time.monotonic() - start
        self._log.info("erase_chip complete duration=%.2fs", duration_s)
        return EraseConfirmation(
            erase_complete=True, reset_issued=False, duration_s=duration_s
        )

    def erase_and_reset(
        self,
        *,
        confirm_destructive: Callable[[list[str]], bool] | bool = False,
    ) -> EraseConfirmation:
        """F-002 — ``-c port=swd -e all -rst`` mass erase + reset.

        Destructive — same HIL gate as ``erase_chip`` (HARD RULE 1):
        ``confirm_destructive`` must resolve True or this raises
        ``UserAbortedError``.
        """
        self._confirm_destructive_or_abort(
            confirm_destructive, ["mass erase + reset — entire flash"]
        )
        args: list[str] = (
            ["-c", "port=swd"] + self._sn_args() + ["-e", "all", "-rst"]
        )
        timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
        start = time.monotonic()
        self._invoke(args, timeout_s=timeout_s)
        duration_s = time.monotonic() - start
        self._log.info(
            "erase_and_reset complete duration=%.2fs", duration_s
        )
        return EraseConfirmation(
            erase_complete=True, reset_issued=True, duration_s=duration_s
        )

    def reset(self, *, hard: bool = False) -> ResetConfirmation:
        """F-016 — ``-rst`` (or ``-hardRst`` if ``hard=True``).

        Routes through ``ctx.session_state.active_debug_session.send_monitor``
        when a debug session is live, to avoid SWD-probe contention.
        Otherwise invokes ``STM32_Programmer_CLI`` directly.
        """
        sess = self._active_debug_session()
        if sess is not None:
            sess.send_monitor("reset")
            self._log.info("reset via gdb (hard=%s)", hard)
            return ResetConfirmation(reset_issued=True, via_gdb=True, hard=hard)
        flag = "-hardRst" if hard else "-rst"
        args: list[str] = ["-c", "port=swd"] + self._sn_args() + [flag]
        timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
        self._invoke(args, timeout_s=timeout_s)
        self._log.info("reset via cli (hard=%s)", hard)
        return ResetConfirmation(reset_issued=True, via_gdb=False, hard=hard)

    def halt(self) -> Confirmation:
        """F-017 — ``-halt``; routes through active gdb session when present.

        ``Confirmation.data.prior_state`` is ``"unknown"`` in v1 — the
        substrate does not probe target state before issuing halt. TODO
        once a state-probe helper lands.
        """
        sess = self._active_debug_session()
        via_gdb = sess is not None
        if sess is not None:
            # MI-level halt (-exec-interrupt) rather than `monitor halt`:
            # keeps gdb's own target-state machine in sync (RES-041).
            sess.halt()
        else:
            args: list[str] = ["-c", "port=swd"] + self._sn_args() + ["-halt"]
            timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
            self._invoke(args, timeout_s=timeout_s)
        self._log.info("halt via %s", "gdb" if via_gdb else "cli")
        return Confirmation(
            operation="halt",
            data={
                "halted": True,
                "prior_state": "unknown",
                "via_gdb": via_gdb,
            },
        )

    def resume(self) -> Confirmation:
        """F-018 — ``-run``; routes through active gdb session when present.

        Same ``prior_state="unknown"`` caveat as ``halt()``. The gdb-side
        path is MI ``-exec-continue`` via ``session.resume()`` — ST-LINK
        gdbserver has no resume-flavored Rcmd (``monitor continue`` /
        ``go`` / ``resume`` all ^error, bench-verified v7.13.0; RES-041).
        """
        sess = self._active_debug_session()
        via_gdb = sess is not None
        if sess is not None:
            sess.resume()
        else:
            args: list[str] = ["-c", "port=swd"] + self._sn_args() + ["-run"]
            timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
            self._invoke(args, timeout_s=timeout_s)
        self._log.info("resume via %s", "gdb" if via_gdb else "cli")
        return Confirmation(
            operation="resume",
            data={
                "running": True,
                "prior_state": "unknown",
                "via_gdb": via_gdb,
            },
        )

    # ------------------------------------------------------------------
    # read (F-019 / F-020)
    # ------------------------------------------------------------------

    def read_flash_to_file(
        self,
        *,
        address: str | None = None,
        size: int | None = None,
        output_path: Path | None = None,
        on_progress: "ProgressCallback | None" = None,
    ) -> Confirmation:
        """F-019 — ``-c port=swd -r32 <addr> <size> <file>``.

        Defaults when any kwarg is omitted (one connect() is issued to
        read the banner):

        - ``address=None`` → ``0x08000000`` (universal STM32 flash start;
          users with non-standard flash regions pass explicit values).
        - ``size=None`` → full flash size from ``banner.flash_size_kb``.
        - ``output_path=None`` → ``<cwd>/flash-<device>-<ts>.bin`` with
          ``device_name`` sanitised (``/`` and spaces → ``_``).

        ``on_progress`` is accepted but not yet wired — TODO once a
        progress-line parser lands.
        """
        if address is None or size is None or output_path is None:
            banner = self.connect()
            if address is None:
                address = "0x08000000"
            if size is None:
                size = banner.flash_size_kb * 1024
            if output_path is None:
                safe_name = (
                    banner.device_name.replace("/", "_").replace(" ", "_") or "stm32"
                )
                output_path = self.ctx.cwd / f"flash-{safe_name}-{int(time.time())}.bin"
        self._validate_address(address)
        if size <= 0:
            raise ValueError(f"read_flash_to_file size must be positive; got {size}")
        output_path = coerce_path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        args: list[str] = (
            ["-c", "port=swd"]
            + self._sn_args()
            # ``-u`` / ``--upload`` is the device-memory→file flag in v2.22.
            # The earlier ``-r32 addr size file`` shape was wrong: ``-r32``
            # in v2.22 only accepts ``addr size`` (output goes to stdout).
            + ["-u", address, str(size), str(output_path)]
        )
        base = self._timeout_s("read_timeout_base_s", 60.0)
        per_mb = self._timeout_s("read_timeout_per_mb_s", 10.0)
        timeout_s = base + per_mb * (size / (1024 * 1024))
        start = time.monotonic()
        self._invoke(args, timeout_s=timeout_s)
        duration_s = time.monotonic() - start
        self._log.info(
            "read_flash_to_file addr=%s size=%d output=%s duration=%.2fs",
            address,
            size,
            output_path,
            duration_s,
        )
        return Confirmation(
            operation="read_flash_to_file",
            data={
                "bytes_read": size,
                "address": address,
                "size": size,
                "output_path": str(output_path),
                "duration_s": duration_s,
            },
        )

    def read_memory(self, address: str, *, size: int | None = None) -> MemoryReadResult:
        """F-020 — ``-c port=swd -r8 <addr> <size>``.

        ``size`` is in **bytes** and defaults to 256 — a common peek
        granularity that fits a couple of typical peripheral blocks.
        Substrate validates the address regex; CLI rejects unaligned
        addresses with ``TARGET_CMD_ERR``.

        Uses ``-r8`` (byte read) rather than ``-rd`` — the legacy ``-rd``
        flag was removed in CubeProgrammer 2.22; ``-r8`` matches the
        substrate's byte-granular ``size`` semantics. ``-r16`` / ``-r32``
        also exist but take size in word units, which would change the
        API contract.
        """
        self._validate_address(address)
        if size is None:
            size = 256
        if size <= 0:
            raise ValueError(f"read_memory size must be positive; got {size}")
        args: list[str] = (
            ["-c", "port=swd"]
            + self._sn_args()
            + ["-r8", address, str(size)]
        )
        timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
        stdout = self._invoke(args, timeout_s=timeout_s)
        result = parsers.parse_hex_dump(stdout, address=address, size=size)
        self._log.info(
            "read_memory addr=%s size=%d bytes_read=%d suspicious_unmapped=%s",
            address,
            size,
            result.bytes_read,
            result.suspicious_unmapped,
        )
        if result.suspicious_unmapped:
            self._log.warning(
                "read_memory %s returned all-0xFF (%d bytes) — likely unmapped region",
                address,
                result.bytes_read,
            )
        return result

    # ------------------------------------------------------------------
    # option bytes (F-021)
    # ------------------------------------------------------------------

    def write_option_bytes(
        self,
        pairs: dict[str, int | str | bool],
        *,
        confirm_destructive: Callable[[list[str]], bool] | bool = False,
        confirm_irreversible: bool = False,
    ) -> Confirmation:
        """F-021 — ``-c port=swd -ob NAME=VALUE [...]``.

        Two-stage gate (per spec § "Destructive option-byte gate"):

        1. **Irreversibility check.** ``RDP=0xCC`` sets RDP level 2, a
           permanent transition; requires ``confirm_irreversible=True``.
           Otherwise raises ``ProtocolError`` (substrate-internal —
           caller can retry with the flag set).
        2. **Destructive gate.** Every OB write is treated as destructive
           in v1 (no per-family schema). ``confirm_destructive`` can be a
           ``bool`` (programmatic) or a callable
           ``(list[field_names]) -> bool`` (slash-command path wires
           ``AskUserQuestion``). ``False`` / callable returning False →
           ``UserAbortedError`` (recoverable).
        3. **Invoke** + read back observed state via
           ``read_option_bytes()`` for the ``Confirmation.data``.

        Value coercion (``_render_ob_value``):
        - ``bool`` → ``"0x1"`` / ``"0x0"``
        - ``int`` → ``f"0x{v:x}"``
        - ``str`` → passthrough (caller controls formatting)
        """
        if not pairs:
            raise ValueError("write_option_bytes requires at least one pair")

        # Step 1: irreversibility.
        if "RDP" in pairs and _is_rdp_level_2(pairs["RDP"]) and not confirm_irreversible:
            raise ProtocolError(
                message=(
                    "RDP=0xCC sets RDP level 2, which is irreversible. "
                    "Pass confirm_irreversible=True to proceed."
                ),
                hint="re-call with confirm_irreversible=True after user double-confirms",
                recoverable=True,
            )

        # Step 2: destructive gate.
        if isinstance(confirm_destructive, bool):
            approved = confirm_destructive
        else:
            approved = bool(confirm_destructive(list(pairs.keys())))
        if not approved:
            raise UserAbortedError(
                message=(
                    f"OB write declined. Pairs requested: {list(pairs.keys())}. "
                    "Pass confirm_destructive=True (or a callable returning True) "
                    "to proceed."
                ),
                hint="re-call with confirm_destructive=True once the user agrees",
                recoverable=True,
            )

        # Step 3: invoke.
        args: list[str] = ["-c", "port=swd"] + self._sn_args() + ["-ob"]
        for key, value in pairs.items():
            args.append(f"{key}={_render_ob_value(value)}")
        timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
        self._invoke(args, timeout_s=timeout_s)

        # IMP-05: RDP level 2 permanently disables the debug port — the
        # read-back reconnect is guaranteed to fail, telling the user the
        # *irreversible* write failed when it succeeded. Skip it and say
        # why in the confirmation instead.
        if "RDP" in pairs and _is_rdp_level_2(pairs["RDP"]):
            self._log.info(
                "write_option_bytes wrote RDP level 2; skipping OB "
                "read-back (debug port is now permanently locked)"
            )
            return Confirmation(
                operation="write_option_bytes",
                data={
                    "pairs_written": dict(pairs),
                    "observed_after": None,
                    "read_back_skipped": (
                        "RDP level 2 locks the debug port; a verification "
                        "reconnect cannot succeed"
                    ),
                    "requires_power_cycle": False,  # TODO: per-family detection
                    "destructive_ops_confirmed": list(pairs.keys()),
                },
            )

        observed_after = self.read_option_bytes()
        self._log.info(
            "write_option_bytes wrote %d field(s); RDP level now %s",
            len(pairs),
            observed_after.rdp_level,
        )
        return Confirmation(
            operation="write_option_bytes",
            data={
                "pairs_written": dict(pairs),
                "observed_after": dict(observed_after.observed),
                "requires_power_cycle": False,  # TODO: per-family detection
                "destructive_ops_confirmed": list(pairs.keys()),
            },
        )

    # ------------------------------------------------------------------
    # atomic flash (F-003/004/006/007/011)
    # ------------------------------------------------------------------

    def flash_file(self, path: Path, *, address: str | None = None) -> FlashConfirmation:
        """F-003 — ``-c port=swd -d <path> [<addr>]``.

        Accepts ELF / HEX / BIN; the CLI determines the load address from
        the file format when ``address`` is omitted. Substrate validates
        the address regex when provided.
        """
        return self._flash_invoke(path, address=address, signed=False)

    def flash_bin(self, path: Path, address: str) -> FlashConfirmation:
        """F-004 — ``-c port=swd -d <path> <addr>``.

        Substrate validates the ``.bin`` extension before invoking the
        CLI (per spec) and the address regex. Raises ``ValueError`` on
        either violation.
        """
        path = coerce_path(path)
        if path.suffix.lower() != ".bin":
            raise ValueError(
                f"flash_bin requires a .bin file; got {path.name!r}"
            )
        return self._flash_invoke(path, address=address, signed=False)

    def flash_data(self, path: Path, address: str) -> FlashConfirmation:
        """F-007 — non-firmware payload write.

        Same CLI shape as ``flash_bin``; semantically distinct intent
        (e.g. a data blob like a font / icon / SVD baseline at a known
        flash region). No extension check — payloads come in any format.
        """
        return self._flash_invoke(path, address=address, signed=False)

    def flash_signed(self, path: Path, *, address: str | None = None) -> FlashConfirmation:
        """F-006 — signed-binary write.

        No family pre-check (RES-018): substrate doesn't validate that
        the target is N6 / MP1 / MP2; non-supported families surface as
        vendor CLI errors (typically ``TARGET_CMD_ERR`` /
        ``TARGET_UNKNOWN_MCU_TARGET``) captured by ``parse_error``.
        ``signed=True`` on the result distinguishes from ``flash_file``.
        """
        return self._flash_invoke(path, address=address, signed=True)

    def _flash_invoke(
        self,
        path: Path,
        *,
        address: str | None,
        signed: bool,
    ) -> FlashConfirmation:
        """Shared implementation for ``flash_file`` / ``flash_bin`` /
        ``flash_data`` / ``flash_signed``.

        Builds ``-c port=swd [sn=...] -d <path> [<addr>]``, validates the
        address regex when provided, picks an appropriate timeout, and
        constructs a ``FlashConfirmation`` from the path size + measured
        duration.
        """
        path = coerce_path(path)  # str|Path tolerated (IMP-22)
        if address is not None:
            self._validate_address(address)
        args: list[str] = ["-c", "port=swd"] + self._sn_args() + ["-d", str(path)]
        if address is not None:
            args.append(address)
        timeout_s = self._flash_timeout_s(path)
        start = time.monotonic()
        self._invoke(args, timeout_s=timeout_s)
        duration_s = time.monotonic() - start
        bytes_written = _file_size_or_zero(path)
        self._log.info(
            "flash%s path=%s address=%s bytes=%d duration=%.2fs",
            " (signed)" if signed else "",
            path,
            address or "<cli-default>",
            bytes_written,
            duration_s,
        )
        return FlashConfirmation(
            bytes_written=bytes_written,
            address=address or "",
            duration_s=duration_s,
            signed=signed,
        )

    def flash_to_bank(self, path: Path, bank: int, address: str) -> FlashConfirmation:
        """F-011 — flash a payload to an explicit bank.

        Substrate validates ``bank ∈ {1, 2}`` + the address regex; bad
        bank/address combos surface as ``TARGET_CMD_ERR`` from the CLI.
        The DeviceDB-based addr-in-bank-range check is deferred per
        RES-020 (no DeviceDB in v1).
        """
        if bank not in (1, 2):
            raise ValueError(
                f"flash_to_bank requires bank ∈ {{1, 2}}; got {bank!r}"
            )
        result = self._flash_invoke(path, address=address, signed=False)
        return replace(result, bank=bank)

    # ------------------------------------------------------------------
    # compound flash T2 (F-005/008/009/010, CP-001)
    # ------------------------------------------------------------------

    def flash_bin_no_address(
        self,
        path: Path,
        *,
        on_confirm: Callable[[str], bool] | None = None,
    ) -> FlashConfirmation:
        """F-005 — infer the flash start address, then call ``flash_bin``.

        Uses the universal STM32 main-flash base ``0x08000000`` as the
        inferred address. Devices with non-standard flash bases (typically
        external-flash boards) should call ``flash_bin(addr)`` or
        ``flash_external`` directly.

        ``on_confirm(inferred_address) -> bool`` lets HIL callers (slash
        commands) prompt the user before committing. ``False`` raises
        ``UserAbortedError``. ``None`` skips the gate (programmatic use).
        """
        inferred_address = "0x08000000"
        user_confirmed = False
        if on_confirm is not None:
            if not on_confirm(inferred_address):
                raise UserAbortedError(
                    message=(
                        f"flash_bin_no_address declined: caller refused the "
                        f"inferred address {inferred_address}"
                    ),
                    hint=(
                        "pass an explicit address to flash_bin(), or return "
                        "True from on_confirm to proceed"
                    ),
                    recoverable=True,
                )
            user_confirmed = True
        result = self.flash_bin(path, inferred_address)
        return replace(
            result, address_inferred=True, user_confirmed=user_confirmed
        )

    def flash_pair(
        self,
        bootloader_path: Path,
        application_path: Path,
        *,
        bootloader_address: str | None = None,
        application_address: str | None = None,
    ) -> PairFlashResult:
        """F-008 — two sequential ``flash_file`` calls.

        Partial-completion semantics: first-leg failure re-raises (HIL —
        the caller sees nothing was written). Second-leg failure is
        captured: returns ``PairFlashResult(bootloader=..., application=None,
        both_succeeded=False)``. Caller decides whether to recover.
        """
        boot_result = self.flash_file(bootloader_path, address=bootloader_address)
        try:
            app_result = self.flash_file(
                application_path, address=application_address
            )
        except CubeProgrammerError:
            self._log.warning(
                "flash_pair second leg failed; bootloader is on the device "
                "but application is not"
            )
            return PairFlashResult(
                bootloader=boot_result, application=None, both_succeeded=False
            )
        return PairFlashResult(
            bootloader=boot_result, application=app_result, both_succeeded=True
        )

    def flash_signed_pair(
        self,
        bootloader_path: Path,
        application_path: Path,
        *,
        bootloader_address: str | None = None,
        application_address: str | None = None,
        sign_unsigned: bool = False,
        signing_header_version: str | None = None,
        bootloader_image_type: str = "fsbl",
        application_image_type: str = "ssbl",
        bootloader_entry_point: str | None = None,
        application_entry_point: str | None = None,
        signing_no_key: bool = False,
    ) -> PairFlashResult:
        """F-009 — signed variant of ``flash_pair``.

        No family pre-check (RES-018). ``sign_unsigned=True`` checks each
        input for the ST image-header magic and routes unsigned ones
        through ``SigningTool.sign_binary`` (F-013) first, flashing the
        signed output (RES-039 — the former "until C2 lands" gate was
        stale; C2 shipped 2026-05-14). Signing parameters come from the
        caller per the F-009 contract: ``signing_header_version`` is
        required when an unsigned input is present; each leg's flash
        address doubles as its signing ``load_address``; image types
        default to the fsbl/ssbl pair convention; entry points are
        forwarded (``sign_binary`` enforces RES-020's fsbl/ssbl
        entry-point requirement); ``signing_no_key=True`` forwards
        ``no_key`` to ``sign_binary`` (``-nk`` dev-mode signing — without
        it, keyed hv≥2 signing requires provisioned key material and the
        SigningTool CLI rejects the run). Same partial-completion
        semantics as ``flash_pair``.
        """
        if sign_unsigned:
            bootloader_path = self._sign_if_unsigned(
                bootloader_path,
                leg="bootloader",
                address=bootloader_address,
                image_type=bootloader_image_type,
                entry_point=bootloader_entry_point,
                header_version=signing_header_version,
                no_key=signing_no_key,
            )
            application_path = self._sign_if_unsigned(
                application_path,
                leg="application",
                address=application_address,
                image_type=application_image_type,
                entry_point=application_entry_point,
                header_version=signing_header_version,
                no_key=signing_no_key,
            )
        boot_result = self.flash_signed(bootloader_path, address=bootloader_address)
        try:
            app_result = self.flash_signed(
                application_path, address=application_address
            )
        except CubeProgrammerError:
            self._log.warning(
                "flash_signed_pair second leg failed; bootloader is on the "
                "device but application is not"
            )
            return PairFlashResult(
                bootloader=boot_result, application=None, both_succeeded=False
            )
        return PairFlashResult(
            bootloader=boot_result, application=app_result, both_succeeded=True
        )

    def _sign_if_unsigned(
        self,
        path: Path,
        *,
        leg: str,
        address: str | None,
        image_type: str,
        entry_point: str | None,
        header_version: str | None,
        no_key: bool = False,
    ) -> Path:
        """Return ``path`` if it already carries the ST image header;
        otherwise sign it (F-013) and return the signed output path."""
        path = coerce_path(path)
        if _has_signed_header(path):
            self._log.info(
                "flash_signed_pair: %s %s already carries the ST image "
                "header; not re-signing",
                leg,
                path.name,
            )
            return path
        if header_version is None:
            raise ValueError(
                f"{leg} input {path.name!r} is unsigned; sign_unsigned=True "
                "requires signing_header_version= (see SigningTool."
                "sign_binary / UM2543 §2.1)"
            )
        if address is None:
            raise ValueError(
                f"{leg} input {path.name!r} is unsigned; its flash address "
                f"kwarg is required (it doubles as the signing load_address)"
            )
        from embedagents.stm32.signing import SigningTool

        firmware = getattr(self.ctx.project, "firmware", None)
        device_family = getattr(firmware, "device_family", None)
        result = SigningTool(self.ctx).sign_binary(
            path,
            load_address=address,
            image_type=image_type,  # type: ignore[arg-type]
            header_version=header_version,  # type: ignore[arg-type]
            entry_point=entry_point,
            no_key=no_key,
            device_family=str(device_family) if device_family else None,
        )
        self._log.info(
            "flash_signed_pair: signed %s %s -> %s",
            leg,
            path.name,
            result.output_path.name,
        )
        return result.output_path

    def flash_external(
        self,
        path: Path,
        address: str,
        *,
        loader_path: Path | None = None,
        on_loader_choice: Callable[[list[Path]], Path] | None = None,
    ) -> FlashConfirmation:
        """F-010 — external loader (``-el``) flash.

        Build the loader path via ``external_loader.discover_external_loader``:

        - ``loader_path`` explicit override → use as-is (after existence
          check); skip the family filter.
        - Otherwise resolve via ``ctx.tools.cube_programmer_cli`` /
          ``bin/ExternalLoader``, filtered by the family prefix extracted
          from the banner's ``device_name``.

        Multi-match resolution: caller passes ``on_loader_choice`` to
        pick one path. Without that callback, multi-match raises
        ``ConfigurationError`` with the candidate list in the hint.
        Zero-match raises ``ConfigurationError`` with the discovery
        path. The picked loader's basename is recorded in
        ``FlashConfirmation.loader_used``.
        """
        path = coerce_path(path)
        self._validate_address(address)
        cli = self._require_cli()
        loader = self._resolve_external_loader(
            cli=cli,
            loader_path=loader_path,
            on_loader_choice=on_loader_choice,
        )
        args: list[str] = (
            ["-c", "port=swd"]
            + self._sn_args()
            + ["-el", str(loader), "-d", str(path), address]
        )
        base = self._timeout_s("flash_external_timeout_base_s", 300.0)
        per_mb = self._timeout_s("flash_external_timeout_per_mb_s", 30.0)
        size_bytes = _file_size_or_zero(path)
        timeout_s = base + per_mb * (size_bytes / (1024 * 1024))
        start = time.monotonic()
        self._invoke(args, timeout_s=timeout_s)
        duration_s = time.monotonic() - start
        self._log.info(
            "flash_external path=%s address=%s loader=%s bytes=%d duration=%.2fs",
            path,
            address,
            loader.name,
            size_bytes,
            duration_s,
        )
        return FlashConfirmation(
            bytes_written=size_bytes,
            address=address,
            duration_s=duration_s,
            loader_used=loader.name,
        )

    def _resolve_external_loader(
        self,
        *,
        cli: Path,
        loader_path: Path | None,
        on_loader_choice: Callable[[list[Path]], Path] | None,
    ) -> Path:
        """Pick a single loader Path or raise loudly.

        Explicit override returns the path after an existence check.
        Otherwise the banner provides ``device_name`` for the family
        filter; multi-match routes through ``on_loader_choice``.
        """
        if loader_path is not None:
            matches = external_loader.discover_external_loader(
                programmer_path=cli,
                device_family="",  # unused with explicit
                explicit=loader_path,
            )
            if not matches:
                raise ConfigurationError(
                    message=f"external loader {loader_path} does not exist",
                    hint="check the path or omit loader_path= to auto-discover",
                )
            return matches[0]

        banner = self.connect()
        family = external_loader.extract_family_prefix(banner.device_name)
        matches = external_loader.discover_external_loader(
            programmer_path=cli, device_family=family
        )
        loader_dir = cli.parent / "ExternalLoader"
        if not matches:
            raise ConfigurationError(
                message=(
                    f"no external loader found for device family {family!r}"
                ),
                hint=(
                    f"check {loader_dir} for matching .stldr files, or pass "
                    "loader_path= explicitly"
                ),
            )
        if len(matches) > 1:
            if on_loader_choice is None:
                names = ", ".join(p.name for p in matches)
                raise ConfigurationError(
                    message=(
                        f"multiple external loaders match family {family!r}: "
                        f"{names}"
                    ),
                    hint=(
                        "pass on_loader_choice=callable to pick one, or set "
                        "loader_path= explicitly"
                    ),
                )
            picked = on_loader_choice(matches)
            if picked not in matches:
                raise ValueError(
                    f"on_loader_choice returned {picked!r}, which is not "
                    f"one of the discovered candidates {[p.name for p in matches]}"
                )
            return picked
        return matches[0]

    def download_image(
        self,
        path: Path,
        *,
        address: str | None = None,
        on_confirm: Callable[[str], bool] | None = None,
    ) -> FlashConfirmation:
        """CP-001 — extension-based router.

        - ``.elf`` / ``.hex`` / ``.axf`` / ``.s19`` / ``.srec`` →
          ``flash_file`` (address embedded in the file format; explicit
          ``address`` optional). Per F-003 / expected-behaviors — the
          router used to reject ``.axf``/``.s19``/``.srec`` with a hint
          misdirecting to ``flash_data`` (A-006).
        - ``.bin`` with explicit ``address`` → ``flash_bin``.
        - ``.bin`` without ``address`` → ``flash_bin_no_address`` (calls
          ``on_confirm`` with the inferred address).
        - Other extensions → ``ValueError`` (use ``flash_data`` for
          arbitrary payloads at an explicit address).

        ``route_used`` on the result records which path fired so callers
        + logs can trace router behaviour.
        """
        path = coerce_path(path)
        ext = path.suffix.lower()
        if ext in (".elf", ".hex", ".axf", ".s19", ".srec"):
            result = self.flash_file(path, address=address)
            return replace(result, route_used="flash_file")
        if ext == ".bin":
            if address is None:
                result = self.flash_bin_no_address(path, on_confirm=on_confirm)
                return replace(result, route_used="flash_bin_no_address")
            result = self.flash_bin(path, address)
            return replace(result, route_used="flash_bin")
        raise ValueError(
            f"download_image cannot infer route from extension {ext!r}; "
            "supported firmware extensions are .elf .hex .axf .s19 .srec "
            ".bin (UM2237 §3.2.3) — use flash_data(path, address) for "
            "non-firmware payloads"
        )

    # ------------------------------------------------------------------
    # diagnostic (DIAG-001 binary-only path, DIAG-018)
    # ------------------------------------------------------------------

    def analyze_hardfault(self) -> HardFaultDecode:
        """DIAG-001 binary-only path — ``-c port=swd mode=HOTPLUG -hf`` per UM2237 §3.2.29.

        Decode produces ``HardFaultDecode`` with ``source_used="cubeprogrammer-hf"``.
        For the gdb-mediated path (debug session active, source available),
        callers use the ``debug`` module's own decoder instead (per M-012
        dual-tool routing).

        ``hardfault_detected=False`` is the canonical no-fault result —
        substrate does NOT raise when the analyzer reports clean state.

        **Why ``mode=HOTPLUG``:** the CLI's default connect mode is Normal,
        which applies a Software reset on attach — clearing the chip's
        CFSR / HFSR / MMFAR / BFAR / SHCSR fault registers BEFORE ``-hf``
        reads them. HOTPLUG attaches without resetting, preserving the
        sticky fault state. (Bench-verified 2026-05-19 against a UDF #0
        faulting firmware on NUCLEO-L476RG.)
        """
        args: list[str] = (
            ["-c", "port=swd", "mode=HOTPLUG"] + self._sn_args() + ["-hf"]
        )
        timeout_s = self._timeout_s("atomic_timeout_s", 30.0)
        stdout = self._invoke(args, timeout_s=timeout_s)
        result = parsers.parse_hardfault(stdout)
        if result.hardfault_detected:
            self._log.warning(
                "analyze_hardfault detected %s at PC=%s",
                result.fault_type or "Fault",
                result.faulty_pc,
            )
        else:
            self._log.info("analyze_hardfault: no fault detected")
        return result

    def verify_option_bytes(
        self, expected: dict[str, int | str | bool]
    ) -> OptionBytesDiff:
        """DIAG-018 — read OB, diff against ``expected``.

        Returns ``OptionBytesDiff`` with the full ``observed`` dict, the
        ``expected`` dict, and a per-field ``diffs`` list. Comparison
        normalises both sides to ``int`` when possible (so
        ``"0xAA"`` matches ``0xAA`` matches ``170``); booleans coerce
        to ``1`` / ``0``. Fields missing from the observed map are
        reported with ``observed_value=None``.
        """
        if not expected:
            raise ValueError(
                "verify_option_bytes requires at least one expected key"
            )
        observed = self.read_option_bytes()
        diffs: list[OptionByteDiffEntry] = []
        for field, exp_val in expected.items():
            obs_val = observed.observed.get(field)
            if not _ob_values_equal(obs_val, exp_val):
                diffs.append(
                    OptionByteDiffEntry(
                        field=field,
                        observed_value=obs_val if obs_val is not None else "<missing>",
                        expected_value=exp_val,
                    )
                )
        self._log.info(
            "verify_option_bytes checked %d field(s); %d mismatch(es)",
            len(expected),
            len(diffs),
        )
        return OptionBytesDiff(
            observed=dict(observed.observed),
            expected=dict(expected),
            diffs=diffs,
        )

    # ------------------------------------------------------------------
    # SWO / ITM stream (VCP-007)
    # ------------------------------------------------------------------

    def tail_swo(
        self,
        *,
        freq_mhz: float,
        port_number: int = 0,
        log_path: Path | None = None,
    ) -> Iterator[ITMRecord]:
        """VCP-007 — ``-c port=swd -startswv freq=<MHz> portnumber=<n> [<logfile>]``.

        Returns a **lazy generator** yielding ``ITMRecord`` for each parsed
        line from the SWO stream. Bypasses ``run_tool`` because the
        operation is unbounded by design (per spec timeout policy:
        "no timeout / stream"); ``run_tool``'s one-shot contract would
        block the caller.

        Lifecycle / cleanup:

        - Subprocess is spawned with ``start_new_session=True`` so SIGTERM
          on shutdown reaches the whole process group cleanly.
        - The generator's ``finally`` clause terminates the subprocess on
          consumer ``break`` / generator ``.close()`` / garbage-collection.
          Grace period is short (HIL — no long waits).

        Behaviour:

        - Lines that don't look like ITM payloads (banner, status,
          warnings, blank lines) are skipped silently. The ITM payload
          patterns are documented on ``parsers.parse_itm_line``.
        - SWV-overflow / drop warnings emit ``ctx.logger`` WARNING but
          do NOT raise — drops are bench reality.
        - ``timestamp_s`` on each record is ``time.monotonic()`` minus the
          subprocess start time, so consumers get a monotonic series.
        - ``log_path`` is passed straight through to the CLI; CubeProgrammer
          writes its own raw capture file (substrate doesn't tail it
          alongside the line stream — opening a second reader risks
          missing lines).
        """
        cli = self._require_cli()
        # `-startswv` is CubeProgrammer's NON-interactive auto-start form:
        # "Printf via SWO & Start the reception of swv automatically". The
        # plain `-swv` is interactive — it prints a menu and blocks waiting
        # for an `R` keypress to begin, so under DEVNULL stdin it captures
        # nothing. `-startswv` begins reception immediately, no stdin needed.
        # (Discovered from `STM32_Programmer_CLI --help`, bench 2026-05-24.)
        args: list[str] = (
            ["-c", "port=swd"]
            + self._sn_args()
            + ["-startswv", f"freq={freq_mhz}", f"portnumber={port_number}"]
        )
        if log_path is not None:
            args.append(str(log_path))

        self._log.info(
            "tail_swo starting freq_mhz=%s port_number=%d log=%s",
            freq_mhz,
            port_number,
            log_path,
        )
        proc = subprocess.Popen(
            [str(cli), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # Merge stderr into the line stream (IMP-25: a separate
            # never-read PIPE blocks the child once it fills) so CLI
            # error lines reach the failure check below (IMP-04).
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
            bufsize=1,  # line-buffered
        )
        start = time.monotonic()
        error_lines: list[str] = []
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if parsers.is_swv_dropped_bytes_warning(raw_line):
                    self._log.warning(
                        "tail_swo SWV overflow: %s", raw_line.strip()
                    )
                    continue
                if raw_line.strip().startswith("Error:"):
                    error_lines.append(raw_line.strip())
                    continue
                record = parsers.parse_itm_line(
                    raw_line, timestamp_s=time.monotonic() - start
                )
                if record is not None:
                    yield record
            # IMP-04: natural EOF means the CLI exited on its own (a
            # consumer break never reaches here — GeneratorExit unwinds
            # from the yield). A nonzero exit must raise, not silently
            # present an empty/truncated stream as a healthy capture.
            try:
                exit_code: int | None = proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                exit_code = None
            if exit_code is not None and exit_code != 0:
                raise parsers.parse_error("\n".join(error_lines), exit_code)
        finally:
            self._terminate_swo(proc)
            self._log.info(
                "tail_swo stopped after %.2fs", time.monotonic() - start
            )

    def _terminate_swo(self, proc: subprocess.Popen) -> None:
        """Terminate the SWO subprocess on generator close / GC.

        Mirrors ``subprocess_runner._terminate``: ``Popen.terminate`` →
        short grace → ``Popen.kill``. No bare ``os.kill`` — the Popen
        API satisfies ADR-005's "no signal in business logic" rule."""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
                return
            except subprocess.TimeoutExpired:
                pass
            proc.kill()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._log.warning(
                    "tail_swo subprocess pid=%s did not die after SIGKILL",
                    proc.pid,
                )
        except ProcessLookupError:
            return
