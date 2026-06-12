"""Internal serial reader.

Per ``v1/vcp-api.md`` § "reader.py — internal serial reader". Wraps a
``pyserial.Serial`` object behind:

- A daemon drain thread (pyserial reads are blocking — one thread per
  active reader, NOT one per public-API call).
- A bounded ``deque`` of decoded lines (UTF-8 with ``errors='replace'``).
- ``is_alive`` / ``reconnect`` for SB-002 lazy stale-port handling.

Encoding is hardcoded UTF-8 with ``errors='replace'`` per v1 spec; latin-1
fallback is TODO(v1+) when VCP-004/005 surface a real consumer.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from stm32_substrate.errors import VCPError, VCPPortInUse
from stm32_substrate.vcp.discovery import discover_vcp_ports
from stm32_substrate.vcp.results import PriorVCPState, ReconnectResult

if TYPE_CHECKING:  # pragma: no cover
    import logging as _logging  # noqa: F401

    from stm32_substrate.context import SubstrateContext


@dataclass
class _VcpReaderDefaults:
    """Numeric knobs extracted from ``ctx.defaults.vcp`` once per reader."""

    line_buffer_capacity: int = 1000
    drain_read_timeout_ms: int = 50
    reconnect_max_s: float = 10.0


class _VcpReader:
    """Substrate-internal serial reader; not part of public API.

    Thread model:
      - One daemon thread (``_drain``) reads bytes from pyserial in
        ``drain_read_timeout_ms``-bounded chunks, splits on the terminator,
        UTF-8-decodes with replace-errors, and appends lines to a bounded
        ``deque``.
      - Public methods (``read_lines``, ``write_line``, ...) are called
        from the main thread; they hold ``self._lock`` while reading from
        the deque so the drain thread sees a consistent view.

    Bounded queue: when the consumer drains slowly and the deque is full,
    appending the next line silently drops the oldest. A WARNING is
    emitted once per overflow window (the warn-once tracking is handled by
    the substrate-wide logger semantics; per-overflow-event is acceptable
    for v1).

    Lifecycle:
      - ``open()`` opens pyserial, starts drain thread.
      - ``close()`` signals drain thread, joins it, closes serial.
      - ``is_alive()`` cheap port-validity check.
      - ``reconnect()`` close + re-enumerate + open at the (possibly new) port.
    """

    def __init__(
        self,
        *,
        ctx: "SubstrateContext",
        port: str,
        baud: int,
        terminator: str = "\n",
        probe_sn: str | None = None,
        _serial_factory=None,
    ) -> None:
        self.ctx = ctx
        self.port = port
        self.baud = baud
        self.terminator = terminator
        self.probe_sn = probe_sn

        self._log = ctx.logger.getChild("vcp")
        self._defaults = _read_defaults(ctx)
        self._serial_factory = _serial_factory  # tests inject; None → real pyserial

        self._serial = None
        self._buffer = deque(maxlen=self._defaults.line_buffer_capacity)
        self._pending = bytearray()         # cross-chunk byte accumulator
        self._lock = threading.Lock()
        self._line_event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_byte_ts: float | None = None
        self._encoding_warned = False
        self._drops_since_warn = 0
        self._open = False
        # IMP-17: set when the drain thread dies on a read error so
        # is_alive() stops trusting a zombie reader whose port path
        # still exists.
        self._drain_failed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._open:
            return
        self._serial = self._open_serial()
        self._drain_failed = False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._drain,
            name=f"vcp-drain[{self.port}]",
            daemon=True,
        )
        self._thread.start()
        self._open = True
        self._log.info(
            "attached port=%s baud=%d probe_sn=%s",
            self.port,
            self.baud,
            self.probe_sn or "<unset>",
        )

    def close(self) -> None:
        if not self._open:
            self._log.debug("close() no-op; reader already closed")
            return
        self._stop.set()
        self._line_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with suppress(Exception):
            if self._serial is not None:
                self._serial.close()
        self._open = False
        self._thread = None
        self._serial = None
        self._log.info("released port=%s", self.port)

    def is_alive(self) -> bool:
        """Cheap port-validity check.

        - A drain thread that died on a read error marks the reader dead
          (IMP-17) — the dominant signal on both OSes.
        - POSIX device-node ports (``/dev/ttyACMx``) additionally check
          the path, covering the USB-yank case. Windows ``COMx`` names
          are not filesystem paths (``os.path.exists('COM10')`` is False
          for a healthy port — IMP-18), so the path probe only applies
          to path-shaped port names.
        """
        if not self._open or self._serial is None:
            return False
        if self._drain_failed:
            return False
        # The port path going away (USB unplug) is the dominant stale
        # case on Linux; only meaningful when the port IS a path. Bare
        # Windows COM names carry no separator; path-shaped ports on
        # either OS (/dev/ttyACMx, C:\...\pipe) do.
        looks_like_path = "/" in self.port or "\\" in self.port
        if looks_like_path and not os.path.exists(self.port):
            return False
        # ``Serial.is_open`` flips False after pyserial sees its own IOError.
        return bool(getattr(self._serial, "is_open", True))

    def reconnect(self, *, max_wait_s: float | None = None) -> ReconnectResult:
        """Close + re-enumerate + re-open at the matching probe's current port.

        ``status="same_port"`` when the device returns at the same path;
        ``"reconnected"`` when it shows up at a different ``/dev/ttyACMx``.
        Raises ``VCPError(vcp_marker="reconnect-timeout")`` when the probe
        does not re-enumerate within ``max_wait_s`` (single failure
        contract per RES-020).
        """
        prior = PriorVCPState(
            port=self.port,
            baud=self.baud,
            last_byte_timestamp_s=self._last_byte_ts,
            open=self._open,
        )
        prior_port = self.port

        if self._open:
            self.close()

        cap = max_wait_s if max_wait_s is not None else self._defaults.reconnect_max_s
        deadline = time.monotonic() + cap
        new_port: str | None = None
        while time.monotonic() < deadline:
            matches = discover_vcp_ports(probe_sn=self.probe_sn)
            if matches:
                new_port = matches[0].port
                break
            time.sleep(0.1)
        if new_port is None:
            raise VCPError(
                message=f"probe SN {self.probe_sn!r} did not re-enumerate within {cap}s",
                vcp_marker="reconnect-timeout",
                port=prior_port,
                requested_probe_sn=self.probe_sn,
                hint="raise reconnect_max_s, or check the USB cable / cubeprogrammer reset",
                recoverable=False,
            )

        self.port = new_port
        self.open()
        status = "same_port" if new_port == prior_port else "reconnected"
        elapsed = cap - (deadline - time.monotonic())
        self._log.info(
            "reconnect status=%s prior_port=%s new_port=%s elapsed=%.2fs",
            status,
            prior_port,
            new_port,
            elapsed,
        )
        return ReconnectResult(
            port=new_port,
            status=status,
            prior_state=prior,
            duration_s=elapsed,
        )

    # ------------------------------------------------------------------
    # Public read / write
    # ------------------------------------------------------------------

    def write_line(self, line: str, *, terminator: str | None = None) -> None:
        """Write ``line`` + terminator; UTF-8 encoded."""
        if self._serial is None:
            raise VCPError(
                message="write attempted on a closed VCP reader",
                vcp_marker="reader-closed",
                port=self.port,
            )
        sep = terminator if terminator is not None else self.terminator
        payload = (line + sep).encode("utf-8", errors="replace")
        self._serial.write(payload)
        with suppress(AttributeError):
            self._serial.flush()
        self._log.debug("wrote %d bytes (line=%r)", len(payload), line)

    def read_lines(
        self,
        *,
        last_n: int | None = None,
        follow: bool = False,
        timeout_s: float | None = None,
    ) -> Iterator[str]:
        """Yield lines drained by the background reader.

        Snapshot mode (``follow=False``): yields up to ``last_n`` buffered
        lines, waiting up to ``timeout_s`` for that many to accumulate.

        Follow mode (``follow=True``): yields as new lines arrive.
        ``last_n`` lets the caller see the recent backlog before the
        live tail. With ``timeout_s=None`` the stream runs until the
        consumer breaks out (Ctrl-C at the CLI); an explicit
        ``timeout_s`` bounds the whole stream by wall clock and returns
        cleanly at the deadline (RES-046 — previously the value was
        silently discarded, leaving agent callers no way to bound the
        stream).
        """
        if follow:
            yield from self._stream_follow(last_n=last_n, timeout_s=timeout_s)
            return
        yield from self._stream_snapshot(last_n=last_n, timeout_s=timeout_s)

    def read_lines_with_idle(
        self,
        *,
        timeout_s: float,
        inter_line_idle_ms: int,
    ) -> tuple[tuple[str, ...], bool]:
        """Collect lines until ``inter_line_idle_ms`` idle or ``timeout_s`` wall.

        Returns ``(lines, timeout_hit)``. ``timeout_hit=True`` iff the
        wall-clock budget expired without observing any reply line.

        The wall clock bounds the WHOLE collection (A-016): firmware
        emitting lines faster than the idle gap previously kept the idle
        slice alive unboundedly — a HIL no-long-waits violation.
        """
        start = time.monotonic()
        idle_s = inter_line_idle_ms / 1000.0
        collected: list[str] = []

        deadline = start + timeout_s
        line = self._pop_one(timeout_s=timeout_s)
        if line is None:
            return ((), True)
        collected.append(line)
        # Idle slice — ends on the first inter-line gap OR when the wall
        # budget runs out, whichever comes first.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log.warning(
                    "send_and_read reply still streaming at the %.1fs "
                    "wall budget; returning the %d lines collected",
                    timeout_s,
                    len(collected),
                )
                return (tuple(collected), False)
            follow = self._pop_one(timeout_s=min(idle_s, remaining))
            if follow is None:
                return (tuple(collected), False)
            collected.append(follow)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_serial(self):
        factory = self._serial_factory
        if factory is None:
            try:
                import serial  # type: ignore
            except Exception as ex:  # pragma: no cover - dev deps include pyserial
                raise VCPError(
                    message="pyserial not importable",
                    vcp_marker="pyserial-missing",
                    hint="pip install pyserial",
                ) from ex
            factory = serial.Serial
        try:
            return factory(
                port=self.port,
                baudrate=self.baud,
                timeout=self._defaults.drain_read_timeout_ms / 1000.0,
            )
        except PermissionError as ex:
            raise VCPPortInUse(
                message=f"could not open {self.port}: {ex}",
                vcp_marker="port-in-use",
                port=self.port,
                hint="close any minicom / screen / picocom / Cutecom holding the port",
            ) from ex
        except Exception as ex:
            # pyserial raises ``serial.SerialException`` which subclasses OSError.
            raise VCPPortInUse(
                message=f"could not open {self.port}: {ex}",
                vcp_marker="port-in-use",
                port=self.port,
                hint="close any external tool holding the port",
            ) from ex

    def _drain(self) -> None:
        ser = self._serial
        if ser is None:
            return
        term_bytes = self.terminator.encode("utf-8")
        while not self._stop.is_set():
            try:
                chunk = ser.read(256)
            except Exception as ex:
                # IMP-17: the reader is now a zombie — flag it so
                # is_alive() reports dead (SB-002 reconnects on the next
                # call) and say so loudly, not at DEBUG.
                self._drain_failed = True
                self._log.warning(
                    "VCP drain thread died on read error (port=%s): %s — "
                    "reader marked stale; next call reconnects",
                    self.port,
                    ex,
                )
                self._line_event.set()
                return
            if not chunk:
                continue
            self._last_byte_ts = time.monotonic()
            self._pending.extend(chunk)
            self._extract_lines(term_bytes)
            self._line_event.set()

    def _extract_lines(self, term_bytes: bytes) -> None:
        # Split on terminator, leaving any partial trailing line in _pending.
        while True:
            idx = self._pending.find(term_bytes)
            if idx == -1:
                return
            raw = bytes(self._pending[:idx])
            del self._pending[: idx + len(term_bytes)]
            # CRLF tolerance: strip trailing CR even when terminator is LF-only.
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                line = raw.decode("utf-8", errors="replace")
                if not self._encoding_warned:
                    self._log.warning(
                        "encoding-error: invalid UTF-8 bytes replaced with U+FFFD"
                    )
                    self._encoding_warned = True
            with self._lock:
                if len(self._buffer) == self._buffer.maxlen:
                    self._drops_since_warn += 1
                self._buffer.append(line)
                if self._drops_since_warn:
                    self._log.warning(
                        "line buffer overflow; dropped %d oldest line(s) "
                        "(capacity=%d)",
                        self._drops_since_warn,
                        self._buffer.maxlen,
                    )
                    self._drops_since_warn = 0

    def _pop_one(self, *, timeout_s: float) -> str | None:
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                if self._buffer:
                    return self._buffer.popleft()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._line_event.clear()
            self._line_event.wait(timeout=min(remaining, 0.1))

    def _stream_snapshot(
        self, *, last_n: int | None, timeout_s: float | None
    ) -> Iterator[str]:
        cap = last_n if last_n is not None else self._defaults.line_buffer_capacity
        budget = timeout_s if timeout_s is not None else 5.0
        deadline = time.monotonic() + budget
        emitted = 0
        while emitted < cap:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            line = self._pop_one(timeout_s=remaining)
            if line is None:
                break
            yield line
            emitted += 1

    def _stream_follow(
        self, *, last_n: int | None, timeout_s: float | None = None
    ) -> Iterator[str]:
        deadline = (
            time.monotonic() + timeout_s if timeout_s is not None else None
        )
        if last_n:
            # IMP-19: snapshot AND clear under one lock acquisition —
            # clearing after the yield loop dropped lines that arrived
            # while the backlog was being yielded.
            with self._lock:
                backlog = list(self._buffer)
                self._buffer.clear()
            for line in backlog[-last_n:]:
                yield line
        while not self._stop.is_set():
            wait = 0.25
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                wait = min(wait, remaining)
            line = self._pop_one(timeout_s=wait)
            if line is None:
                continue
            yield line


def _read_defaults(ctx: "SubstrateContext") -> _VcpReaderDefaults:
    """Extract the ``vcp.*`` knobs from ctx.defaults with safe fallbacks."""
    vcp = getattr(ctx.defaults, "vcp", None)
    return _VcpReaderDefaults(
        line_buffer_capacity=_attr_or(vcp, "line_buffer_capacity", 1000),
        drain_read_timeout_ms=_attr_or(vcp, "drain_read_timeout_ms", 50),
        reconnect_max_s=_attr_or(vcp, "reconnect_max_s", 10.0),
    )


def _attr_or(obj, name: str, default):
    value = getattr(obj, name, None)
    return default if value is None else value
