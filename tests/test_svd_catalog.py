"""Catalog-wide SVD resolution sweep — software-validated breadth.

Hardware tests exercise the substrate on a handful of attached boards; the
STM32 family spans hundreds of variants. This suite closes that gap *without
hardware* by walking **every** ``.svd`` file the installed Cube tools ship
(CubeIDE / CubeProgrammer / CLT) through the real resolution path:

  1. every device SVD maps to a known core (``_core_for_device``),
  2. every mapped core has a resolvable core SVD on disk (``find_core_for``),
  3. every device SVD resolves by name (``find_for``),
  4. every device SVD parses cleanly **and** the core peripherals every
     diagnostic depends on — ``NVIC`` + ``SCB`` — resolve end-to-end via
     ``get_peripheral`` (device-SVD miss → core-SVD fallback → SCB alias),
  5. no parsed peripheral is register-less (CMSIS ``derivedFrom`` resolved).

This is the test that turns "validated on N boards" into "device resolution
software-validated across the full installed catalog." It is **self-expanding**:
install a new Cube package (a new family, e.g. a not-yet-public series) and
the sweep covers it automatically — failing loudly until ``_FAMILY_CORE``
learns the family, which is the exact "new family = new bug" class that has
repeatedly bitten the bench bring-up.

Marked ``smoke`` (reads vendor-shipped SVD files; no hardware, no CLI spawn).
Skips cleanly when no SVD source root resolves (e.g. CI runners without the
Cube tools installed). Excluded from the default run; invoke with
``pytest -m smoke tests/test_svd_catalog.py``.

The CI-runnable unit guards for the underlying fixes (``_svd_int`` zero-padded
parsing, the C5/U3/V8/GBK1 family-core mappings) live in ``test_debug_svd.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from stm32_substrate.debug.svd import _FAMILY_CORE, _core_for_device
from stm32_substrate.errors import SVDLookupError

if TYPE_CHECKING:
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.debug.svd import SvdDb


@pytest.fixture(scope="module")
def catalog(smoke_ctx: "SubstrateContext") -> "tuple[SvdDb, tuple[str, ...]]":
    """The live SVD db + every device-SVD stem across the resolved roots.

    Module-scoped so the (~MB-scale, hundreds-of-files) parse cache on the
    shared ``SvdDb`` is reused across the full-catalog tests below.
    """
    db = smoke_ctx.svd_db
    if db is None or not db.roots.configured():
        pytest.skip("no SVD source roots configured on this host")
    stems = sorted(
        {
            f.stem
            for root in (
                db.roots.cubeide,
                db.roots.cube_programmer,
                db.roots.stm32cubeclt,
            )
            if root is not None
            for f in root.glob("*.svd")
        }
    )
    if not stems:
        pytest.skip("SVD roots configured but contain no .svd files")
    return db, tuple(stems)


@pytest.mark.smoke
class TestSvdCatalog:
    def test_catalog_is_non_trivial(
        self, catalog: "tuple[SvdDb, tuple[str, ...]]"
    ) -> None:
        """Guard against silently validating an empty catalog."""
        db, stems = catalog
        assert db.roots.configured(), "expected at least one resolved SVD root"
        assert len(stems) >= 50, (
            f"only {len(stems)} device SVDs found — expected the full ST "
            f"catalog (hundreds). Roots: {db.roots.configured()}"
        )

    def test_every_device_svd_maps_to_a_core(
        self, catalog: "tuple[SvdDb, tuple[str, ...]]"
    ) -> None:
        """Every installed device SVD must map to a known Cortex core, or
        ``NVIC`` / ``SCB`` / ``decode-hardfault`` 404 on that family. This is
        the gap that surfaced C5 / U3 / V8 / GBK1 during this sweep's
        authoring."""
        db, stems = catalog
        unmapped = sorted(s for s in stems if _core_for_device(s) is None)
        assert not unmapped, (
            f"{len(unmapped)} device SVD(s) have no core mapping in "
            f"_FAMILY_CORE (their core peripherals won't resolve): {unmapped}"
        )

    def test_every_mapped_core_resolves_to_a_core_svd(
        self, catalog: "tuple[SvdDb, tuple[str, ...]]"
    ) -> None:
        """Every core token in ``_FAMILY_CORE`` must resolve to a real core
        SVD file (guards the ``Cortex-M0plus.svd`` spelling, M55/M85
        presence, etc.)."""
        db, _ = catalog
        cores = sorted(set(_FAMILY_CORE.values()))
        missing = [c for c in cores if db.find_core_for(c) is None]
        assert not missing, (
            f"core SVD(s) not found on disk for mapped core token(s): "
            f"{missing} (all mapped cores: {cores})"
        )

    def test_every_device_svd_resolves_by_name(
        self, catalog: "tuple[SvdDb, tuple[str, ...]]"
    ) -> None:
        """``find_for`` must resolve every device-SVD stem back to a file —
        guards the canonicalisation / trim / wildcard path against the whole
        catalog."""
        db, stems = catalog
        unresolved = sorted(s for s in stems if db.find_for(s) is None)
        assert not unresolved, (
            f"{len(unresolved)} device stem(s) did not resolve via find_for: "
            f"{unresolved}"
        )

    def test_full_catalog_parses_and_core_peripherals_resolve(
        self, catalog: "tuple[SvdDb, tuple[str, ...]]"
    ) -> None:
        """The breadth proof: every device SVD parses without raising, yields
        peripherals, and resolves ``NVIC`` + ``SCB`` end-to-end through the
        core-SVD fallback. This is the path that ``read-peripheral`` and
        ``decode-hardfault`` ride, and the one the zero-padded-resetValue
        crash silently broke on every F2/F4/F7 part."""
        db, stems = catalog
        parse_fail: list[str] = []
        empty_doc: list[str] = []
        nvic_fail: list[str] = []
        scb_fail: list[str] = []

        for stem in stems:
            path = db.find_for(stem)
            if path is None:
                parse_fail.append(f"{stem}: find_for → None")
                continue
            try:
                doc = db.parse(path)
            except Exception as exc:  # noqa: BLE001 — report any parse blow-up
                parse_fail.append(f"{stem}: {type(exc).__name__}: {exc}")
                continue
            if not doc.peripherals:
                empty_doc.append(stem)
            for peripheral, bucket in (("NVIC", nvic_fail), ("SCB", scb_fail)):
                try:
                    db.get_peripheral(stem, peripheral)
                except SVDLookupError as exc:
                    bucket.append(f"{stem}: {exc.gdb_marker}")

        problems: list[str] = []
        if parse_fail:
            problems.append(f"{len(parse_fail)} parse failure(s): {parse_fail[:20]}")
        if empty_doc:
            problems.append(f"{len(empty_doc)} empty document(s): {empty_doc[:20]}")
        if nvic_fail:
            problems.append(f"{len(nvic_fail)} NVIC resolve failure(s): {nvic_fail[:20]}")
        if scb_fail:
            problems.append(f"{len(scb_fail)} SCB resolve failure(s): {scb_fail[:20]}")
        assert not problems, "Full-catalog SVD validation failed:\n" + "\n".join(problems)

    def test_no_peripheral_is_register_less(
        self, catalog: "tuple[SvdDb, tuple[str, ...]]"
    ) -> None:
        """CMSIS ``derivedFrom`` integrity: a real peripheral with zero
        registers means its inheritance went unresolved (the G0-USART2 bug
        class). Across the catalog this should be exactly zero."""
        db, stems = catalog
        register_less: list[str] = []
        for stem in stems:
            doc = db.parse(db.find_for(stem))  # cache hit after the sweep above
            for name, periph in doc.peripherals.items():
                if not periph.registers:
                    register_less.append(f"{stem}:{name}")
        assert not register_less, (
            f"{len(register_less)} register-less peripheral(s) "
            f"(unresolved derivedFrom): {register_less[:30]}"
        )
