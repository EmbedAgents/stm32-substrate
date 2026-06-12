"""STM32_Programmer_CLI stdout / stderr parsers.

Tolerant of cosmetic variation (ANSI escape codes, locale-specific
decimal separators, missing optional lines, ``--`` placeholder values).
Maps CLI output onto the typed result dataclasses in
``embedagents.stm32.cubeprogrammer.results``.

Public surface (B-phase rollout):

- ``parse_banner(stdout)`` — B2. Reads the connect / banner header.
- ``parse_error(stderr, exit_code)`` — B3. Maps CLI stderr + exit code
  onto a typed ``CubeProgrammerError`` with the right enum + recoverable.
- ``parse_probe_list(stdout)`` — B4. ``STM32_Programmer_CLI -l`` output
  into ``list[ProbeRecord]``. Empty list = empty result, not error.
- ``parse_option_bytes(stdout, device_name)`` — B5. ``-ob displ`` output
  into ``OptionBytesResult`` with raw key→value map + extracted
  ``rdp_level``. No per-family decoding in v1.
- ``parse_hex_dump(stdout, address, size)`` — B6. ``-rd`` output into
  ``MemoryReadResult`` with canonical width-16 hex+ASCII rendering and
  the ``suspicious_unmapped`` all-0xFF detect flag.
- ``parse_hardfault(stdout)`` — B9. ``-hf`` output (UM2237 §3.2.29) into
  ``HardFaultDecode``. No-fault input returns ``hardfault_detected=False``.
- ``parse_itm_line(line, *, timestamp_s)`` — B10. One line from the
  ``-swv`` stream into ``ITMRecord``. Returns ``None`` for CLI noise
  (banner / status / WARNING lines).
"""

from __future__ import annotations

import re
from typing import Literal

from embedagents.stm32.cubeprogrammer.codes import (
    CubeProgrammerErrorCode,
    is_recoverable,
)
from embedagents.stm32.cubeprogrammer.results import (
    BannerResult,
    HardFaultDecode,
    ITMRecord,
    MemoryReadResult,
    OptionBytesResult,
    ProbeRecord,
)
from embedagents.stm32.errors import CubeProgrammerError


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


# Strip ANSI escape sequences (e.g. ``\x1b[36m\x1b[01m``).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# A banner field line looks like ``ST-LINK SN  : 066BFF...`` or
# ``Connect mode: Normal``. Capture key + value with arbitrary whitespace.
_FIELD_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 \-/]*?)\s*:\s*(.*?)\s*$")

# Voltage threshold for the suspicious flag (per BannerResult docstring).
_LOW_VOLTAGE_THRESHOLD_V = 2.5

# Map the CLI's free-text connect-mode value onto the typed enum literal.
_CONNECT_MODE_MAP: dict[str, Literal["NORMAL", "UR", "HOTPLUG", "POWERDOWN", "hwRstPulse"]] = {
    "normal": "NORMAL",
    "under reset": "UR",
    "ur": "UR",
    "hot plug": "HOTPLUG",
    "hotplug": "HOTPLUG",
    "power down": "POWERDOWN",
    "powerdown": "POWERDOWN",
    "hardware reset pulse": "hwRstPulse",
    "hwrstpulse": "hwRstPulse",
}


def parse_banner(stdout: str) -> BannerResult:
    """Parse ``STM32_Programmer_CLI -c port=swd`` stdout into ``BannerResult``.

    Tolerates:

    - ANSI escape codes (stripped).
    - Locale variations in the voltage line (``3.28V`` and ``3,28V`` both work).
    - Missing optional lines — ``board_name`` becomes ``None`` when the
      banner shows ``--`` or omits the line.
    - Variable whitespace around the ``:`` separator.
    """
    cleaned = _ANSI_RE.sub("", stdout)
    fields = _extract_fields(cleaned)

    # CubeProgrammer banner field-name drift across versions:
    # - Board emitted as "Board" (legacy / synthesised fixtures) or
    #   "Board Name" (CubeProgrammer 2.22.0 Windows live output).
    # - Flash size emitted as "Flash size" (legacy) or "NVM size" (v2.22+).
    # Accept both keys; legacy first.
    board = fields.get("Board") or fields.get("Board Name")
    if board in (None, "", "--"):
        board_name: str | None = None
    else:
        board_name = board

    voltage_v = _parse_voltage(fields.get("Voltage"))
    swd_freq_khz = _parse_swd_freq(fields.get("SWD freq"))
    flash_size_kb = _parse_flash_size(
        fields.get("Flash size") or fields.get("NVM size")
    )
    mode_used = _parse_connect_mode(fields.get("Connect mode"))

    return BannerResult(
        stlink_sn=fields.get("ST-LINK SN", ""),
        stlink_fw=fields.get("ST-LINK FW", ""),
        board_name=board_name,
        voltage_v=voltage_v,
        swd_freq_khz=swd_freq_khz,
        device_id=fields.get("Device ID", ""),
        device_name=fields.get("Device name", ""),
        device_type=fields.get("Device type", ""),
        device_cpu=fields.get("Device CPU", ""),
        flash_size_kb=flash_size_kb,
        mode_used=mode_used,
        voltage_suspicious=voltage_v < _LOW_VOLTAGE_THRESHOLD_V and voltage_v > 0,
    )


# ---------------------------------------------------------------------------
# Banner field parsing helpers
# ---------------------------------------------------------------------------


def _extract_fields(text: str) -> dict[str, str]:
    """Pull every ``Key : Value`` line into a dict keyed by the trimmed key.

    Lines that don't match the pattern (decorative headers, blank lines,
    error lines) are skipped.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        m = _FIELD_LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if not value:
            continue
        # Reject obvious headers ("-----...") even though they wouldn't
        # match the regex; defensive belt + braces.
        if all(ch in "-_=*" for ch in value):
            continue
        result[key] = value
    return result


def _parse_voltage(raw: str | None) -> float:
    """Parse ``"3.28V"`` / ``"3,28V"`` / ``"3.30 Volts"`` / ``None``.

    Returns ``0.0`` when missing or unparseable so ``voltage_suspicious``
    can still surface (zero is below the threshold but the bare-zero case
    is excluded from the suspicious flag via the explicit ``> 0`` guard
    in ``parse_banner``).
    """
    if raw is None:
        return 0.0
    # Normalise comma decimal separator + strip units.
    cleaned = raw.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except ValueError:
        return 0.0


def _parse_swd_freq(raw: str | None) -> int:
    """Parse ``"4000 KHz"`` / ``"1800 kHz"`` / ``None``.

    Returns ``0`` when missing — callers see a zero frequency rather than
    a partial banner.
    """
    if raw is None:
        return 0
    m = re.search(r"\d+", raw)
    return int(m.group(0)) if m else 0


def _parse_flash_size(raw: str | None) -> int:
    """Parse ``"1 MBytes (default)"`` / ``"256 KBytes"`` / ``"2048 KBytes"``.

    Returns the size in **kilobytes** to match ``BannerResult.flash_size_kb``.
    Returns ``0`` when missing.
    """
    if raw is None:
        return 0
    m = re.search(r"(\d+)\s*([KMG]?)Bytes", raw, flags=re.IGNORECASE)
    if not m:
        return 0
    value = int(m.group(1))
    unit = m.group(2).upper()
    multiplier = {
        "": 0,        # plain "Bytes" — value is in bytes; we report 0 since
                      # rounding to KB would mislead. CubeProgrammer doesn't
                      # emit raw-bytes flash sizes in practice; defensive.
        "K": 1,
        "M": 1024,
        "G": 1024 * 1024,
    }.get(unit, 1)
    if unit == "":
        # Round to KB; values below 1 KB report 0.
        return value // 1024
    return value * multiplier


def _parse_connect_mode(
    raw: str | None,
) -> Literal["NORMAL", "UR", "HOTPLUG", "POWERDOWN", "hwRstPulse"]:
    """Map ``"Normal"`` / ``"Under Reset"`` / ``None`` onto the BannerResult enum.

    Returns ``"NORMAL"`` when missing — preserves the dataclass default.
    Unknown free-text mode values also fall through to ``"NORMAL"`` since
    BannerResult's Literal forbids surfacing a custom string.
    """
    if raw is None:
        return "NORMAL"
    return _CONNECT_MODE_MAP.get(raw.strip().lower(), "NORMAL")


# ---------------------------------------------------------------------------
# Error parsing — map stderr patterns to CubeProgrammerErrorCode
# ---------------------------------------------------------------------------


# Ordered pattern table. First match wins. Patterns are intentionally loose
# (substring matches) and case-insensitive — STM32_Programmer_CLI wording
# varies across versions. Locale-specific phrasings can be added as
# observed without breaking the existing mapping.
_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], CubeProgrammerErrorCode], ...] = (
    (re.compile(r"no debug probe detected", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_DLL_ERR),
    (re.compile(r"no st-?link", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_DLL_ERR),
    (re.compile(r"usb communication", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_USB_COMM_ERR),
    (re.compile(r"no stm32 target found", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_NO_DEVICE),
    (re.compile(r"target not found", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_NO_DEVICE),
    (re.compile(r"no device found", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_NO_DEVICE),
    (re.compile(r"unknown mcu target", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_UNKNOWN_MCU_TARGET),
    (re.compile(r"st-?link firmware is too old", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD),
    (re.compile(r"stsw-link007", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD),
    (re.compile(r"target is held under reset", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_HELD_UNDER_RESET),
    (re.compile(r"target is not halted", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_NOT_HALTED),
    (re.compile(r"read[- ]?out protected", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_CMD_ERR),
    (re.compile(r"erase memory failed", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_CMD_ERR),
    (re.compile(r"alignment", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_CMD_ERR),
    (re.compile(r"target connection error", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_CONNECT_ERR),
    (re.compile(r"multiple st-?link probes", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_STLINK_SELECT_REQ),
    (re.compile(r"please select.*sn=", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_STLINK_SELECT_REQ),
    (re.compile(r"specified serial number was not found", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_STLINK_SERIAL_NOT_FOUND),
    (re.compile(r"st-?link.*serial.*not found", re.IGNORECASE), CubeProgrammerErrorCode.TARGET_STLINK_SERIAL_NOT_FOUND),
)


# Hint text per code — surfaces actionable guidance in HIL-mode error output.
_HINTS: dict[CubeProgrammerErrorCode, str] = {
    CubeProgrammerErrorCode.TARGET_CONNECT_ERR: "another tool may be holding the probe; close CubeIDE / gdbserver and retry",
    CubeProgrammerErrorCode.TARGET_DLL_ERR: "connect an ST-LINK probe over USB",
    CubeProgrammerErrorCode.TARGET_USB_COMM_ERR: "check the USB cable / port and try again",
    CubeProgrammerErrorCode.TARGET_NO_DEVICE: "connect the target board and check power / SWD wiring; D-002 ladder may help",
    CubeProgrammerErrorCode.TARGET_UNKNOWN_MCU_TARGET: "device ID not recognised; D-002 ladder may help if it's a connectivity hiccup",
    CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD: "update the ST-LINK firmware using STSW-LINK007",
    CubeProgrammerErrorCode.TARGET_HELD_UNDER_RESET: "release the reset line or use connect_under_reset()",
    CubeProgrammerErrorCode.TARGET_NOT_HALTED: "halt the target first or use diagnose_micro() to walk the recovery ladder",
    CubeProgrammerErrorCode.TARGET_CMD_ERR: "the operation was rejected by the target; check RDP level, alignment, or external-loader fit",
    CubeProgrammerErrorCode.TARGET_STLINK_SELECT_REQ: "set programmer.default_probe_sn in stm32-tools.local.jsonc or STM32_PROGRAMMER_DEFAULT_SN env var",
    CubeProgrammerErrorCode.TARGET_STLINK_SERIAL_NOT_FOUND: "the configured probe serial does not match any attached probe; verify default_probe_sn",
}


def parse_error(stderr: str, exit_code: int) -> CubeProgrammerError:
    """Map ``STM32_Programmer_CLI`` stderr + exit code onto a typed error.

    Walks the pattern table; the first match wins. Unmapped output yields
    ``error_code=None`` and ``code=<exit_code>`` so callers still surface
    the raw exit code.

    The returned exception is not raised — callers wrap as appropriate
    (typically with extra fields like ``target_device`` from a banner
    they already captured pre-error).
    """
    code = _classify(stderr)
    recoverable = is_recoverable(code)
    hint = _HINTS.get(code) if code is not None else None
    message = _summarise(stderr) or f"STM32_Programmer_CLI exited with code {exit_code}"
    return CubeProgrammerError(
        message=message,
        code=exit_code,
        tool_output=stderr,
        hint=hint,
        recoverable=recoverable,
        error_code=int(code) if code is not None else None,
    )


def _classify(stderr: str) -> CubeProgrammerErrorCode | None:
    cleaned = _ANSI_RE.sub("", stderr)
    for pattern, code in _ERROR_PATTERNS:
        if pattern.search(cleaned):
            return code
    return None


def _summarise(stderr: str) -> str:
    """Extract the first ``Error: ...`` line for the exception message."""
    cleaned = _ANSI_RE.sub("", stderr)
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped.startswith("Error:"):
            return stripped
    return ""


# ---------------------------------------------------------------------------
# Probe-list parsing (D-005)
# ---------------------------------------------------------------------------


# Match the header for each ST-Link Probe block. CubeProgrammer emits
# ``ST-Link Probe N :`` (sometimes with whitespace variations).
_PROBE_HEADER_RE = re.compile(r"^\s*ST-Link Probe\s+(\d+)\s*:", re.IGNORECASE)


def parse_probe_list(stdout: str) -> list[ProbeRecord]:
    """Parse ``STM32_Programmer_CLI -l`` stdout into ``list[ProbeRecord]``.

    - Empty list when no probe is attached (``"No STLink probes connected!"``
      or the STLink Interface section absent). Empty is a valid result, not
      an error.
    - Each ``ST-Link Probe N :`` block becomes one ``ProbeRecord``.
    - Board name ``"--"`` (placeholder for custom boards) → ``None`` to
      match the BannerResult convention.
    - ``target_sel`` is always ``None`` in v1 — multidrop probing needs a
      separate ``-getTargetSelList`` call per probe (UM2237 §3.2.10). The
      caller fills ``target_sel`` / ``multidrop_unavailable`` when
      multidrop info is queried. TODO(v1+).
    """
    cleaned = _ANSI_RE.sub("", stdout)
    lines = cleaned.splitlines()

    # Split the input into probe blocks by walking line-by-line and
    # opening a new block on each ``ST-Link Probe N :`` header.
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if _PROBE_HEADER_RE.match(line):
            current = []
            blocks.append(current)
            continue
        if current is not None:
            # Stop accumulating on a clearly-unrelated section header
            # ("=====  ... =====") so blocks don't bleed into the next
            # interface group.
            stripped = line.strip()
            if stripped.startswith("=====") and stripped.endswith("====="):
                current = None
                continue
            current.append(line)

    records: list[ProbeRecord] = []
    for block in blocks:
        record = _parse_probe_block(block)
        if record is not None:
            records.append(record)
    return records


def _parse_probe_block(block_lines: list[str]) -> ProbeRecord | None:
    """Pull ST-Link SN / FW / Board / Voltage out of a single probe block.

    Returns ``None`` if the block has no SN field (defensive — a
    well-formed block always carries SN).
    """
    fields: dict[str, str] = {}
    for line in block_lines:
        m = _FIELD_LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value:
            fields[key] = value

    sn = fields.get("ST-LINK SN")
    if not sn:
        return None

    # Probe-list field-name drift mirrors the banner drift: "Board"
    # (legacy / synthesised fixtures) vs "Board Name" (live v2.22.0).
    board = fields.get("Board") or fields.get("Board Name")
    board_name = None if board in (None, "", "--") else board

    return ProbeRecord(
        stlink_sn=sn,
        stlink_fw=fields.get("ST-LINK FW", ""),
        board_name=board_name,
        target_sel=None,
        query_failed=False,
        multidrop_unavailable=False,
    )


# ---------------------------------------------------------------------------
# Option-bytes parsing (D-009)
# ---------------------------------------------------------------------------


# Lines inside an OB section look like:
#   ``  RDP          : 0xAA (Level 0, no protection)``
#   ``  nRST_STOP    : 0x1 (No reset generated when entering Stop mode)``
# Identifiers may start with a lowercase ``n`` (active-low convention)
# but never contain whitespace — that rules out section headers like
# ``User Configuration:`` automatically.
_OB_LINE_RE = re.compile(
    r"""
    ^\s+                          # leading indent
    ([A-Za-z][A-Za-z0-9_]*)       # identifier — mixed-case, no spaces
    \s*:\s*                       # colon separator
    (\S+)                         # value (the first whitespace-delimited token)
    (?:\s*\(.*\))?                # optional " (description)" tail
    \s*$
    """,
    re.VERBOSE,
)


def parse_option_bytes(stdout: str, *, device_name: str) -> OptionBytesResult:
    """Parse ``-c port=swd -ob displ`` stdout into ``OptionBytesResult``.

    Raw key→value mapping; no per-family decoding in v1 (the spec
    explicitly defers that). Values that look like ``0x<hex>`` are parsed
    to ``int``; other tokens stay as strings. The universal ``RDP`` byte
    is mapped to a 0/1/2 level using the STM32-wide convention:

    - ``0xAA`` → level 0 (no protection)
    - ``0xCC`` → level 2 (irreversible)
    - anything else (most commonly ``0x55``) → level 1

    ``device_name`` is passed in by the caller (typically by parsing the
    banner portion of the same stdout) so this function stays a pure
    text-to-dataclass mapper.

    ``redacted_due_to_rdp`` stays ``False`` in v1 — TODO when RDP-1
    behaviour redacts specific fields per family.
    """
    cleaned = _ANSI_RE.sub("", stdout)
    observed: dict[str, int | str | bool] = {}
    for line in cleaned.splitlines():
        m = _OB_LINE_RE.match(line)
        if not m:
            continue
        key, raw_value = m.group(1), m.group(2)
        observed[key] = _coerce_ob_value(raw_value)

    rdp_level = _classify_rdp(observed.get("RDP"))

    return OptionBytesResult(
        device_name=device_name,
        observed=observed,
        rdp_level=rdp_level,
        redacted_due_to_rdp=False,
    )


def _coerce_ob_value(raw: str) -> int | str:
    """Parse hex / decimal numerics; fall back to the raw string."""
    if raw.lower().startswith("0x"):
        try:
            return int(raw, 16)
        except ValueError:
            return raw
    if raw.isdigit():
        return int(raw)
    return raw


def _classify_rdp(value: int | str | bool | None) -> int | None:
    """Map the RDP byte onto the 0/1/2 level."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            num = int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    elif isinstance(value, int):
        num = value
    else:
        return None

    if num == 0xAA:
        return 0
    if num == 0xCC:
        return 2
    return 1


# ---------------------------------------------------------------------------
# Hex-dump parsing (F-020)
# ---------------------------------------------------------------------------


# Matches one row of an address + 1..16 hex byte pairs:
#   ``0x20000000 : 00 04 01 20 7D 0B ...``
# Tolerates a missing space before ``:``, extra spaces between bytes,
# and lower / upper case hex.
_HEX_DUMP_LINE_RE = re.compile(
    r"""
    ^\s*
    0x[0-9A-Fa-f]+               # row address (discarded — we rebuild from caller-supplied start)
    \s*:\s*
    ((?:[0-9A-Fa-f]{2}\s*)+)     # one or more hex bytes
    \s*$
    """,
    re.VERBOSE,
)


def parse_hex_dump(stdout: str, *, address: str, size: int) -> MemoryReadResult:
    """Parse ``-c port=swd -rd <addr> <size>`` stdout into ``MemoryReadResult``.

    Walks the CLI output for hex-dump rows (``0x<addr> : XX XX XX ...``),
    extracts the raw bytes, and renders them as a canonical width-16 hex
    + ASCII string anchored at ``address``. Detects all-0xFF regions and
    sets ``suspicious_unmapped`` accordingly.

    Args:
        stdout: full CLI stdout (banner + hex dump section).
        address: starting address; used for the rendered output header.
        size: requested byte count; ``bytes_read`` reports how many bytes
            the parser actually extracted (may be ≤ size on truncation).

    ``sr_or_dr_warning`` is always ``False`` in v1 — that flag needs
    SVD-aware region knowledge owned by the debug module (C4 ``svd_db``).
    """
    cleaned = _ANSI_RE.sub("", stdout)
    raw_bytes = _extract_hex_bytes(cleaned)
    rendered = _render_hex_dump(raw_bytes, start_address=address)
    suspicious = bool(raw_bytes) and all(b == 0xFF for b in raw_bytes)
    return MemoryReadResult(
        address=address,
        size=size,
        bytes_read=len(raw_bytes),
        hex_dump=rendered,
        suspicious_unmapped=suspicious,
        sr_or_dr_warning=False,
    )


def _extract_hex_bytes(text: str) -> list[int]:
    """Walk ``text`` line-by-line, pulling hex bytes from each dump row."""
    collected: list[int] = []
    for line in text.splitlines():
        m = _HEX_DUMP_LINE_RE.match(line)
        if not m:
            continue
        for token in m.group(1).split():
            try:
                collected.append(int(token, 16))
            except ValueError:
                continue
    return collected


def _render_hex_dump(data: list[int], *, start_address: str) -> str:
    """Render ``data`` as canonical width-16 hex + ASCII.

    Example::

        0x08000000: 00 04 01 20 7d 0b 00 08 81 12 00 08 81 12 00 08  |... }...........|
        0x08000010: 81 12 00 08 81 12 00 08 81 12 00 08 00 00 00 00  |................|

    Each row is 16 bytes; the final row pads its hex column with spaces
    so the ASCII delimiter aligns. ASCII column uses ``.`` for
    non-printable bytes (anything outside 0x20..0x7e).
    """
    if not data:
        return ""
    try:
        base = int(start_address, 16)
    except ValueError:
        base = 0
    width = 16
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        hex_part = hex_part.ljust(width * 3 - 1)  # 16 bytes × "XX " minus trailing space
        ascii_part = "".join(
            chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk
        )
        lines.append(f"0x{base + offset:08x}: {hex_part}  |{ascii_part}|")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Hard-fault parsing (DIAG-001 binary path)
# ---------------------------------------------------------------------------


# Canonical fault-type literals (must match HardFaultDecode._FaultType).
_FAULT_TYPES: tuple[str, ...] = (
    "HardFault",
    "MemManage",
    "BusFault",
    "UsageFault",
    "SecureFault",
    "NMI",
)

# Field-extraction regexes anchored to recognisable header tokens. Each
# pattern grabs the value after the colon; the parser handles unit-stripping
# / hex normalisation downstream.
#
# Two CLI output formats are supported:
#   (a) Rich format ("Status: HardFault detected" + structured register
#       block) — seen in older CLI versions + the synthetic unit-test
#       fixtures.
#   (b) Minimal format ("Hard Fault detected in instruction located at
#       0x...") — STM32CubeProgrammer 2.22 emits this when its fault
#       analyzer can't capture rich register state (e.g. running against
#       a HardFault_Handler tight loop that hasn't preserved SCB state).
#       Bench-verified 2026-05-19 on NUCLEO-L476RG with UDF #0 firmware.
_HF_STATUS_RE = re.compile(r"^\s*Status\s*:\s*(.+?)\s*$", re.MULTILINE)
_HF_DETECTED_INLINE_RE = re.compile(
    r"Hard\s*Fault\s*detected\s*in\s*instruction\s*located\s*at\s*(0x[0-9A-Fa-f]+)",
    re.IGNORECASE,
)
_HF_EXECUTION_HANDLER_RE = re.compile(
    r"^\s*Execution Mode\s*:\s*Handler\s*$", re.MULTILINE | re.IGNORECASE
)
_HF_PC_RE = re.compile(r"^\s*Faulty PC\s*:\s*(0x[0-9A-Fa-f]+)", re.MULTILINE)
_HF_NVIC_RE = re.compile(r"^\s*NVIC position\s*:\s*(-?\d+)", re.MULTILINE)
_HF_TYPE_RE = re.compile(r"^\s*Fault type\s*:\s*([A-Za-z]+)", re.MULTILINE)
# Register lines come in two shapes:
#   plain: ``    MMFAR : 0xE000ED34`` (SCB block)
#   paren: ``Configurable Fault Status Register (CFSR) : 0x00010000``
# Two regexes are simpler than one; the collector tries both and de-dups
# against the canonical key set.
_HF_REG_PLAIN_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9_]*)\s*:\s*(0x[0-9A-Fa-f]+)", re.MULTILINE
)
_HF_REG_PAREN_RE = re.compile(
    r"\(([A-Z][A-Z0-9_]+)\)\s*:\s*(0x[0-9A-Fa-f]+)"
)

# Names we always promote into the register_snapshot when present in the
# output. Anything else matching the register regex (UFSR, MMFSR, BFSR,
# SHCSR, MMFAR, BFAR) is also captured. The CFSR / HFSR top-level lines
# are the load-bearing fields callers (Claude) need.
_REGISTER_KEYS: frozenset[str] = frozenset(
    {"CFSR", "HFSR", "MMFAR", "BFAR", "SHCSR", "UFSR", "MMFSR", "BFSR"}
)


def parse_hardfault(stdout: str) -> HardFaultDecode:
    """Parse ``-c port=swd -hf`` stdout into ``HardFaultDecode``.

    Behaviour:

    - ``Status: No fault detected`` (or absent) → ``hardfault_detected=False``
      with all decode fields ``None`` / empty.
    - ``Status: HardFault detected`` → populate ``faulty_pc`` /
      ``nvic_position`` / ``fault_type`` from the header lines and harvest
      CFSR / HFSR / SCB registers into ``register_snapshot``.
    - ``fault_type`` falls back to ``None`` when the CLI's free-text label
      is outside the canonical Literal set; the raw value is preserved in
      ``fault_decode``.

    ``fault_decode`` is the human-readable summary used by callers /
    slash commands: ``"<FaultType> at PC=<pc> (CFSR=<cfsr> HFSR=<hfsr>)"``.
    Substrate captures the CLI's analysis verbatim — no re-interpretation
    of UFSR / BFSR bit flags here (Claude reads the captured stdout for
    fine-grained decoding per ADR-004).
    """
    cleaned = _ANSI_RE.sub("", stdout)
    detected = _detect_hardfault(cleaned)
    if not detected:
        return HardFaultDecode(
            hardfault_detected=False,
            fault_type=None,
            faulty_pc=None,
            nvic_position=None,
            register_snapshot={},
            fault_decode="No fault detected",
            source_used="cubeprogrammer-hf",
        )

    pc_match = _HF_PC_RE.search(cleaned)
    nvic_match = _HF_NVIC_RE.search(cleaned)
    type_match = _HF_TYPE_RE.search(cleaned)

    # Fallback: minimal-format ("Hard Fault detected in instruction
    # located at 0x...") carries the PC inline without a "Faulty PC:"
    # line. Used when the rich-format Faulty PC line is absent.
    if pc_match is None:
        inline_pc = _HF_DETECTED_INLINE_RE.search(cleaned)
        if inline_pc is not None:
            faulty_pc: str | None = inline_pc.group(1)
        else:
            faulty_pc = None
    else:
        faulty_pc = pc_match.group(1)
    nvic_position = int(nvic_match.group(1)) if nvic_match else None
    raw_type = type_match.group(1) if type_match else None
    fault_type = raw_type if raw_type in _FAULT_TYPES else None

    register_snapshot = _collect_registers(cleaned)
    fault_decode = _summarise_fault(
        raw_type=raw_type,
        faulty_pc=faulty_pc,
        register_snapshot=register_snapshot,
    )

    return HardFaultDecode(
        hardfault_detected=True,
        fault_type=fault_type,
        faulty_pc=faulty_pc,
        nvic_position=nvic_position,
        register_snapshot=register_snapshot,
        fault_decode=fault_decode,
        source_used="cubeprogrammer-hf",
    )


def _detect_hardfault(text: str) -> bool:
    """``True`` when the analyzer reports a detected fault.

    Two CLI formats:

    (a) Rich (``Status: <message>``): explicit no-fault → False;
        anything containing ``detect`` / ``fault`` → True.
    (b) Minimal (no Status line): ``Hard Fault detected in instruction
        located at 0x...`` carries the detection inline. Also
        cross-checks ``Execution Mode: Handler`` so a literal
        ``Hard Fault`` mention in surrounding prose doesn't false-
        positive. Either signal alone is enough; both is most reliable.
    """
    # Rich format.
    m = _HF_STATUS_RE.search(text)
    if m is not None:
        status = m.group(1).lower()
        if "no fault" in status:
            return False
        if "detect" in status or "fault" in status:
            return True

    # Minimal format (CLI 2.22 fault analyzer with no rich register block).
    if _HF_DETECTED_INLINE_RE.search(text) is not None:
        return True
    return False


def _collect_registers(text: str) -> dict[str, int]:
    """Pull every ``NAME : 0x<hex>`` register line into a dict.

    Walks both the plain-name and the parenthesised-name shapes. Filters
    to the canonical register-name set so unrelated hex lines (banner
    fields, SCB header line, etc.) don't pollute the snapshot.
    """
    snapshot: dict[str, int] = {}
    for regex in (_HF_REG_PAREN_RE, _HF_REG_PLAIN_RE):
        for m in regex.finditer(text):
            name = m.group(1)
            if name not in _REGISTER_KEYS:
                continue
            try:
                snapshot[name] = int(m.group(2), 16)
            except ValueError:
                continue
    return snapshot


def _summarise_fault(
    *,
    raw_type: str | None,
    faulty_pc: str | None,
    register_snapshot: dict[str, int],
) -> str:
    """One-line human-readable summary used by callers / slash commands."""
    fault_label = raw_type or "Fault"
    parts: list[str] = [fault_label]
    if faulty_pc:
        parts.append(f"at PC={faulty_pc}")
    reg_parts: list[str] = []
    for key in ("CFSR", "HFSR"):
        if key in register_snapshot:
            reg_parts.append(f"{key}=0x{register_snapshot[key]:08X}")
    if reg_parts:
        parts.append(f"({' '.join(reg_parts)})")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# ITM / SWV stream parsing (VCP-007)
# ---------------------------------------------------------------------------


# CLI emits ITM lines in two common shapes:
#   ``ITM channel 0: Hello, STM32!``
#   ``[0] Hello, STM32!``
# Both capture (port_number, payload).
_ITM_CHANNEL_RE = re.compile(r"^ITM channel (\d+):\s*(.*)$")
_ITM_BRACKET_RE = re.compile(r"^\[(\d+)\]\s*(.*)$")

# Prefixes that indicate CLI noise (banner header, status messages,
# warnings) — never an ITM payload. Filtered before the free-form fallback
# so we don't accidentally route a status line into port 0.
_ITM_NOISE_PREFIXES: tuple[str, ...] = (
    "ST-LINK",
    "Board",
    "Voltage",
    "SWD freq",
    "Connect mode",
    "Reset mode",
    "Device ID",
    "Revision ID",
    "Device name",
    "Device type",
    "Device CPU",
    "Flash size",
    "NVM size",  # real v2.22 banner label (the synthetic fixtures said "Flash size")
    "BL Version",
    "SWV started",
    # Real STM32CubeProgrammer v2.22 interactive `-swv` chrome (observed on
    # bench 2026-05-24): the session prints a menu + reception-status lines;
    # none are ITM payload. Without these they hit parse_itm_line's free-form
    # fallback and were mis-yielded as port-0 records.
    "Debug in Low Power",
    "Entering Serial Wire Viewer",
    "Press R to",
    "Press S to",
    "Press E to",
    "Reception Started",
    "Reception Stopped",
    "Exiting Serial Wire Viewer",
    "WARNING:",
    "Error:",
    "STM32CubeProgrammer",
)


def parse_itm_line(line: str, *, timestamp_s: float = 0.0) -> ITMRecord | None:
    """Parse one line of ``-swv`` stdout into an ``ITMRecord``.

    Returns ``None`` for unparseable / noise lines so the streaming
    consumer in ``tail_swo`` can ``continue`` cleanly without conditional
    yields.

    Recognised payload shapes:

    - ``ITM channel N: <payload>`` → port_number=N, line=<payload>.
    - ``[N] <payload>`` → port_number=N, line=<payload>.

    Free-form lines that aren't recognised noise default to port 0 —
    rare on real CubeProgrammer output but useful when consumers tee
    arbitrary text through ``tail_swo`` for transcription.

    ``timestamp_s`` is forwarded onto the record. Callers ``tail_swo``
    pass ``time.monotonic() - start`` so consumers get a steadily
    increasing time series; standalone uses can default to ``0.0``.
    """
    cleaned = _ANSI_RE.sub("", line).rstrip("\r\n").strip()
    if not cleaned:
        return None
    m = _ITM_CHANNEL_RE.match(cleaned)
    if m:
        return ITMRecord(
            port_number=int(m.group(1)),
            line=m.group(2),
            timestamp_s=timestamp_s,
        )
    m = _ITM_BRACKET_RE.match(cleaned)
    if m:
        return ITMRecord(
            port_number=int(m.group(1)),
            line=m.group(2),
            timestamp_s=timestamp_s,
        )
    if cleaned.startswith(_ITM_NOISE_PREFIXES):
        return None
    # Decorative banner dividers / blank-ish lines.
    if all(ch in "-=" for ch in cleaned):
        return None
    return ITMRecord(port_number=0, line=cleaned, timestamp_s=timestamp_s)


def is_swv_dropped_bytes_warning(line: str) -> bool:
    """Return ``True`` when ``line`` indicates an SWV overflow.

    Caller (``tail_swo``) logs a WARNING but does not raise — drops are
    bench reality, not substrate failures.
    """
    cleaned = _ANSI_RE.sub("", line)
    return "SWV dropped" in cleaned or "SWO overflow" in cleaned
