"""Substrate-wide error hierarchy.

Mirrors ``v1/api-conventions.md`` § "Error class hierarchy". The shapes here
are stable; per-tool fields (markers, error codes, mode) get populated by the
matching tool wrapper. Result dataclasses live in each tool's ``_results.py``;
this module only carries exceptions.

Layout::

    SubstrateError                          # base
    ├── ConfigurationError                  # bad config / missing JSON key (R-003/R-004)
    ├── ResolutionError                     # file / source / tool resolution failed (R-002/R-003)
    ├── ToolError                           # vendor tool subprocess failed
    │   ├── CubeProgrammerError
    │   ├── CubeIDEError
    │   │   └── WorkspaceLockedError
    │   ├── CubeMXError
    │   ├── GDBError
    │   │   └── SVDLookupError
    │   ├── VCPError
    │   │   └── VCPAmbiguousProbe
    │   └── SigningToolError
    ├── ProtocolError                       # XML edit rollback, snapshot failure, atomicity violation
    ├── HardwareError                       # board absent / probe-conflict / RDP-blocked
    └── UserAbortedError                    # AskUserQuestion declined; not really an error

Per ADR-006 conventions, exceptions are ``@dataclass`` (not ``frozen=True``;
Python's exception machinery mutates ``__traceback__`` post-init). Result
types in other modules use ``frozen=True``.

``CubeProgrammerError.error_code`` is typed ``int | None`` here to avoid a
circular import with ``stm32_substrate.cubeprogrammer.codes`` (which owns the
``CubeProgrammerErrorCode`` IntEnum per ``v1/cubeprogrammer-api.md``). The
cubeprogrammer wrapper performs the enum wrap when surfacing the error to
callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SubstrateError(Exception):
    """Base for every substrate-raised exception.

    Fields per ``v1/api-conventions.md`` § "Common fields on every
    ``SubstrateError``".
    """

    message: str
    code: int | str | None = None
    tool_output: str | None = None
    hint: str | None = None
    recoverable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)


@dataclass
class ConfigurationError(SubstrateError):
    """Config file missing, malformed, or schema-invalid.

    Raised by ``SubstrateContext.from_environment()`` per M-016. The extra
    fields drive the loud-error format documented in api-conventions.md
    § "Loud error format".
    """

    schema_name: str | None = None
    json_path: str | None = None
    expected: str | None = None
    actual: str | None = None


@dataclass
class ResolutionError(SubstrateError):
    """File / source / tool resolution failed (R-002 / R-003)."""


@dataclass
class ToolError(SubstrateError):
    """A vendor tool subprocess failed.

    Per-tool subclasses extend with tool-specific fields (markers, exit
    codes, mode attempted, etc.).
    """


@dataclass
class CubeProgrammerError(ToolError):
    """STM32_Programmer_CLI failure.

    ``error_code`` is the raw integer from UM2576 Appendix A; the
    cubeprogrammer wrapper exposes a typed ``CubeProgrammerErrorCode`` enum
    on top of this field.
    """

    error_code: int | None = None
    target_device: str | None = None
    swd_freq_khz: int | None = None
    mode_attempted: str | None = None


@dataclass
class CubeIDEError(ToolError):
    """STM32CubeIDE headless-build / project-import failure."""

    cubeide_marker: str | None = None
    workspace_path: Path | None = None
    project_name: str | None = None
    configuration: str | None = None


@dataclass
class WorkspaceLockedError(CubeIDEError):
    """Workspace held by another holder (GUI or substrate sibling).

    Per RES-010 Q7 / Q8: substrate raises immediately rather than waiting.
    All extra context (``workspace_path`` etc.) inherits from
    ``CubeIDEError``.
    """


@dataclass
class ProjectAmbiguityError(CubeIDEError):
    """``find_project`` returned more than one match (or substring match
    without an exact match) and the caller did not provide an
    ``on_ambiguous`` callback to disambiguate."""

    candidates: tuple[Path, ...] = field(default_factory=tuple)


@dataclass
class CubeMXError(ToolError):
    """STM32CubeMX async-completion / script / launcher failure.

    NOT raised for subprocess non-zero exit — that's
    ``CubeMXResult(success=False, ...)``. Reserved for substrate-side
    preconditions (e.g. missing IOC, launcher unresolvable). Only marker
    in v1: ``ioc-missing``.
    """

    cubemx_marker: str | None = None
    ioc_path: Path | None = None
    output_dir: Path | None = None


@dataclass
class CubeMXLauncherError(CubeMXError):
    """STM32CubeMX launcher binary not resolvable.

    ``checked_candidates`` lists every path the resolver attempted
    (explicit config / env / PATH lookup) so the user can fix the right
    setting.
    """

    checked_candidates: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class GDBError(ToolError):
    """ST-LINK gdbserver / arm-none-eabi-gdb runtime failure.

    NOT raised for build-style outcomes (e.g., breakpoint-never-hit is
    a ``RunResult(breakpoint_hit=False)``, not an exception).
    """

    gdb_marker: str | None = None
    gdbserver_exit_code: int | None = None
    gdb_exit_code: int | None = None
    target_state: str | None = None


@dataclass
class GDBSessionLost(GDBError):
    """Remote connection dropped — target reset / USB yank / gdbserver
    crash. Whole session is dead; caller must re-``start_session()``."""


@dataclass
class TargetNotHalted(GDBError):
    """A method requiring ``target_halted=True`` was called while running."""


@dataclass
class SVDLookupError(GDBError):
    """SVD file not found across the three-priority lookup chain (P-033),
    or a requested peripheral / register name not present in the SVD.
    """

    device_id: str | None = None
    requested_name: str | None = None
    candidates: tuple[Path, ...] = field(default_factory=tuple)
    attempted_paths: tuple[Path, ...] = field(default_factory=tuple)


@dataclass
class VCPError(ToolError):
    """USB virtual COM port / pyserial failure.

    Canonical markers per ``v1/vcp-api.md``:
    ``no-vcp-enumerated`` / ``ambiguous-probe`` / ``port-in-use`` /
    ``reader-already-active`` / ``reconnect-timeout``.
    """

    vcp_marker: str | None = None
    port: str | None = None
    requested_probe_sn: str | None = None


@dataclass
class VCPNotEnumerated(VCPError):
    """No ST-LINK VCP enumerated for the requested probe SN.

    Raised from ``_ensure_reader()`` when ``discover_vcp_ports()`` returns
    an empty list. Recoverable from ``reconnect()`` callers (the device
    may re-enumerate within ``max_wait_s``); non-recoverable on first call.
    """


@dataclass
class VCPAmbiguousProbe(VCPError):
    """Multiple ST-LINK probes enumerated; descriptor cannot disambiguate.

    ``candidates`` carries ``VCPProbeCandidate`` records (defined in the vcp
    module's ``results.py``); typed loosely here to avoid the cross-package
    import. Slash-command path consumes these via ``AskUserQuestion``.
    """

    candidates: tuple = field(default_factory=tuple)


@dataclass
class VCPPortInUse(VCPError):
    """``pyserial.Serial(...)`` raised ``PermissionError`` / ``SerialException``.

    Typically another process owns the port (minicom / screen / picocom).
    Hint: close the conflicting tool and retry.
    """


@dataclass
class VCPReaderAlreadyActive(VCPError):
    """A second reader was requested while one is already open.

    Per HIL-mode (M-019): raise immediately rather than serialise; user
    closes the existing reader.
    """


@dataclass
class SigningToolError(ToolError):
    """STM32_SigningTool_CLI failure (F-013, N6 / MP1 / MP2 only).

    Markers per RES-015 + RES-020:
    ``align-required`` / ``output-exists`` / ``input-file-not-found`` /
    ``signing-cli-failed``.
    """

    signing_marker: str | None = None
    input_path: Path | None = None
    device_family: str | None = None
    header_version: str | None = None


@dataclass
class ProtocolError(SubstrateError):
    """Substrate-internal protocol invariant violated.

    Examples: ``.cproject`` edit rollback failed, snapshot missing,
    destructive op missing ``confirm_irreversible=True`` gate.
    """


@dataclass
class CProjectEditError(ProtocolError):
    """``CProjectEditor`` atomic-edit protocol failure.

    Triggers automatic rollback in ``cubeide.build()``. ``failed_step``
    identifies which protocol stage tripped so callers / logs can route
    the failure correctly.
    """

    backup_path: Path | None = None
    file: Path | None = None
    superclass_attempted: str | None = None
    failed_step: str = "snapshot"
    """One of: snapshot / parse / modify / validate_xml / build_invocation /
    commit / rollback."""


@dataclass
class HardwareError(SubstrateError):
    """Hardware-level condition: board absent, probe contended, RDP locked."""


@dataclass
class UserAbortedError(SubstrateError):
    """User declined an ``AskUserQuestion`` prompt.

    Clean choice, not a failure. Callers should not surface as an error.
    """
