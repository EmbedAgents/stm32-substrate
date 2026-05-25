"""VCP result dataclasses.

Per the VCP API spec § "Result types". Frozen + JSON-friendly via the
shared ``cli/_serialize.py`` reducer.

Note: ``VCP.tail()`` has ``success_signal=stream`` — it returns
``Iterator[str]`` (no dataclass wrapper). Per-line timestamps are TODO(v1+).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VCPProbeCandidate:
    """Combined-shape candidate used in ``VCPAmbiguousProbe.candidates``.

    Carries port + serial + board together so the slash-command picker
    sees a full record without an extra ``cubeprogrammer.list_probes()``
    round-trip (per RES-020 #d). ``board_name`` is ``None`` when the
    cubeprogrammer cross-call did not resolve a board for that probe.
    """

    port: str
    serial_number: str
    board_name: str | None


@dataclass(frozen=True)
class VCPPortCandidate:
    """Pure enumeration result from ``discover_vcp_ports()``.

    Wrapper over the ``pyserial.tools.list_ports.ListPortInfo`` fields
    substrate consumes. ``board_name`` stays ``None`` here — it is only
    populated when ``_ensure_reader()`` cross-references
    ``cubeprogrammer.list_probes()`` for multi-probe resolution.
    """

    port: str
    vid: int
    pid: int
    serial_number: str
    board_name: str | None = None


@dataclass(frozen=True)
class PriorVCPState:
    """Captured before a reconnect attempt; carried on ``ReconnectResult``."""

    port: str | None
    baud: int | None
    last_byte_timestamp_s: float | None
    open: bool


@dataclass(frozen=True)
class ReconnectResult:
    """VCP-003 result.

    Per RES-020: returned only on success — ``reconnect()`` raises
    ``VCPError(vcp_marker="reconnect-timeout")`` when the device does
    not re-enumerate within ``max_wait_s``. Callers do not branch on
    a ``"failed"`` status literal.
    """

    port: str
    status: Literal["reconnected", "same_port"]
    prior_state: PriorVCPState
    duration_s: float


@dataclass(frozen=True)
class RequestResponse:
    """VCP-002 result. Shape per the success_signal conventions."""

    sent_line: str
    reply_lines: tuple[str, ...]
    timeout_hit: bool
    duration_s: float
    port: str
    baud: int
    echo_filtered: bool = False
