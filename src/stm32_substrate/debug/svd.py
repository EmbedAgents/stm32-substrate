"""SVD lookup + parsing — 3-path priority (CubeIDE → CubeProgrammer → CLT).

Per ``v1/debug-api.md`` § "svd.py — SVD lookup (3-path priority)".
Substrate's decoded register values align with what a developer sees in
CubeIDE's GUI debugger because CubeIDE is the highest-priority source.

Public surface:

- ``SvdSourceRoots`` — frozen dataclass holding the three optional roots.
- ``resolve_svd_roots(ctx)`` — discover the roots from ``ctx.tools.*``.
- ``SvdDb`` — find / parse / decode.

SVD parsing uses stdlib ``xml.etree.ElementTree``. The parsed model is
internal (``SvdPeripheral``, ``SvdRegister``); consumers see
``PeripheralDump`` / ``RegisterValue`` / ``FieldValue`` via
``decode_register``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from stm32_substrate.debug.results import FieldValue, RegisterValue
from stm32_substrate.errors import SVDLookupError

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext


# ---------------------------------------------------------------------------
# Source-root resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SvdSourceRoots:
    """The three SVD source roots, resolved at context-load time."""

    cubeide: Path | None = None
    cube_programmer: Path | None = None
    stm32cubeclt: Path | None = None

    def configured(
        self,
    ) -> tuple[Literal["cubeide", "cube_programmer", "stm32cubeclt"], ...]:
        """Return the subset of source labels that resolved to a real path."""
        out: list[Literal["cubeide", "cube_programmer", "stm32cubeclt"]] = []
        if self.cubeide is not None:
            out.append("cubeide")
        if self.cube_programmer is not None:
            out.append("cube_programmer")
        if self.stm32cubeclt is not None:
            out.append("stm32cubeclt")
        return tuple(out)


def resolve_svd_roots(ctx: "SubstrateContext") -> SvdSourceRoots:
    """Discover the three SVD roots from ``ctx.tools.*``.

    - CubeIDE: glob ``<cubeide_path>/plugins/com.st.stm32cube.ide.mcu.productdb.debug_*``
      for ``resources/cmsis/STMicroelectronics_CMSIS_SVD/``; pick newest
      plugin-dir by version-string sort when multiple are present.
    - CubeProgrammer: ``<cube_programmer_cli>.parent.parent / "SVD"``.
    - CLT: ``<stm32cubeclt_path> / "STMicroelectronics_CMSIS_SVD"``.

    Each returns ``None`` when the corresponding tool is unconfigured or
    the path is absent on disk. ``SvdDb`` degrades gracefully.
    """
    return SvdSourceRoots(
        cubeide=_resolve_cubeide_svd(ctx.tools.cubeide_path),
        cube_programmer=_resolve_cube_programmer_svd(ctx.tools.cube_programmer_cli),
        stm32cubeclt=_resolve_clt_svd(ctx.tools.stm32cubeclt_path),
    )


def _resolve_cubeide_svd(cubeide_path: Path | None) -> Path | None:
    if cubeide_path is None:
        return None
    base = cubeide_path if cubeide_path.is_dir() else cubeide_path.parent
    plugins = base / "plugins"
    if not plugins.is_dir():
        return None
    candidates = sorted(plugins.glob("com.st.stm32cube.ide.mcu.productdb.debug_*"))
    if not candidates:
        return None
    newest = candidates[-1]  # version-string sort ascending → last is newest
    svd_dir = newest / "resources" / "cmsis" / "STMicroelectronics_CMSIS_SVD"
    return svd_dir if svd_dir.is_dir() else None


def _resolve_cube_programmer_svd(cube_programmer_cli: Path | None) -> Path | None:
    if cube_programmer_cli is None:
        return None
    candidate = cube_programmer_cli.parent.parent / "SVD"
    return candidate if candidate.is_dir() else None


def _resolve_clt_svd(stm32cubeclt_path: Path | None) -> Path | None:
    if stm32cubeclt_path is None:
        return None
    candidate = stm32cubeclt_path / "STMicroelectronics_CMSIS_SVD"
    return candidate if candidate.is_dir() else None


# ---------------------------------------------------------------------------
# Internal parsed SVD model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SvdField:
    name: str
    bit_offset: int
    bit_width: int
    access: str | None = None
    enums: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SvdRegister:
    name: str
    address_offset: int
    width_bits: int
    access: Literal["RO", "WO", "RW", "RW_w0c", "RW_w1c", "unknown"]
    reset_value: int
    fields: dict[str, SvdField]


@dataclass(frozen=True)
class SvdPeripheral:
    name: str
    base_address: int
    registers: dict[str, SvdRegister]


@dataclass(frozen=True)
class SvdDocument:
    device_name: str
    peripherals: dict[str, SvdPeripheral]


# ---------------------------------------------------------------------------
# SvdDb — public lookup + decode
# ---------------------------------------------------------------------------


class SvdDb:
    """Read-only SVD lookup with three-source priority + on-demand parse.

    The parsed document for each ``device_name`` is cached so repeated
    register lookups don't reread the file.
    """

    def __init__(self, *, roots: SvdSourceRoots) -> None:
        self.roots = roots
        self._cache: dict[str, SvdDocument] = {}

    # -------- file discovery --------

    def find_for(self, device_name: str) -> Path | None:
        """3-path priority lookup for a vendor SVD file.

        Input is a device name from the CubeProgrammer banner
        (``"STM32L476RG"``), a descriptor ``board.mcu`` carrying the full
        ordering code (``"STM32L476RGTx"``), or an ELF stem. Resolution:

        1. Canonicalise (strip a 2-letter package suffix) and look it up
           across the CubeIDE → CubeProgrammer → CLT priority chain.
        2. On a miss, **trim the trailing ordering code one char at a
           time, re-canonicalising, and retry** — so a full ordering code
           (``STM32L476RGTx`` → … → ``STM32L476.svd``) resolves. The
           filesystem decides: the most-specific name that actually has a
           ``.svd`` wins, so a family name that legitimately keeps a
           longer suffix (``STM32H7Sxx.svd``) is found on the unstripped
           candidate before any over-trim.
        """
        for filename in self._candidate_filenames(device_name):
            for source in (
                self.roots.cubeide,
                self.roots.cube_programmer,
                self.roots.stm32cubeclt,
            ):
                if source is None:
                    continue
                candidate = source / filename
                if candidate.is_file():
                    return candidate
        return None

    @staticmethod
    def _candidate_filenames(device_name: str) -> list[str]:
        """Ordered, de-duplicated SVD filename candidates, most-specific
        first: the canonicalised name, then progressively-trimmed ordering
        codes. Floor of 8 chars keeps at least an ``STM32`` + a 3-char
        series stem (``STM32H7S`` — the H7RS family SVD), while still
        admitting the 4-char families (``STM32L476``). Trimming only
        *appends* shorter candidates after the specific ones, so a board
        that already resolves keeps its first match; the floor only widens
        what an otherwise-unresolved name can reach.

        A trailing single-char ``x`` wildcard (CubeProgrammer emits a
        family glob like ``STM32G07x`` / ``STM32G08x`` in the banner — the
        last digit varies across G070/G071/… which share a device id) is
        expanded to the ten concrete digits; the filesystem then picks the
        first that ships a ``.svd``. Distinct from the trailing ``xx``
        package wildcard, which ``_canonical_svd_filename`` already strips.

        Dual-core parts (STM32H745/H747/H755) ship *core-split* device SVDs
        — ``STM32H747_CM7.svd`` / ``STM32H747_CM4.svd``, no plain
        ``STM32H747.svd`` — so a ``_CM7`` variant of each stem is appended
        as a fallback (CM7 is the boot/primary core). These come *after*
        every plain candidate, so a single-core part keeps its plain match
        and only a part lacking one reaches the ``_CM7`` form.
        """
        candidates: list[str] = []

        def add(name: str) -> None:
            filename = _canonical_svd_filename(name)
            if filename not in candidates:
                candidates.append(filename)

        add(device_name)
        wildcard = re.match(r"^(STM32.*\d)x$", device_name, re.IGNORECASE)
        if wildcard:
            for digit in "0123456789":
                add(f"{wildcard.group(1)}{digit}")
        trimmed = device_name
        while len(trimmed) > 8:
            trimmed = trimmed[:-1]
            add(trimmed)
        # Append CM7 (primary-core) variants of every stem as a fallback for
        # the dual-core core-split SVDs, after all plain candidates.
        for filename in list(candidates):
            cm7 = f"{filename[:-4]}_CM7.svd"
            if cm7 not in candidates:
                candidates.append(cm7)
        return candidates

    def find_core_for(self, core_name: str) -> Path | None:
        """Core (Cortex-M*) SVD lookup. CubeIDE has no core SVDs — skip.

        CubeProgrammer uses ``Cores/`` (plural), CLT uses ``Core/`` (singular).
        ST spells the Cortex-M0+ core SVD ``Cortex-M0plus.svd`` (``+`` isn't
        filename-friendly), so the ``M0+`` token maps to ``...M0plus.svd``.
        """
        stem = core_name if core_name.startswith("Cortex") else f"Cortex-{core_name}"
        filename = f"{stem.replace('+', 'plus')}.svd"
        for source, subdir in (
            (self.roots.cube_programmer, "Cores"),
            (self.roots.stm32cubeclt, "Core"),
        ):
            if source is None:
                continue
            candidate = source / subdir / filename
            if candidate.is_file():
                return candidate
        return None

    # -------- parsing + decoding --------

    def parse(self, svd_path: Path) -> SvdDocument:
        """Parse an SVD file into the internal model. Cached by content path."""
        cache_key = str(svd_path.resolve())
        if cache_key in self._cache:
            return self._cache[cache_key]
        doc = _parse_svd(svd_path)
        self._cache[cache_key] = doc
        return doc

    def get_peripheral(self, device_name: str, peripheral: str) -> SvdPeripheral:
        """Resolve + parse + index a peripheral. Raises ``SVDLookupError``.

        Tries the **device** SVD first; on a miss, falls back to the
        Cortex-M **core** SVD — ``NVIC`` / ``SCB`` / ``SysTick`` / ``MPU``
        live in the core SVD (``Cortex-M4.svd``), not the device SVD, so a
        device-only lookup 404s on them (and on ``decode-hardfault``,
        which reads ``SCB``). The core is derived from the device family
        (``STM32L4`` → ``M4``). Raises only when neither SVD carries it.
        """
        device_svd = self.find_for(device_name)
        if device_svd is not None:
            doc = self.parse(device_svd)
            if peripheral in doc.peripherals:
                return doc.peripherals[peripheral]

        core = _core_for_device(device_name)
        core_svd = self.find_core_for(core) if core else None
        if core_svd is not None:
            core_doc = self.parse(core_svd)
            for alias in _CORE_PERIPHERAL_ALIASES.get(peripheral, (peripheral,)):
                if alias in core_doc.peripherals:
                    return core_doc.peripherals[alias]

        if device_svd is None and core_svd is None:
            raise SVDLookupError(
                message=f"SVD for device {device_name!r} not found",
                gdb_marker="svd-not-found",
                hint=(
                    "no SVD for this device in any configured source "
                    "(CubeIDE / CubeProgrammer / CLT); check the device name, "
                    "or install the STM32Cube package that ships this family's SVD"
                ),
                requested_name=device_name,
                attempted_paths=tuple(
                    p for p in (
                        self.roots.cubeide,
                        self.roots.cube_programmer,
                        self.roots.stm32cubeclt,
                    )
                    if p is not None
                ),
            )
        # At least one SVD resolved, but the peripheral is in neither.
        candidate_names: set[str] = set()
        if device_svd is not None:
            candidate_names.update(self.parse(device_svd).peripherals.keys())
        if core_svd is not None:
            candidate_names.update(self.parse(core_svd).peripherals.keys())
        raise SVDLookupError(
            message=(
                f"peripheral {peripheral!r} not in the device or core SVD "
                f"for {device_name}"
            ),
            gdb_marker="peripheral-not-in-svd",
            hint=(
                "peripheral name not found in this device's SVD; "
                "see candidates for the peripheral names this device exposes"
            ),
            requested_name=peripheral,
            candidates=tuple(Path(p) for p in sorted(candidate_names)),
        )

    def get_register(
        self, device_name: str, peripheral: str, register: str
    ) -> SvdRegister:
        periph = self.get_peripheral(device_name, peripheral)
        if register not in periph.registers:
            raise SVDLookupError(
                message=(
                    f"register {register!r} not in peripheral {peripheral!r} "
                    f"(device {device_name!r})"
                ),
                gdb_marker="register-not-in-svd",
                hint=(
                    "register name not found in this peripheral; "
                    "see candidates for the register names it exposes"
                ),
                requested_name=register,
                candidates=tuple(Path(r) for r in sorted(periph.registers.keys())),
            )
        return periph.registers[register]

    def decode_register(self, register: SvdRegister, raw_value: int) -> RegisterValue:
        """Reduce a raw register value to a ``RegisterValue`` with each
        SVD bitfield decoded into a ``FieldValue``."""
        decoded_fields: dict[str, FieldValue] = {}
        for field_name, fld in register.fields.items():
            mask = (1 << fld.bit_width) - 1
            extracted = (raw_value >> fld.bit_offset) & mask
            enum_name = fld.enums.get(extracted)
            decoded_fields[field_name] = FieldValue(
                name=field_name,
                bit_offset=fld.bit_offset,
                bit_width=fld.bit_width,
                raw_value=extracted,
                enum_name=enum_name,
            )
        return RegisterValue(
            name=register.name,
            address=f"0x{register.address_offset:08X}",
            raw_value=raw_value,
            width_bits=register.width_bits,
            access=register.access,
            fields=decoded_fields,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_svd_filename(device_name: str) -> str:
    """Strip the CubeProgrammer-banner package suffix to derive the
    family-grouped SVD filename.

    Heuristic: if ``device_name`` ends in exactly two letters (either
    case) that follow a digit, those two letters are the package code
    (``RG`` / ``ZI`` / ``XX``) or ST's "any package" wildcard (``xx``)
    and the rest is the SVD-family name. Longer ordering codes (a
    descriptor ``board.mcu`` like ``STM32L476RGTx``) are handled by
    ``SvdDb.find_for``'s filesystem-checked trim fallback, not here —
    family names that legitimately keep a longer suffix (``STM32H7Sxx``)
    must pass through unchanged.

    Examples:

    - ``"STM32L476RG"`` → ``"STM32L476.svd"`` (specific device)
    - ``"STM32H743ZI"`` → ``"STM32H743.svd"``
    - ``"STM32N657XX"`` → ``"STM32N657.svd"``
    - ``"STM32L476xx"`` → ``"STM32L476.svd"`` (CubeProgrammer 2.22+ glob
      variant — banner emits ``...xx`` for the family wildcard)
    - ``"STM32H7Sxx"`` → ``"STM32H7Sxx.svd"`` (family name keeps ``Sxx``)
    - ``"STM32L476"`` → ``"STM32L476.svd"`` (no trailing 2-letter suffix)
    """
    m = re.match(r"^(.*\d)[A-Za-z]{2}$", device_name)
    if m:
        return f"{m.group(1)}.svd"
    return f"{device_name}.svd"


# Device-family → CMSIS core token, for the core-SVD fallback in
# ``SvdDb.get_peripheral`` (NVIC / SCB / SysTick / MPU live in the core
# SVD, not the device SVD). Keyed by the ``STM32`` + series + first-digit
# prefix; matched by ``startswith`` against the upper-cased device name.
# The token feeds ``find_core_for`` → ``Cortex-<token>.svd``.
# Cores are confirmed against each family's SVD ``<cpu><name>`` element
# where present (C5/U3 → CM33, GBK1 → CM4); the M0/M0+ split is silicon
# knowledge ST's SVD ``<cpu>`` simplifies to ``CM0`` for the C0/G0/L0/U0
# Cortex-M0+ parts, so those keep ``M0+`` (the core SVD that ships their
# MPU). V8's SVD omits ``<cpu>`` but the part is Arm Cortex-M85.
_FAMILY_CORE: dict[str, str] = {
    "STM32C0": "M0+",
    "STM32C5": "M33",
    "STM32F0": "M0",
    "STM32F1": "M3",
    "STM32F2": "M3",
    "STM32F3": "M4",
    "STM32F4": "M4",
    "STM32F7": "M7",
    "STM32G0": "M0+",
    "STM32G4": "M4",
    "STM32GBK1": "M4",
    "STM32H5": "M33",
    "STM32H7": "M7",
    "STM32L0": "M0+",
    "STM32L1": "M3",
    "STM32L4": "M4",
    "STM32L5": "M33",
    "STM32U0": "M0+",
    "STM32U3": "M33",
    "STM32U5": "M33",
    "STM32V8": "M85",
    "STM32WB": "M4",
    "STM32WBA": "M33",  # Cortex-M33 wireless family — NOT M4 (longer prefix than STM32WB)
    "STM32WL": "M4",
    "STM32N6": "M55",
}


def _core_for_device(device_name: str) -> str | None:
    """Map a device name (``STM32L476RGTx``) or family (``STM32L4``) to its
    CMSIS core token (``M4``) for the core-SVD fallback. ``None`` when the
    family is unrecognised.

    Matches **longest prefix first** so a more-specific family wins over a
    shorter one that is a prefix of it — e.g. ``STM32WBA*`` resolves to M33,
    not the ``STM32WB``→M4 entry it would otherwise shadow-match.
    """
    name = device_name.upper()
    for prefix in sorted(_FAMILY_CORE, key=len, reverse=True):
        if name.startswith(prefix):
            return _FAMILY_CORE[prefix]
    return None


# Canonical core-peripheral name → the names ST's CMSIS Cortex-M*.svd may
# use. The System Control Block (CFSR / HFSR / AIRCR / VTOR / …) is grouped
# under ``Control`` in ST's core SVDs, not ``SCB`` — so ``read_peripheral
# ("SCB")`` and ``decode-hardfault`` must try the vendor aliases.
_CORE_PERIPHERAL_ALIASES: dict[str, tuple[str, ...]] = {
    "SCB": ("SCB", "Control", "System_Control_Block", "SystemControl", "SCS"),
}


_ACCESS_MAP: dict[str, str] = {
    "read-only": "RO",
    "write-only": "WO",
    "read-write": "RW",
    "writeOnce": "WO",
    "read-writeOnce": "RW",
}


def _parse_svd(svd_path: Path) -> SvdDocument:
    """Parse a CMSIS-SVD XML file into the internal model."""
    try:
        tree = ET.parse(svd_path)
    except ET.ParseError as ex:
        raise SVDLookupError(
            message=f"SVD parse failed: {ex}",
            gdb_marker="svd-parse-failed",
            attempted_paths=(svd_path,),
        ) from ex
    root = tree.getroot()
    device_name = _text(root.find("name"), default=svd_path.stem)

    peripherals: dict[str, SvdPeripheral] = {}
    derived_from: dict[str, str] = {}
    peripherals_root = root.find("peripherals")
    # Numeric parsing (``_svd_int``) can raise ``ValueError`` on a value form
    # the substrate hasn't seen — the zero-padded-decimal crash was one such
    # class (now handled), but a future Cube package could ship another.
    # Convert any such miss into a loud-but-clean ``svd-parse-failed`` so a
    # CLI recipe surfaces structured JSON instead of an uncaught traceback
    # (HIL: fail loud with a hint, never crash raw).
    try:
        if peripherals_root is not None:
            for periph_el in peripherals_root.findall("peripheral"):
                periph = _parse_peripheral(periph_el)
                peripherals[periph.name] = periph
                base = periph_el.get("derivedFrom")
                if base:
                    derived_from[periph.name] = base
    except ValueError as ex:
        raise SVDLookupError(
            message=f"SVD numeric value parse failed: {ex}",
            gdb_marker="svd-parse-failed",
            hint=(
                "the SVD contains a numeric value the parser couldn't read; "
                "report this device's SVD so the parser can be extended"
            ),
            attempted_paths=(svd_path,),
        ) from ex

    # Resolve CMSIS-SVD ``derivedFrom``: a peripheral declared as
    # ``<peripheral derivedFrom="USART1"><name>USART2</name>…</peripheral>``
    # inherits the base's register map (only name / baseAddress / interrupt
    # are overridden). ST uses this heavily — USART2-6, SPI2, DMA2, GPIOC-F,
    # I2C2, several timers — so without resolution those peripherals decode
    # with zero registers. The derived instance keeps its own name + base
    # address; registers are offset-relative and reusable across instances.
    for name, base_name in derived_from.items():
        periph = peripherals.get(name)
        base = peripherals.get(base_name)
        if periph is not None and base is not None and not periph.registers:
            peripherals[name] = replace(periph, registers=base.registers)

    return SvdDocument(device_name=device_name, peripherals=peripherals)


def _parse_peripheral(el: ET.Element) -> SvdPeripheral:
    name = _text(el.find("name"))
    base_address = _svd_int(_text(el.find("baseAddress"), default="0"))
    registers: dict[str, SvdRegister] = {}
    regs_root = el.find("registers")
    if regs_root is not None:
        for reg_el in regs_root.findall("register"):
            reg = _parse_register(reg_el)
            registers[reg.name] = reg
    return SvdPeripheral(name=name, base_address=base_address, registers=registers)


def _parse_register(el: ET.Element) -> SvdRegister:
    name = _text(el.find("name"))
    address_offset = _svd_int(_text(el.find("addressOffset"), default="0"))
    # CMSIS SVD scaledNonNegativeInteger allows hex/oct/bin prefixes
    # (e.g. STM32L476 emits ``<size>0x20</size>`` for 32) and zero-padded
    # decimals (``<resetValue>00000010</resetValue>``); ``_svd_int`` covers
    # both.
    width_bits = _svd_int(_text(el.find("size"), default="32"))
    access_raw = _text(el.find("access"), default="read-write")
    access = _ACCESS_MAP.get(access_raw, "unknown")
    reset_value = _svd_int(_text(el.find("resetValue"), default="0"))

    fields: dict[str, SvdField] = {}
    fields_root = el.find("fields")
    if fields_root is not None:
        for fld_el in fields_root.findall("field"):
            fld = _parse_field(fld_el)
            fields[fld.name] = fld

    return SvdRegister(
        name=name,
        address_offset=address_offset,
        width_bits=width_bits,
        access=access,  # type: ignore[arg-type]
        reset_value=reset_value,
        fields=fields,
    )


def _parse_field(el: ET.Element) -> SvdField:
    name = _text(el.find("name"))
    # SVD supports both bitOffset/bitWidth AND bitRange "[msb:lsb]".
    bit_offset_el = el.find("bitOffset")
    bit_width_el = el.find("bitWidth")
    if bit_offset_el is not None and bit_width_el is not None:
        # ``_svd_int`` per CMSIS SVD scaledNonNegativeInteger - vendor SVDs
        # mix hex (e.g. "0x4"), decimal, and zero-padded decimal across
        # files; we have to accept all.
        bit_offset = _svd_int(_text(bit_offset_el))
        bit_width = _svd_int(_text(bit_width_el))
    else:
        bit_range = _text(el.find("bitRange"), default="[0:0]")
        # Format "[msb:lsb]"
        body = bit_range.strip()[1:-1]
        msb_s, _, lsb_s = body.partition(":")
        msb, lsb = _svd_int(msb_s), _svd_int(lsb_s)
        bit_offset = lsb
        bit_width = msb - lsb + 1
    access = el.find("access")
    access_str = access.text if access is not None and access.text else None

    enums: dict[int, str] = {}
    enum_values = el.find("enumeratedValues")
    if enum_values is not None:
        for ev in enum_values.findall("enumeratedValue"):
            name_el = ev.find("name")
            value_el = ev.find("value")
            if name_el is None or value_el is None or name_el.text is None or value_el.text is None:
                continue
            try:
                enums[_svd_int(value_el.text)] = name_el.text
            except ValueError:
                # Non-numeric enum value (e.g. a "do not care" ``x`` mask
                # token some SVDs use); skip — can't decode to an int.
                continue
    return SvdField(
        name=name,
        bit_offset=bit_offset,
        bit_width=bit_width,
        access=access_str,
        enums=enums,
    )


def _text(el: ET.Element | None, *, default: str = "") -> str:
    if el is None or el.text is None:
        return default
    return el.text.strip()


def _svd_int(text: str) -> int:
    """Parse a CMSIS-SVD ``scaledNonNegativeInteger`` token.

    ``int(text, 0)`` handles ``0x`` / ``0b`` / ``0o`` prefixes and plain
    (unpadded) decimals, but **rejects the zero-padded decimal literals ST
    emits in real device SVDs** — ``<resetValue>00000010</resetValue>``
    (every STM32F2/F4/F7 device SVD) and enum ``<value>007</value>`` (64
    files). A bare zero-padded digit string is decimal per the SVD spec, so
    fall back to base 10; a ``#`` prefix is SVD binary. Without this, F4/F7
    device SVDs raise an uncaught ``ValueError`` mid-parse — breaking
    ``read_peripheral`` / ``decode-hardfault`` on the most widely-used STM32
    parts — and zero-padded enum values silently drop their decoded names.

    A trailing ``k`` / ``m`` / ``g`` / ``t`` (case-insensitive) is the CMSIS
    binary-multiplier suffix (x2^10 / x2^20 / x2^30 / x2^40) the spec permits
    on this type; none of those letters is a hex digit, so a hex literal is
    never mis-split. A genuinely unparseable token still raises ``ValueError``
    — ``_parse_svd`` turns that into a clean ``svd-parse-failed`` error rather
    than letting it escape as a traceback.
    """
    s = text.strip()
    if s.startswith("#"):
        return int(s[1:], 2)
    multiplier = 1
    if s and s[-1] in "kmgtKMGT":
        multiplier = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}[
            s[-1].lower()
        ]
        s = s[:-1]
    try:
        return int(s, 0) * multiplier
    except ValueError:
        return int(s, 10) * multiplier
