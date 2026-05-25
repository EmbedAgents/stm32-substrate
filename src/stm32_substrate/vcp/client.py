"""``VCP`` public class.

Per the VCP API spec § "Public methods". Four methods — ``tail`` /
``send_and_read`` / ``reconnect`` / ``close`` — over a lazy-attached
``_VcpReader``. Implements:

- SB-001 — lazy attach on first call (``_ensure_reader``).
- SB-002 — lazy reconnect on stale port (``_ensure_reader`` validates
  ``is_alive`` before returning the cached reader).
- HIL collision (M-019) — second open against a different port raises
  ``VCPReaderAlreadyActive``.
- Multi-probe resolution (MR-2 closure, RES-020) — ``firmware.board`` +
  ``cubeprogrammer.list_probes()`` cross-call.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Iterator

from stm32_substrate.errors import (
    VCPAmbiguousProbe,
    VCPError,
    VCPNotEnumerated,
    VCPReaderAlreadyActive,
)
from stm32_substrate.vcp.discovery import discover_vcp_ports
from stm32_substrate.vcp.reader import _VcpReader
from stm32_substrate.vcp.results import (
    PriorVCPState,
    ReconnectResult,
    RequestResponse,
    VCPPortCandidate,
    VCPProbeCandidate,
)

if TYPE_CHECKING:  # pragma: no cover
    from stm32_substrate.context import SubstrateContext


_DEFAULT_BAUD = 115200
_DEFAULT_TERMINATOR = "\n"


class VCP:
    """Substrate's internal serial reader; one instance per ``SubstrateContext``.

    Implements SB-001 (auto-attach on first call) and SB-002 (auto-reconnect
    on next call after a reset). One internal drain thread per active
    ``_VcpReader`` — pyserial reads are blocking; the thread fills the
    bounded line buffer without blocking public-API callers.

    No hotplug daemon: every public method lazy-validates the port handle
    and re-enumerates if stale.
    """

    def __init__(
        self,
        ctx: "SubstrateContext",
        *,
        _list_probes_fn=None,
        _reader_factory=None,
    ) -> None:
        self.ctx = ctx
        self._log = ctx.logger.getChild("vcp")
        # Test injection points — production callers leave these None.
        self._list_probes_fn = _list_probes_fn  # callable(ctx) -> list[ProbeRecord]
        self._reader_factory = _reader_factory  # callable(**kwargs) -> _VcpReader

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def tail(
        self,
        *,
        port: str | None = None,
        baud: int | None = None,
        last_n: int | None = None,
        follow: bool = False,
        timeout_s: float | None = None,
    ) -> Iterator[str]:
        """VCP-001 — yield buffered + live VCP lines as ``str``."""
        reader = self._ensure_reader(port=port, baud=baud)
        effective_last_n = (
            last_n
            if last_n is not None
            else _vcp_default(self.ctx, "tail_default_last_n", 100)
        )
        effective_timeout = (
            timeout_s
            if timeout_s is not None
            else _vcp_default(self.ctx, "tail_default_timeout_s", 5.0)
        )
        yield from reader.read_lines(
            last_n=effective_last_n,
            follow=follow,
            timeout_s=effective_timeout,
        )

    def send_and_read(
        self,
        line: str,
        *,
        port: str | None = None,
        baud: int | None = None,
        terminator: str | None = None,
        timeout_s: float | None = None,
        inter_line_idle_ms: int | None = None,
        echo_filter: bool = False,
    ) -> RequestResponse:
        """VCP-002 — write a line; collect reply via idle-bounded read."""
        reader = self._ensure_reader(port=port, baud=baud, terminator=terminator)

        sep = (
            terminator
            if terminator is not None
            else self._descriptor_field("uart_terminator", _DEFAULT_TERMINATOR)
        )
        wall_s = (
            timeout_s
            if timeout_s is not None
            else _vcp_default(self.ctx, "send_default_timeout_s", 2.0)
        )
        idle_ms = (
            inter_line_idle_ms
            if inter_line_idle_ms is not None
            else _vcp_default(self.ctx, "inter_line_idle_ms", 100)
        )

        # Flush any pre-existing buffered lines so the reply is clean.
        with reader._lock:  # noqa: SLF001 — intentional: reader is substrate-private
            reader._buffer.clear()

        start = time.monotonic()
        reader.write_line(line, terminator=sep)
        replies, timeout_hit = reader.read_lines_with_idle(
            timeout_s=wall_s,
            inter_line_idle_ms=idle_ms,
        )
        elapsed = time.monotonic() - start

        filtered = False
        if echo_filter and replies:
            sent = line.rstrip(sep) if sep else line
            new_replies: list[str] = []
            dropped = False
            for r in replies:
                if not dropped and r == sent:
                    dropped = True
                    continue
                new_replies.append(r)
            if dropped:
                filtered = True
            replies = tuple(new_replies)

        self._log.info(
            "send_and_read sent=%r reply_lines=%d timeout_hit=%s elapsed=%.3fs",
            line,
            len(replies),
            timeout_hit,
            elapsed,
        )
        return RequestResponse(
            sent_line=line,
            reply_lines=tuple(replies),
            timeout_hit=timeout_hit,
            duration_s=elapsed,
            port=reader.port,
            baud=reader.baud,
            echo_filtered=filtered,
        )

    def reconnect(
        self,
        *,
        port: str | None = None,
        max_wait_s: float | None = None,
    ) -> ReconnectResult:
        """VCP-003 — explicit force-reconnect on top of SB-002."""
        reader = self.ctx.session_state.active_vcp_reader
        if reader is None:
            # No prior reader to recycle — attach lazily, treat as a same-port
            # "reconnect" from a virtual prior state.
            attached = self._ensure_reader(port=port)
            prior = PriorVCPState(
                port=None, baud=None, last_byte_timestamp_s=None, open=False
            )
            return ReconnectResult(
                port=attached.port,
                status="reconnected",
                prior_state=prior,
                duration_s=0.0,
            )
        return reader.reconnect(max_wait_s=max_wait_s)

    def close(self) -> None:
        """Release the active reader (if any).

        Idempotent — calling ``close()`` when no reader is active is a
        DEBUG-logged no-op. Useful for handing /dev/ttyACMx to minicom /
        screen / picocom / Cutecom without restarting the Python session.
        """
        reader = self.ctx.session_state.active_vcp_reader
        if reader is None:
            self._log.debug("close() no-op; no active reader")
            return
        reader.close()
        self.ctx.session_state.active_vcp_reader = None

    # ------------------------------------------------------------------
    # Discovery (exposed for tests + CLI introspection)
    # ------------------------------------------------------------------

    def discover_vcp_ports(self) -> list[VCPPortCandidate]:
        """Pure enumeration helper.

        Wrapper around the module-level ``discover_vcp_ports`` that pulls
        ``probe_sn`` from ``ctx.default_probe_sn``. Returns a possibly-empty
        list and never raises for ambiguity — ambiguity resolution lives in
        ``_ensure_reader()`` per RES-020 #b.
        """
        return discover_vcp_ports(probe_sn=self.ctx.default_probe_sn)

    # ------------------------------------------------------------------
    # Internal: _ensure_reader (the heart of SB-001 + SB-002)
    # ------------------------------------------------------------------

    def _ensure_reader(
        self,
        *,
        port: str | None = None,
        baud: int | None = None,
        terminator: str | None = None,
    ) -> _VcpReader:
        """Return an attached reader, opening one lazily if needed.

        - SB-001: first call attaches.
        - SB-002: cached reader is health-checked; stale → reconnect.
        - HIL collision: explicit ``port=`` mismatch with cached reader →
          ``VCPReaderAlreadyActive``.
        """
        cached = self.ctx.session_state.active_vcp_reader
        if cached is not None:
            if port is not None and port != cached.port:
                raise VCPReaderAlreadyActive(
                    message=(
                        f"a VCP reader is already attached to {cached.port!r}; "
                        f"refusing to open a second one for {port!r}"
                    ),
                    vcp_marker="reader-already-active",
                    port=cached.port,
                    hint="call VCP.close() on the existing reader first",
                )
            if cached.is_alive():
                return cached
            # Stale handle — SB-002 lazy reconnect.
            self._log.info("port stale; reconnecting (prior=%s)", cached.port)
            cached.reconnect()
            return cached

        resolved_port = port or self._resolve_port()
        resolved_baud = (
            baud
            if baud is not None
            else self._descriptor_field("uart_baud", _DEFAULT_BAUD)
        )
        resolved_terminator = (
            terminator
            if terminator is not None
            else self._descriptor_field("uart_terminator", _DEFAULT_TERMINATOR)
        )

        factory = self._reader_factory
        if factory is None:
            factory = _VcpReader
        reader = factory(
            ctx=self.ctx,
            port=resolved_port,
            baud=resolved_baud,
            terminator=resolved_terminator,
            probe_sn=self.ctx.default_probe_sn,
        )
        reader.open()
        self.ctx.session_state.active_vcp_reader = reader
        return reader

    # ------------------------------------------------------------------
    # Internal: probe resolution
    # ------------------------------------------------------------------

    def _resolve_port(self) -> str:
        """Resolve which ``/dev/ttyACMx`` to open.

        - 1 candidate → use it (ergonomic single-probe path).
        - Multiple + ``ctx.default_probe_sn`` → match by SN.
        - Multiple + no SN + ``firmware.board`` → cross-call cubeprogrammer
          to map SN → board, auto-pick on unique match.
        - Otherwise → ``VCPAmbiguousProbe`` with combined candidate records.
        """
        candidates = discover_vcp_ports(probe_sn=self.ctx.default_probe_sn)
        if not candidates:
            raise VCPNotEnumerated(
                message=self._enumerate_loud_error(),
                vcp_marker="no-vcp-enumerated",
                requested_probe_sn=self.ctx.default_probe_sn,
                hint=(
                    "check cubeprogrammer.list_probes() for the actual SNs, "
                    "or run `lsusb -v` to see whether the probe enumerates at all"
                ),
            )
        if len(candidates) == 1:
            return candidates[0].port

        # Multiple ST-LINK VCPs + no SN — try descriptor-driven auto-match.
        board = self._descriptor_field("board", None)
        if not board:
            raise self._ambiguous_probe(candidates)

        sn_to_board = self._fetch_probe_board_map()
        matches = [
            c for c in candidates if (sn_to_board.get(c.serial_number) or "").lower() == board.lower()
        ]
        if len(matches) == 1:
            picked = matches[0]
            self._log.info(
                "multi-probe auto-match: board=%r → probe SN=%s port=%s",
                board,
                picked.serial_number,
                picked.port,
            )
            # Latch the SN for the rest of the session.
            object.__setattr__(self.ctx, "default_probe_sn", picked.serial_number)
            return picked.port
        raise self._ambiguous_probe(candidates, sn_to_board=sn_to_board)

    def _ambiguous_probe(
        self,
        candidates: list[VCPPortCandidate],
        *,
        sn_to_board: dict[str, str | None] | None = None,
    ) -> VCPAmbiguousProbe:
        sn_to_board = sn_to_board or self._fetch_probe_board_map()
        records = tuple(
            VCPProbeCandidate(
                port=c.port,
                serial_number=c.serial_number,
                board_name=sn_to_board.get(c.serial_number),
            )
            for c in candidates
        )
        return VCPAmbiguousProbe(
            message=(
                f"multiple ST-LINK VCPs enumerated and ctx.default_probe_sn is unset; "
                f"candidates: {[r.port for r in records]}"
            ),
            vcp_marker="ambiguous-probe",
            candidates=records,
            hint=(
                "set ctx.default_probe_sn (or the env var STM32_PROGRAMMER_DEFAULT_SN), "
                "or add firmware.board to the project descriptor to auto-match"
            ),
        )

    def _fetch_probe_board_map(self) -> dict[str, str | None]:
        """Cross-call into cubeprogrammer for SN → board_name resolution.

        Returns ``{sn: board_name|None}``. Errors degrade silently to an
        empty map (cubeprogrammer failures should not block VCP discovery
        when the descriptor / SN already disambiguates).
        """
        fn = self._list_probes_fn
        if fn is None:
            # Lazy import: avoids circular import at module load.
            from stm32_substrate.cubeprogrammer import CubeProgrammer

            def fn(ctx):
                return CubeProgrammer(ctx).list_probes()

        try:
            probes = fn(self.ctx)
        except Exception as ex:  # pragma: no cover - defensive
            self._log.debug("list_probes() failed during VCP resolution: %s", ex)
            return {}

        result: dict[str, str | None] = {}
        for p in probes:
            sn = getattr(p, "stlink_sn", None) or getattr(p, "serial_number", None)
            board = getattr(p, "board_name", None)
            if sn:
                result[sn] = board
        return result

    def _descriptor_field(self, name: str, default):
        project = self.ctx.project
        if project is None:
            return default
        firmware = getattr(project, "firmware", None)
        if firmware is None:
            return default
        value = getattr(firmware, name, None)
        return default if value is None else value

    def _enumerate_loud_error(self) -> str:
        """Loud-error format per v1 spec for the empty-candidate case."""
        sn = self.ctx.default_probe_sn
        if sn:
            return f"no ST-LINK VCP found for probe SN {sn!r}"
        return "no ST-LINK VCP enumerated"


def _vcp_default(ctx, name: str, default):
    """Pull a numeric knob from ``ctx.defaults.vcp.<name>`` with fallback."""
    vcp = getattr(ctx.defaults, "vcp", None)
    if vcp is None:
        return default
    value = getattr(vcp, name, None)
    return default if value is None else value
