"""C4b SvdDb tests — 3-path priority lookup + parse + decode_register.

Tests use small synthesised SVD XML files in tmp_path. Real ST SVDs are
~MB-scale; we test the parser shape, not coverage of real device defs."""

from __future__ import annotations

from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.debug.svd import (
    SvdDb,
    SvdSourceRoots,
    _canonical_svd_filename,
    _core_for_device,
    _svd_int,
    resolve_svd_roots,
)
from embedagents.stm32.debug.results import FieldValue, RegisterValue
from embedagents.stm32.errors import SVDLookupError


def _make_svd(
    path: Path,
    *,
    device: str = "STM32L476",
    peripherals: dict[str, dict] | None = None,
) -> Path:
    """Build a minimal CMSIS-SVD XML file.

    ``peripherals`` shape::

        {
            "USART1": {
                "base": "0x40013800",
                "registers": {
                    "CR1": {
                        "offset": "0x00",
                        "width": 32,
                        "access": "read-write",
                        "fields": {
                            "UE":   {"bit_offset": 0, "bit_width": 1},
                            "PCE":  {"bit_offset": 10, "bit_width": 1,
                                     "enums": {0: "even", 1: "odd"}},
                            "M0":   {"bit_offset": 12, "bit_width": 1},
                        },
                    },
                },
            },
        }
    """
    peripherals = peripherals or {}
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    lines.append("<device>")
    lines.append(f"  <name>{device}</name>")
    lines.append("  <peripherals>")
    for periph_name, periph in peripherals.items():
        lines.append("    <peripheral>")
        lines.append(f"      <name>{periph_name}</name>")
        lines.append(f"      <baseAddress>{periph['base']}</baseAddress>")
        lines.append("      <registers>")
        for reg_name, reg in periph["registers"].items():
            lines.append("        <register>")
            lines.append(f"          <name>{reg_name}</name>")
            lines.append(f"          <addressOffset>{reg['offset']}</addressOffset>")
            lines.append(f"          <size>{reg.get('width', 32)}</size>")
            lines.append(f"          <access>{reg.get('access', 'read-write')}</access>")
            lines.append(f"          <resetValue>{reg.get('reset', '0x0')}</resetValue>")
            lines.append("          <fields>")
            for fld_name, fld in reg.get("fields", {}).items():
                lines.append("            <field>")
                lines.append(f"              <name>{fld_name}</name>")
                lines.append(f"              <bitOffset>{fld['bit_offset']}</bitOffset>")
                lines.append(f"              <bitWidth>{fld['bit_width']}</bitWidth>")
                if "enums" in fld:
                    lines.append("              <enumeratedValues>")
                    for val, n in fld["enums"].items():
                        lines.append("                <enumeratedValue>")
                        lines.append(f"                  <name>{n}</name>")
                        lines.append(f"                  <value>{val}</value>")
                        lines.append("                </enumeratedValue>")
                    lines.append("              </enumeratedValues>")
                lines.append("            </field>")
            lines.append("          </fields>")
            lines.append("        </register>")
        lines.append("      </registers>")
        lines.append("    </peripheral>")
    lines.append("  </peripherals>")
    lines.append("</device>")
    path.write_text("\n".join(lines))
    return path


@pytest.fixture()
def stm32l476_svd(tmp_path: Path) -> Path:
    """USART1.CR1 with three SVD-typical bitfields."""
    return _make_svd(
        tmp_path / "STM32L476.svd",
        device="STM32L476",
        peripherals={
            "USART1": {
                "base": "0x40013800",
                "registers": {
                    "CR1": {
                        "offset": "0x00",
                        "width": 32,
                        "access": "read-write",
                        "reset": "0x0",
                        "fields": {
                            "UE": {"bit_offset": 0, "bit_width": 1},
                            "PCE": {
                                "bit_offset": 10,
                                "bit_width": 1,
                                "enums": {0: "even", 1: "odd"},
                            },
                            "M0": {"bit_offset": 12, "bit_width": 1},
                        },
                    },
                },
            },
        },
    )


# ---------------------------------------------------------------------------
# _canonical_svd_filename
# ---------------------------------------------------------------------------


class TestCanonicalSvdFilename:
    @pytest.mark.parametrize(
        "device_name,expected",
        [
            ("STM32L476RG", "STM32L476.svd"),
            ("STM32L476", "STM32L476.svd"),
            ("STM32H743ZI", "STM32H743.svd"),
            ("STM32F401RE", "STM32F401.svd"),
            ("STM32N657XX", "STM32N657.svd"),
        ],
    )
    def test_strips_package_suffix(self, device_name: str, expected: str) -> None:
        assert _canonical_svd_filename(device_name) == expected

    def test_passthrough_when_no_suffix(self) -> None:
        # Already family-grouped → unchanged.
        assert _canonical_svd_filename("STM32H7Sxx") == "STM32H7Sxx.svd"

    def test_garbage_input_passthrough(self) -> None:
        assert _canonical_svd_filename("not-an-stm32") == "not-an-stm32.svd"


# ---------------------------------------------------------------------------
# _svd_int — CMSIS scaledNonNegativeInteger parsing
# ---------------------------------------------------------------------------


class TestSvdInt:
    """``_svd_int`` must accept the literal forms real ST device SVDs emit.

    The zero-padded-decimal cases are regression guards for the bug the
    catalog sweep found: ``int(x, 0)`` rejects ``"00000010"`` /  ``"007"``,
    which crashed every STM32F2/F4/F7 device-SVD parse (uncaught) and
    silently dropped zero-padded enum values on 64 files."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("0x20", 32),  # hex prefix
            ("32", 32),  # plain decimal
            ("0", 0),
            ("00000010", 10),  # zero-padded decimal (real resetValue) → 10
            ("007", 7),  # zero-padded enum value
            ("019", 19),
            ("0b101", 5),  # binary prefix
            ("0o17", 15),  # octal prefix
            ("#1010", 10),  # CMSIS '#' binary prefix
            ("+5", 5),  # signed
            ("4k", 4096),  # CMSIS binary-multiplier suffix (x2^10)
            ("2K", 2048),  # suffix is case-insensitive
            ("1m", 1 << 20),  # x2^20
            ("0x10", 16),  # hex never mis-split (no k/m/g/t hex digit)
        ],
    )
    def test_parses_literal_forms(self, text: str, expected: int) -> None:
        assert _svd_int(text) == expected

    def test_zero_padded_is_decimal_not_octal(self) -> None:
        # "010" is decimal 10 per the SVD spec (bare digits = decimal), not
        # octal 8 — the base-10 fallback must not reinterpret it.
        assert _svd_int("010") == 10


# ---------------------------------------------------------------------------
# 3-path priority
# ---------------------------------------------------------------------------


class TestThreePathPriority:
    def test_cubeide_wins(self, tmp_path: Path) -> None:
        ide_dir = tmp_path / "ide"
        cp_dir = tmp_path / "cp"
        clt_dir = tmp_path / "clt"
        for d in (ide_dir, cp_dir, clt_dir):
            d.mkdir()
            _make_svd(d / "STM32L476.svd")
        db = SvdDb(
            roots=SvdSourceRoots(
                cubeide=ide_dir,
                cube_programmer=cp_dir,
                stm32cubeclt=clt_dir,
            )
        )
        result = db.find_for("STM32L476RG")
        assert result == ide_dir / "STM32L476.svd"

    def test_cube_programmer_fallback(self, tmp_path: Path) -> None:
        cp_dir = tmp_path / "cp"
        clt_dir = tmp_path / "clt"
        cp_dir.mkdir()
        clt_dir.mkdir()
        _make_svd(cp_dir / "STM32L476.svd")
        _make_svd(clt_dir / "STM32L476.svd")
        db = SvdDb(
            roots=SvdSourceRoots(
                cubeide=None,  # missing
                cube_programmer=cp_dir,
                stm32cubeclt=clt_dir,
            )
        )
        result = db.find_for("STM32L476RG")
        assert result == cp_dir / "STM32L476.svd"

    def test_clt_last_resort(self, tmp_path: Path) -> None:
        clt_dir = tmp_path / "clt"
        clt_dir.mkdir()
        _make_svd(clt_dir / "STM32L476.svd")
        db = SvdDb(
            roots=SvdSourceRoots(
                cubeide=None,
                cube_programmer=None,
                stm32cubeclt=clt_dir,
            )
        )
        assert db.find_for("STM32L476RG") == clt_dir / "STM32L476.svd"

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        db = SvdDb(
            roots=SvdSourceRoots(
                cubeide=empty, cube_programmer=None, stm32cubeclt=None
            )
        )
        assert db.find_for("STM32L476RG") is None

    def test_dual_core_falls_back_to_cm7_split_svd(self, tmp_path: Path) -> None:
        # Dual-core parts ship core-split SVDs (STM32H747_CM7.svd) with no
        # plain STM32H747.svd. Regression guard for the H747I-DISCO bring-up.
        clt = tmp_path / "clt"
        clt.mkdir()
        _make_svd(clt / "STM32H747_CM7.svd")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_for("STM32H747XIHx") == clt / "STM32H747_CM7.svd"

    def test_plain_svd_wins_over_cm7_variant(self, tmp_path: Path) -> None:
        # A single-core part with a plain .svd must NOT be shadowed by the
        # _CM7 fallback — plain candidates are tried first.
        clt = tmp_path / "clt"
        clt.mkdir()
        _make_svd(clt / "STM32H743.svd")
        _make_svd(clt / "STM32H743_CM7.svd")  # decoy
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_for("STM32H743ZITx") == clt / "STM32H743.svd"

    def test_trailing_x_wildcard_expands_to_digits(self, tmp_path: Path) -> None:
        # CubeProgrammer emits a family glob (``STM32G07x``) in the connect
        # banner; the trailing single ``x`` must expand to concrete digits
        # so ``svd_for_attached`` resolves. Regression guard for the G0
        # bring-up (banner ``STM32G07x/STM32G08x``).
        clt = tmp_path / "clt"
        clt.mkdir()
        _make_svd(clt / "STM32G070.svd")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_for("STM32G07x") == clt / "STM32G070.svd"

    def test_eight_char_family_stem_resolves(self, tmp_path: Path) -> None:
        # The H7RS family SVD is the 8-char ``STM32H7S.svd``; the ordering
        # code ``STM32H7S7L8Hx`` must trim down to it (floor is 8, not 9).
        # Regression guard for the H7S78-DK bring-up.
        clt = tmp_path / "clt"
        clt.mkdir()
        _make_svd(clt / "STM32H7S.svd")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_for("STM32H7S7L8Hx") == clt / "STM32H7S.svd"

    def test_trailing_x_wildcard_picks_first_present(self, tmp_path: Path) -> None:
        # When several digit-expansions ship a .svd, the lowest digit wins
        # (most-specific-first ordering). STM32G08x → G081 here.
        clt = tmp_path / "clt"
        clt.mkdir()
        _make_svd(clt / "STM32G081.svd")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_for("STM32G08x") == clt / "STM32G081.svd"

    def test_all_roots_none(self) -> None:
        db = SvdDb(roots=SvdSourceRoots())
        assert db.find_for("STM32L476RG") is None
        assert db.roots.configured() == ()


# ---------------------------------------------------------------------------
# find_core_for
# ---------------------------------------------------------------------------


class TestFindCore:
    def test_cube_programmer_cores_dir(self, tmp_path: Path) -> None:
        cp = tmp_path / "cp"
        (cp / "Cores").mkdir(parents=True)
        target = cp / "Cores" / "Cortex-M4.svd"
        target.write_text("<device/>")
        db = SvdDb(
            roots=SvdSourceRoots(cube_programmer=cp)
        )
        assert db.find_core_for("M4") == target

    def test_clt_core_singular(self, tmp_path: Path) -> None:
        clt = tmp_path / "clt"
        (clt / "Core").mkdir(parents=True)
        target = clt / "Core" / "Cortex-M7.svd"
        target.write_text("<device/>")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_core_for("M7") == target

    def test_cubeide_skipped_for_cores(self, tmp_path: Path) -> None:
        ide = tmp_path / "ide"
        ide.mkdir()
        # Even if a Cortex-M4.svd existed under cubeide, the lookup skips
        # it because CubeIDE doesn't ship core SVDs.
        (ide / "Cortex-M4.svd").write_text("<device/>")
        db = SvdDb(roots=SvdSourceRoots(cubeide=ide))
        assert db.find_core_for("M4") is None

    def test_m0plus_maps_to_plus_spelled_filename(self, tmp_path: Path) -> None:
        # ST spells the Cortex-M0+ core SVD ``Cortex-M0plus.svd`` (``+`` is
        # not filename-friendly). The ``M0+`` family-map token must resolve
        # to it — regression guard for the M0+ boards (C0/G0/L0/U0), which
        # otherwise 404 SCB/NVIC/decode-hardfault.
        clt = tmp_path / "clt"
        (clt / "Core").mkdir(parents=True)
        target = clt / "Core" / "Cortex-M0plus.svd"
        target.write_text("<device/>")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        assert db.find_core_for("M0+") == target


class TestDerivedFrom:
    """CMSIS-SVD ``derivedFrom`` resolution — a derived peripheral with no
    own registers inherits the base's register map (keeping its own name +
    base address). Regression guard for the G0 bring-up, where USART2 et al.
    are ``derivedFrom="USART1"`` and otherwise decode with zero registers."""

    @staticmethod
    def _write_derived_svd(path: Path) -> Path:
        path.write_text(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
            "<device>\n"
            "  <name>STM32G070</name>\n"
            "  <peripherals>\n"
            "    <peripheral>\n"
            "      <name>USART1</name>\n"
            "      <baseAddress>0x40013800</baseAddress>\n"
            "      <registers>\n"
            "        <register>\n"
            "          <name>CR1</name>\n"
            "          <addressOffset>0x00</addressOffset>\n"
            "          <size>32</size>\n"
            "          <access>read-write</access>\n"
            "          <resetValue>0x0</resetValue>\n"
            "          <fields>\n"
            "            <field><name>UE</name><bitOffset>0</bitOffset>"
            "<bitWidth>1</bitWidth></field>\n"
            "          </fields>\n"
            "        </register>\n"
            "      </registers>\n"
            "    </peripheral>\n"
            "    <peripheral derivedFrom=\"USART1\">\n"
            "      <name>USART2</name>\n"
            "      <baseAddress>0x40004400</baseAddress>\n"
            "    </peripheral>\n"
            "  </peripherals>\n"
            "</device>\n"
        )
        return path

    def test_derived_peripheral_inherits_registers(self, tmp_path: Path) -> None:
        clt = tmp_path / "clt"
        clt.mkdir()
        self._write_derived_svd(clt / "STM32G070.svd")
        db = SvdDb(roots=SvdSourceRoots(stm32cubeclt=clt))
        usart2 = db.get_peripheral("STM32G070", "USART2")
        # inherits USART1's register map ...
        assert "CR1" in usart2.registers
        assert usart2.registers["CR1"].fields["UE"].bit_width == 1
        # ... but keeps its own base address.
        assert usart2.base_address == 0x40004400


class TestCoreForDevice:
    @pytest.mark.parametrize(
        "device_name, expected",
        [
            ("STM32G070RBTx", "M0+"),
            ("STM32U083RCTx", "M0+"),  # STM32U0 — added with the M0+ fix
            ("STM32L053R8Tx", "M0+"),
            ("STM32L152RETx", "M3"),
            ("STM32U585AIIx", "M33"),
            ("STM32L476RGTx", "M4"),
            ("STM32H743ZITx", "M7"),
            ("STM32N657X0", "M55"),
            # Families added after the catalog-wide sweep found them
            # unmapped (their NVIC/SCB/decode-hardfault otherwise 404):
            ("STM32C591", "M33"),  # STM32C5 — SVD <cpu> says CM33
            ("STM32U375", "M33"),  # STM32U3 — SVD <cpu> says CM33
            ("STM32V863", "M85"),  # STM32V8 — Cortex-M85 (SVD omits <cpu>)
            ("STM32GBK1CBT6", "M4"),  # GBK1 one-off — SVD <cpu> says CM4
            # WBA is Cortex-M33, NOT M4 — and STM32WB is a prefix of STM32WBA,
            # so longest-prefix matching must pick WBA→M33 over WB→M4.
            ("STM32WBA52CGUx", "M33"),
            ("STM32WBA55", "M33"),
            ("STM32WB55RGVx", "M4"),  # plain STM32WB stays M4 (regression guard)
            ("STM32WLE5JCIx", "M4"),  # STM32WL stays M4 (shares the WL prefix)
            ("STM32XYZ", None),  # unknown family
        ],
    )
    def test_family_to_core(self, device_name: str, expected: str | None) -> None:
        assert _core_for_device(device_name) == expected


# ---------------------------------------------------------------------------
# parse + decode_register
# ---------------------------------------------------------------------------


class TestParseAndDecode:
    def test_parse_loads_peripherals(self, stm32l476_svd: Path) -> None:
        db = SvdDb(roots=SvdSourceRoots())
        doc = db.parse(stm32l476_svd)
        assert doc.device_name == "STM32L476"
        assert "USART1" in doc.peripherals
        usart1 = doc.peripherals["USART1"]
        assert usart1.base_address == 0x40013800
        assert "CR1" in usart1.registers

    def test_get_peripheral_routes_through_lookup(
        self, stm32l476_svd: Path, tmp_path: Path
    ) -> None:
        roots_dir = tmp_path / "ide"
        roots_dir.mkdir()
        # Move the synth svd into the root so find_for resolves it.
        stm32l476_svd.rename(roots_dir / "STM32L476.svd")
        db = SvdDb(roots=SvdSourceRoots(cubeide=roots_dir))
        periph = db.get_peripheral("STM32L476RG", "USART1")
        assert periph.name == "USART1"

    def test_unknown_device_raises(self, tmp_path: Path) -> None:
        db = SvdDb(roots=SvdSourceRoots(cubeide=tmp_path))
        with pytest.raises(SVDLookupError) as excinfo:
            db.get_peripheral("STM32UNKNOWN", "USART1")
        assert excinfo.value.gdb_marker == "svd-not-found"
        # HIL rule #1: fail loud *with a hint*.
        assert excinfo.value.hint, "svd-not-found must carry an actionable hint"

    def test_unknown_peripheral_raises(
        self, stm32l476_svd: Path, tmp_path: Path
    ) -> None:
        roots_dir = tmp_path / "cp"
        roots_dir.mkdir()
        stm32l476_svd.rename(roots_dir / "STM32L476.svd")
        db = SvdDb(roots=SvdSourceRoots(cube_programmer=roots_dir))
        with pytest.raises(SVDLookupError) as excinfo:
            db.get_peripheral("STM32L476RG", "NOSUCH")
        assert excinfo.value.gdb_marker == "peripheral-not-in-svd"
        assert excinfo.value.hint, "peripheral-not-in-svd must carry a hint"
        # candidates list the names the device actually exposes.
        assert excinfo.value.candidates

    def test_decode_register_extracts_fields(self, stm32l476_svd: Path) -> None:
        db = SvdDb(roots=SvdSourceRoots())
        doc = db.parse(stm32l476_svd)
        cr1 = doc.peripherals["USART1"].registers["CR1"]
        # UE=1, PCE=1 (odd), M0=1 → raw = (1<<0) | (1<<10) | (1<<12)
        raw = 0b1010000000001 | (1 << 10)  # 0x1401
        decoded = db.decode_register(cr1, raw)
        assert isinstance(decoded, RegisterValue)
        assert decoded.raw_value == raw
        assert decoded.access == "RW"
        ue = decoded.fields["UE"]
        assert ue.raw_value == 1
        pce = decoded.fields["PCE"]
        assert pce.raw_value == 1
        assert pce.enum_name == "odd"
        m0 = decoded.fields["M0"]
        assert m0.raw_value == 1

    def test_decode_register_zero_value(self, stm32l476_svd: Path) -> None:
        db = SvdDb(roots=SvdSourceRoots())
        doc = db.parse(stm32l476_svd)
        cr1 = doc.peripherals["USART1"].registers["CR1"]
        decoded = db.decode_register(cr1, 0)
        for fv in decoded.fields.values():
            assert isinstance(fv, FieldValue)
            assert fv.raw_value == 0
        assert decoded.fields["PCE"].enum_name == "even"

    def test_parse_cached(self, stm32l476_svd: Path) -> None:
        """Repeated parse() of the same path reuses the cached document."""
        db = SvdDb(roots=SvdSourceRoots())
        first = db.parse(stm32l476_svd)
        second = db.parse(stm32l476_svd)
        assert first is second  # same object

    def test_malformed_xml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.svd"
        bad.write_text("<device><not-closed")
        db = SvdDb(roots=SvdSourceRoots())
        with pytest.raises(SVDLookupError) as excinfo:
            db.parse(bad)
        assert excinfo.value.gdb_marker == "svd-parse-failed"

    def test_unparseable_numeric_value_raises_clean_error(
        self, tmp_path: Path
    ) -> None:
        """A numeric value ``_svd_int`` can't read must surface as a clean
        ``svd-parse-failed`` SVDLookupError (with a hint), NOT escape as a
        raw ValueError the CLI's ``except SubstrateError`` wouldn't catch.
        Graceful-failure guard: the parse path can never crash a recipe."""
        bad = tmp_path / "STM32GARBAGE.svd"
        bad.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<device><name>STM32GARBAGE</name><peripherals><peripheral>"
            "<name>X</name><baseAddress>0xZZZ</baseAddress></peripheral>"
            "</peripherals></device>\n"
        )
        db = SvdDb(roots=SvdSourceRoots())
        with pytest.raises(SVDLookupError) as excinfo:
            db.parse(bad)
        assert excinfo.value.gdb_marker == "svd-parse-failed"
        assert excinfo.value.hint

    def test_zero_padded_decimal_values_parse(self, tmp_path: Path) -> None:
        """Regression for the catalog-sweep find: real STM32F2/F4/F7 SVDs
        emit ``<resetValue>00000010</resetValue>`` (crashed parse, uncaught)
        and zero-padded enum ``<value>007</value>`` (silently dropped). Both
        must now parse — reset as decimal 10, the enum as decimal 7."""
        svd = tmp_path / "STM32F4ZEROPAD.svd"
        svd.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<device>\n"
            "  <name>STM32F4ZEROPAD</name>\n"
            "  <peripherals>\n"
            "    <peripheral>\n"
            "      <name>PWR</name>\n"
            "      <baseAddress>0x40007000</baseAddress>\n"
            "      <registers>\n"
            "        <register>\n"
            "          <name>CR</name>\n"
            "          <addressOffset>0x00</addressOffset>\n"
            "          <size>32</size>\n"
            "          <access>read-write</access>\n"
            "          <resetValue>00000010</resetValue>\n"
            "          <fields>\n"
            "            <field>\n"
            "              <name>VOS</name><bitOffset>0</bitOffset>"
            "<bitWidth>4</bitWidth>\n"
            "              <enumeratedValues>\n"
            "                <enumeratedValue><name>scale7</name>"
            "<value>007</value></enumeratedValue>\n"
            "              </enumeratedValues>\n"
            "            </field>\n"
            "          </fields>\n"
            "        </register>\n"
            "      </registers>\n"
            "    </peripheral>\n"
            "  </peripherals>\n"
            "</device>\n"
        )
        db = SvdDb(roots=SvdSourceRoots())
        doc = db.parse(svd)  # must not raise
        cr = doc.peripherals["PWR"].registers["CR"]
        assert cr.reset_value == 10  # "00000010" → decimal 10
        # the zero-padded enum value survived (value 7 → "scale7")
        assert cr.fields["VOS"].enums[7] == "scale7"
        decoded = db.decode_register(cr, 7)
        assert decoded.fields["VOS"].enum_name == "scale7"


# ---------------------------------------------------------------------------
# resolve_svd_roots
# ---------------------------------------------------------------------------


class TestResolveSvdRoots:
    def test_all_unresolved_when_tools_none(
        self, tmp_path: Path
    ) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        roots = resolve_svd_roots(ctx)
        assert roots.configured() == ()

    def test_cube_programmer_root_derived_from_cli_grandparent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a CubeProgrammer-shaped tree:
        # tmp/cp/bin/STM32_Programmer_CLI + tmp/cp/SVD/
        cli_dir = tmp_path / "cp" / "bin"
        cli_dir.mkdir(parents=True)
        cli = cli_dir / "STM32_Programmer_CLI"
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o755)
        svd_dir = tmp_path / "cp" / "SVD"
        svd_dir.mkdir()
        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(cli))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        roots = resolve_svd_roots(ctx)
        assert roots.cube_programmer == svd_dir
        assert "cube_programmer" in roots.configured()

    def test_clt_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clt = tmp_path / "clt"
        clt.mkdir()
        (clt / "STMicroelectronics_CMSIS_SVD").mkdir()
        # No CLT executable_name — use env-var override.
        monkeypatch.setenv("STM32CUBECLT", str(clt))
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        # NOTE: the tools-local config maps stm32cubeclt to the install
        # root; resolution path is env_var → config → PATH. We pointed
        # STM32CUBECLT at a directory; substrate accepts dirs as paths
        # via Path.exists() (which is True for dirs).
        roots = resolve_svd_roots(ctx)
        assert roots.stm32cubeclt == clt / "STMicroelectronics_CMSIS_SVD"
