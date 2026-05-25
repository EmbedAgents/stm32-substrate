"""CubeProgrammer-specific result dataclasses.

Every type is ``@dataclass(frozen=True)`` per ADR-006 convention. Field
names match the ``success_signal.fields`` taxonomy in
the behavior spec (P-024); they are part of the API contract.

Many dataclasses default to ``None`` / empty collections to reflect the
post-cubemx-scope-cut reality (RES-020): without a DeviceDB the substrate
returns banner-only fields and surfaces None for what it cannot derive.
Callers handle None gracefully instead of receiving incorrect mini-table
guesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# banner + discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BannerResult:
    """D-001 — full banner. D-003 / D-004 / D-007 / D-008 / D-011 project this."""

    stlink_sn: str
    stlink_fw: str
    board_name: str | None
    voltage_v: float
    swd_freq_khz: int
    device_id: str
    device_name: str
    device_type: str
    device_cpu: str
    flash_size_kb: int
    mode_used: Literal["NORMAL", "UR", "HOTPLUG", "POWERDOWN", "hwRstPulse"] = "NORMAL"
    voltage_suspicious: bool = False


@dataclass(frozen=True)
class ProbeRecord:
    """D-005 list item."""

    stlink_sn: str
    stlink_fw: str
    board_name: str | None
    target_sel: int | None = None
    query_failed: bool = False
    multidrop_unavailable: bool = False


@dataclass(frozen=True)
class BankInfo:
    bank: int
    base_address: str
    size_kb: int


@dataclass(frozen=True)
class MemoryLayoutResult:
    """D-004. ``ram_size_kb`` / ``bank_layout`` are None when not derivable
    from the banner alone (post-cubemx scope cut)."""

    flash_size_kb: int
    ram_size_kb: int | None
    device_name: str
    bank_layout: list[BankInfo] | None = None


@dataclass(frozen=True)
class CoresResult:
    """D-007. Secondary cores / multi-core flag come from a DeviceDB which
    v1 does not ship — substrate returns empty / None and callers surface
    the limitation."""

    device_name: str
    primary_core: str
    secondary_cores: list[str] = field(default_factory=list)
    multi_core: bool | None = None


@dataclass(frozen=True)
class SVDResult:
    """D-008."""

    device_name: str
    device_id: str
    svd_path: Path | None
    svd_version: str | None
    candidates: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class OptionBytesResult:
    """D-009 — raw key→value mapping; no per-family decoding in v1."""

    device_name: str
    observed: dict[str, int | str | bool]
    rdp_level: int | None
    redacted_due_to_rdp: bool = False


@dataclass(frozen=True)
class BooleanResult:
    """D-006 (and future booleans)."""

    value: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# target control (promoted subclasses)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EraseConfirmation:
    """F-001 / F-002. ``erase_and_reset`` sets ``reset_issued=True``."""

    erase_complete: bool
    reset_issued: bool = False
    duration_s: float = 0.0


@dataclass(frozen=True)
class ResetConfirmation:
    """F-016."""

    reset_issued: bool
    via_gdb: bool
    hard: bool = False


# halt / resume keep generic Confirmation in v1 per spec (TODO: promote).


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryAttempt:
    mode: str
    freq_khz: int
    success: bool
    error_code: int | None
    error_message: str | None


@dataclass(frozen=True)
class RecoveryResult:
    """D-002. ``target_responsive=False`` is a valid result, not an error."""

    target_responsive: bool
    recovery_method: str | None
    swd_freq_khz_used: int | None
    attempts_log: list[RecoveryAttempt] = field(default_factory=list)
    bailed_on_timeout: bool = False


# ---------------------------------------------------------------------------
# flash (promoted subclass)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlashConfirmation:
    """F-003/004/005/006/007/008(per leg)/009(per leg)/010/011, CP-001."""

    bytes_written: int
    address: str
    duration_s: float
    bank: int | None = None
    loader_used: str | None = None
    route_used: str | None = None
    address_inferred: bool = False
    user_confirmed: bool = False
    signed: bool = False


@dataclass(frozen=True)
class PairFlashResult:
    """F-008/009 — partial completion semantics."""

    bootloader: FlashConfirmation | None
    application: FlashConfirmation | None
    both_succeeded: bool


# ---------------------------------------------------------------------------
# memory read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryReadResult:
    """F-020. ``hex_dump`` is canonical width-16 hex+ASCII text."""

    address: str
    size: int
    bytes_read: int
    hex_dump: str
    suspicious_unmapped: bool = False
    sr_or_dr_warning: bool = False
    # TODO: expose raw `bytes` field for programmatic consumers.


# ---------------------------------------------------------------------------
# diagnostic
# ---------------------------------------------------------------------------


_FaultType = Literal[
    "HardFault",
    "MemManage",
    "BusFault",
    "UsageFault",
    "SecureFault",
    "NMI",
    "None",
]


@dataclass(frozen=True)
class HardFaultDecode:
    """DIAG-001 binary-only path (gdb path returns its own decode in ``debug``)."""

    hardfault_detected: bool
    fault_type: _FaultType | None
    faulty_pc: str | None
    nvic_position: int | None
    register_snapshot: dict[str, int] = field(default_factory=dict)
    fault_decode: str = ""
    source_used: Literal["cubeprogrammer-hf", "gdb"] = "cubeprogrammer-hf"


@dataclass(frozen=True)
class OptionByteDiffEntry:
    field: str
    observed_value: int | str | bool
    expected_value: int | str | bool


@dataclass(frozen=True)
class OptionBytesDiff:
    """DIAG-018."""

    observed: dict[str, int | str | bool]
    expected: dict[str, int | str | bool]
    diffs: list[OptionByteDiffEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SWO / ITM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ITMRecord:
    """VCP-007 stream item."""

    port_number: int
    line: str
    timestamp_s: float


# ---------------------------------------------------------------------------
# generic fallback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """Generic shape for ops not yet promoted to typed subclasses.

    ``data`` carries the per-op payload documented in
    the CubeProgrammer API spec § "Generic-``Confirmation.data`` shapes":

    - ``halt`` — ``{"halted", "prior_state", "via_gdb"}``
    - ``resume`` — ``{"running", "prior_state", "via_gdb"}``
    - ``read_flash_to_file`` — ``{"bytes_read", "address", "size",
       "output_path", "duration_s"}``
    - ``write_option_bytes`` — ``{"pairs_written", "observed_after",
       "requires_power_cycle", "destructive_ops_confirmed"}``
    """

    operation: str
    data: dict[str, Any] = field(default_factory=dict)
