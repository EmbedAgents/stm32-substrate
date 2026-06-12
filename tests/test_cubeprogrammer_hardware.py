"""Cubeprogrammer hardware tests — non-destructive.

These run against an attached ST-LINK + (where required) an attached
NUCLEO-L476RG. Excluded from the default ``pytest`` run; invoke with
``pytest -m smoke_with_probe`` or ``pytest -m hardware``.

What's covered (per the bench-smoke reconciliation in `plan-windows.md`):

  - Probe discovery: list_probes (smoke_with_probe — works without a board).
  - Banner / discovery: connect, ping_swd, cores, board_name,
    memory_layout, read_option_bytes, svd_for_attached, diagnose_micro.
  - Atomic target control: halt, resume, reset (soft + hard).
  - Memory reads: read_memory at flash start, default-size, unmapped
    region; read_flash_to_file round-trip.

Destructive operations (erase, OB-write) live in
``test_cubeprogrammer_hardware_destructive.py`` behind the separate
``@pytest.mark.hardware_destructive`` marker so they don't fire by
accident on ``pytest -m hardware``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embedagents.stm32.cubeide import CubeIDE
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.cubeprogrammer.results import (
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    FlashConfirmation,
    HardFaultDecode,
    MemoryLayoutResult,
    MemoryReadResult,
    OptionBytesResult,
    PairFlashResult,
    ProbeRecord,
    ResetConfirmation,
)
from embedagents.stm32.errors import CubeProgrammerError


# ---------------------------------------------------------------------------
# Smoke-with-probe — no specific board required
# ---------------------------------------------------------------------------


@pytest.mark.smoke_with_probe
class TestSmokeWithProbe:
    """Probe enumeration works whenever an ST-LINK is attached. No board
    requirement — even an ST-LINK with ``Board: --`` (custom target) is
    a valid result."""

    def test_list_probes_returns_at_least_one(self, hardware_ctx, attached_boards) -> None:
        if not attached_boards:
            pytest.skip("no ST-LINK probe attached")
        client = CubeProgrammer(hardware_ctx)
        probes = client.list_probes()
        assert len(probes) >= 1
        assert all(isinstance(p, ProbeRecord) for p in probes)
        assert all(p.stlink_sn for p in probes), "every probe must carry a SN"


# ---------------------------------------------------------------------------
# Discovery — L476RG-specific banner shape
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestDiscovery:
    def test_connect_returns_banner(self, l476rg_ctx) -> None:
        banner = CubeProgrammer(l476rg_ctx).connect()
        assert isinstance(banner, BannerResult)
        assert banner.board_name == "NUCLEO-L476RG"
        assert banner.device_id == "0x415", f"L476 family id is 0x415; got {banner.device_id}"
        assert banner.device_cpu == "Cortex-M4"
        assert banner.flash_size_kb == 1024  # 1 MByte → 1024 KB (NVM-size parse)
        assert banner.voltage_v == pytest.approx(3.3, abs=0.15)
        assert banner.voltage_suspicious is False
        assert banner.mode_used == "NORMAL"

    def test_ping_swd_returns_true(self, l476rg_ctx) -> None:
        result = CubeProgrammer(l476rg_ctx).ping_swd()
        assert isinstance(result, BooleanResult)
        assert result.value is True
        assert result.reason is None

    def test_cores_primary_is_cortex_m4(self, l476rg_ctx) -> None:
        result = CubeProgrammer(l476rg_ctx).cores()
        assert isinstance(result, CoresResult)
        assert result.primary_core == "Cortex-M4"
        assert result.secondary_cores == []

    def test_board_name_helper(self, l476rg_ctx) -> None:
        assert CubeProgrammer(l476rg_ctx).board_name() == "NUCLEO-L476RG"

    def test_memory_layout_flash_size_1024kb(self, l476rg_ctx) -> None:
        layout = CubeProgrammer(l476rg_ctx).memory_layout()
        assert isinstance(layout, MemoryLayoutResult)
        assert layout.flash_size_kb == 1024
        # ram_size_kb + bank_layout are intentionally None in v1 per RES-020 #a.

    def test_read_option_bytes_rdp_unprotected(self, l476rg_ctx) -> None:
        ob = CubeProgrammer(l476rg_ctx).read_option_bytes()
        assert isinstance(ob, OptionBytesResult)
        # RDP = 0xAA (170) is level 0 (unprotected). 0xCC (204) is level 2
        # (irreversibly locked); 0xBB is anything else (level 1).
        rdp = ob.observed.get("RDP")
        assert rdp == 170, f"L476RG bench should be unprotected; got RDP={rdp}"
        assert ob.rdp_level == 0

    def test_svd_for_attached_resolves_l4_family(self, l476rg_ctx) -> None:
        """CubeProgrammer 2.22 banner emits a multi-family glob for L476:
        ``STM32L4x1/STM32L475xx/STM32L476xx/STM32L486xx``. Substrate
        walks the variants in order and returns the first that resolves
        on disk — on most installs that's ``STM32L475.svd`` (the L475
        register map is a subset of L476's; either is acceptable for
        L476RG hardware tests since the substrate doesn't decode L476-
        specific peripherals from this lookup result directly).

        TODO(v1+): when device_id→exact-family mapping lands, pick the
        L476 variant deterministically (id 0x415 ⇒ L476). For now,
        first-match-wins per the substrate's documented behaviour."""
        svd = CubeProgrammer(l476rg_ctx).svd_for_attached()
        assert svd.svd_path is not None, (
            f"expected an L4 family SVD; banner emitted {svd.device_name!r}"
        )
        assert svd.svd_path.is_file()
        # Accept any L4 family variant — the L475/L476/L486 SVDs share
        # the same core peripheral register layout for L476RG operations.
        assert svd.svd_path.name in {
            "STM32L4x1.svd",
            "STM32L475.svd",
            "STM32L476.svd",
            "STM32L486.svd",
        }, f"unexpected SVD: {svd.svd_path.name}"

    def test_diagnose_micro_succeeds_on_first_attempt(self, l476rg_ctx) -> None:
        """L476RG should be reachable on the first ladder rung. We assert
        only that the target responded and the ladder didn't bail on
        timeout — the specific recovery_method is allowed to drift."""
        result = CubeProgrammer(l476rg_ctx).diagnose_micro()
        assert result.target_responsive is True
        assert result.bailed_on_timeout is False
        assert result.attempts_log, "expected at least one attempt logged"
        assert result.attempts_log[0].success is True


# ---------------------------------------------------------------------------
# Atomic target control — halt / resume / reset
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAtomicTargetControl:
    def test_reset_returns_confirmation(self, l476rg_ctx) -> None:
        result = CubeProgrammer(l476rg_ctx).reset()
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is False
        assert result.via_gdb is False  # no active debug session

    def test_hard_reset_returns_confirmation(self, l476rg_ctx) -> None:
        result = CubeProgrammer(l476rg_ctx).reset(hard=True)
        assert isinstance(result, ResetConfirmation)
        assert result.reset_issued is True
        assert result.hard is True

    def test_halt_then_resume_round_trip(self, l476rg_ctx) -> None:
        """Halt + resume both succeed back-to-back. We don't assert
        prior_state because v1 leaves it 'unknown' (TODO state-probe)."""
        client = CubeProgrammer(l476rg_ctx)
        halt_result = client.halt()
        try:
            assert isinstance(halt_result, Confirmation)
            assert halt_result.data.get("halted") is True
        finally:
            resume_result = client.resume()
            assert isinstance(resume_result, Confirmation)
            assert resume_result.data.get("running") is True


# ---------------------------------------------------------------------------
# Memory reads
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestMemoryReads:
    def test_read_64_bytes_from_flash_start(self, l476rg_ctx) -> None:
        result = CubeProgrammer(l476rg_ctx).read_memory("0x08000000", size=64)
        assert isinstance(result, MemoryReadResult)
        assert result.address == "0x08000000"
        assert result.size == 64
        assert result.bytes_read == 64
        assert result.suspicious_unmapped is False, (
            "flash@0x08000000 should never be all 0xFF on a board with any "
            "previously-loaded firmware (substrate flags this as suspicious)"
        )

    def test_read_default_256_bytes(self, l476rg_ctx) -> None:
        """``size`` defaults to 256 when omitted (per F-020 spec)."""
        result = CubeProgrammer(l476rg_ctx).read_memory("0x08000000")
        assert result.size == 256
        assert result.bytes_read == 256

    def test_read_unmapped_region_does_not_crash(self, l476rg_ctx) -> None:
        """Reading from 0x90000000 (unmapped QSPI region on L476) — the
        substrate must either return a result or raise a typed error,
        never silently crash.

        Real bench observation: CubeProgrammer 2.22 returns all-0x00 for
        the unmapped QSPI window on L476RG (likely the bus matrix's
        unmapped-read behaviour), not all-0xFF. ``suspicious_unmapped``
        only flags the all-0xFF case (matches erased flash); this test
        therefore just asserts substrate handles the call gracefully
        without crashing — the bus-matrix-returns-zeros case is not
        currently distinguishable from a legitimate all-zero read.
        TODO(v1+): expand the heuristic to also flag all-0x00 reads
        from regions outside the known flash/RAM map."""
        from embedagents.stm32.errors import CubeProgrammerError

        client = CubeProgrammer(l476rg_ctx)
        try:
            result = client.read_memory("0x90000000", size=64)
            assert result.bytes_read == 64
        except CubeProgrammerError:
            # Some CLI versions reject the unmapped read with TARGET_CMD_ERR
            # rather than returning zeros. Equally acceptable.
            pass


@pytest.mark.hardware
class TestReadFlashToFile:
    def test_read_first_1kb_to_file(self, l476rg_ctx, tmp_path: Path) -> None:
        """Smoke test for F-019 — read a small slice of flash to a file
        and verify the file landed with the expected byte count.

        Uses an explicit ``size`` to keep the test fast; the default
        (entire flash) is exercised on a slower bench-smoke pass."""
        out = tmp_path / "flash-first-1kb.bin"
        result = CubeProgrammer(l476rg_ctx).read_flash_to_file(
            address="0x08000000",
            size=1024,
            output_path=out,
        )
        assert out.is_file()
        assert out.stat().st_size == 1024
        # read_flash_to_file returns a generic Confirmation with a
        # ``data`` dict documented at results.Confirmation docstring.
        assert result.operation == "read_flash_to_file"
        assert result.data["size"] == 1024
        assert result.data["bytes_read"] == 1024
        assert result.data["address"] == "0x08000000"
        assert result.data["output_path"] == str(out)


# ---------------------------------------------------------------------------
# Flash pair (F-008 / F-009)
# ---------------------------------------------------------------------------


_BLINKY_ELF_RELATIVE = Path(
    "Projects/NUCLEO-L476RG/Examples/GPIO/BLINKY/STM32CubeIDE/Debug/BLINKY.elf"
)
_VCP_ECHO_ELF_RELATIVE = Path(
    "Projects/NUCLEO-L476RG/Examples/PWR/VCP-ECHO/STM32CubeIDE/Debug/VCP-ECHO.elf"
)


@pytest.fixture
def blinky_elf(l476rg_ctx) -> Path:
    """L476RG BLINKY ELF under the F-PROJ tree."""
    elf = (l476rg_ctx.cwd / _BLINKY_ELF_RELATIVE).resolve()
    if not elf.is_file():
        pytest.skip(
            f"BLINKY ELF not built at {elf}; build the F-PROJ blinky first"
        )
    return elf


@pytest.fixture
def vcp_echo_elf(l476rg_ctx) -> Path:
    """L476RG VCP-ECHO ELF (PWR_ModesSelection retargeted as polled-char-echo
    firmware) under the F-PROJ tree."""
    elf = (l476rg_ctx.cwd / _VCP_ECHO_ELF_RELATIVE).resolve()
    if not elf.is_file():
        pytest.skip(
            f"VCP-ECHO ELF not built at {elf}; rebuild VCP-ECHO first"
        )
    return elf


@pytest.fixture
def restore_blinky_after(l476rg_ctx, blinky_elf: Path):
    """Re-flash blinky after the test runs, regardless of what the test
    left on the chip. Downstream test files (test_debug_hardware,
    test_vcp_hardware after VCP-ECHO swap, etc.) assume blinky is
    flashed; this fixture keeps the bench in that canonical state across
    test invocations within this class."""
    yield
    try:
        CubeProgrammer(l476rg_ctx).flash_file(blinky_elf)
    except CubeProgrammerError:
        # Best-effort restore. If it fails the next test class will skip
        # cleanly via its own preconditions; don't mask the original
        # test's outcome with a teardown error.
        pass


@pytest.mark.hardware
class TestFlashPair:
    """F-008 (flash_pair) + F-009 (flash_signed_pair) partial-completion
    semantics on real hardware. Substrate doesn't care that GPIO_IOToggle
    and VCP-ECHO aren't a real boot/app pair — flash_pair is just two
    sequential flash_file calls. Each leg targets the L476 flash base
    (0x08000000) from its ELF header, so the second leg overwrites the
    first; that's expected and out-of-scope for substrate's contract."""

    def test_flash_pair_succeeds_with_two_elfs(
        self,
        l476rg_ctx,
        blinky_elf: Path,
        vcp_echo_elf: Path,
        restore_blinky_after,
    ) -> None:
        """Two valid ELFs flash sequentially.

        Asserts PairFlashResult.both_succeeded=True; both legs populated
        as FlashConfirmation with bytes_written matching each ELF's
        on-disk size (substrate uses path.stat().st_size, not parsed
        CLI output, per _file_size_or_zero)."""
        result = CubeProgrammer(l476rg_ctx).flash_pair(blinky_elf, vcp_echo_elf)
        assert isinstance(result, PairFlashResult)
        assert result.both_succeeded is True
        assert isinstance(result.bootloader, FlashConfirmation)
        assert result.bootloader.bytes_written == blinky_elf.stat().st_size
        assert result.bootloader.signed is False
        assert isinstance(result.application, FlashConfirmation)
        assert result.application.bytes_written == vcp_echo_elf.stat().st_size
        assert result.application.signed is False

    def test_flash_pair_captures_second_leg_failure(
        self,
        l476rg_ctx,
        blinky_elf: Path,
        tmp_path: Path,
        restore_blinky_after,
    ) -> None:
        """Second leg fails → partial-completion captured (no raise).

        Boot leg flashes blinky successfully; app leg points at a path
        that doesn't exist. Substrate's flash_pair catches the CLI-side
        CubeProgrammerError from the second invocation and returns
        PairFlashResult(bootloader=<populated>, application=None,
        both_succeeded=False). First-leg failure would have re-raised
        per the HIL contract — that's a separate test (covered by unit
        tests; not worth burning bench time on)."""
        nonexistent = tmp_path / "no-such-file.elf"
        assert not nonexistent.exists()
        result = CubeProgrammer(l476rg_ctx).flash_pair(blinky_elf, nonexistent)
        assert isinstance(result, PairFlashResult)
        assert result.both_succeeded is False
        assert isinstance(result.bootloader, FlashConfirmation)
        assert result.bootloader.bytes_written == blinky_elf.stat().st_size
        assert result.application is None


# ---------------------------------------------------------------------------
# Hardfault analyzer (DIAG-001 binary path) — backlog #3 + #7
# ---------------------------------------------------------------------------
#
# Substrate's analyze_hardfault uses mode=HOTPLUG (per commit ed4bb2b):
# preserves the chip's sticky fault registers (CFSR / HFSR / SHCSR /
# MMFAR / BFAR) across the connect — Normal mode would apply a software
# reset and wipe them before -hf reads.
#
# Builds the dedicated F-PROJ-NUCLEO-L476RG FAULTING sub-project
# directly (gitignored user-provides tree under Projects/NUCLEO-L476RG/
# Examples/PWR/FAULTING/). The fixture injects canonical UDF #0 main.c
# content idempotently, builds + flashes via the substrate, hard-resets,
# then re-flashes BLINKY in teardown so downstream tests find the
# canonical L476 firmware. BLINKY's source stays untouched throughout.


# The FAULTING constants + faulting_firmware_flashed fixture live in
# tests/conftest.py — shared with test_debug_hardware.py (A-014: the
# gdb-path fault test reads the same sticky-fault state).


@pytest.mark.hardware
class TestHardfault:
    """DIAG-001 binary-only path: analyze_hardfault decodes the chip's
    fault state via STM32_Programmer_CLI -hf. After flashing a UDF #0
    firmware + hard-resetting, the chip sits in HardFault_Handler and
    the SCB fault registers carry the fault snapshot. The substrate's
    HOTPLUG-mode connect preserves these registers across the attach
    — validating the bench-driven fix from commit <pending>."""

    def test_analyze_hardfault_detects_fault_after_runtime_fault(
        self, l476rg_ctx, faulting_firmware_flashed
    ) -> None:
        """Faulting firmware on chip → analyze_hardfault returns
        HardFaultDecode(hardfault_detected=True). substrate doesn't
        raise on detection (detected fault is a valid result)."""
        result = CubeProgrammer(l476rg_ctx).analyze_hardfault()
        assert isinstance(result, HardFaultDecode)
        assert result.source_used == "cubeprogrammer-hf"
        assert result.hardfault_detected is True, (
            f"expected fault detected on faulting firmware; got "
            f"hardfault_detected={result.hardfault_detected}; "
            f"decode: {result.fault_decode!r}"
        )


# ---------------------------------------------------------------------------
# Flash .bin without explicit address (F-005) + CP-001 router
# ---------------------------------------------------------------------------


_INFERRED_FLASH_BASE = "0x08000000"


@pytest.fixture
def blinky_bin(blinky_elf: Path, tmp_path: Path) -> Path:
    """Raw .bin derived from blinky.elf via arm-none-eabi-objcopy.

    Substrate's F-005 (flash_bin_no_address) accepts a .bin and infers
    the L476 flash base (0x08000000). Since the linker put blinky's
    sections at that base, the .bin written at 0x08000000 produces the
    same flashed image as flashing the .elf directly — the chip ends
    up running blinky regardless of which form went through the CLI.
    """
    import subprocess

    objcopy = "arm-none-eabi-objcopy"
    bin_path = tmp_path / "blinky.bin"
    result = subprocess.run(
        [objcopy, "-O", "binary", str(blinky_elf), str(bin_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not bin_path.is_file():
        pytest.skip(
            f"arm-none-eabi-objcopy unavailable or failed (exit "
            f"{result.returncode}); install GNU Tools for STM32. "
            f"stderr: {result.stderr[:300]!r}"
        )
    return bin_path


@pytest.mark.hardware
class TestFlashBinNoAddress:
    """F-005 (flash_bin_no_address) + CP-001 download_image router for
    .bin-without-address. The substrate infers the universal STM32 main-
    flash base (0x08000000), which matches the linker layout for the
    L476RG blinky — so this test exercises the inference path without
    needing a board with a non-standard flash layout."""

    def test_flash_bin_no_address_infers_flash_base(
        self,
        l476rg_ctx,
        blinky_bin: Path,
        restore_blinky_after,
    ) -> None:
        """flash_bin_no_address writes at the inferred 0x08000000 base.

        Asserts FlashConfirmation contract: ``address_inferred=True``,
        ``address`` is the inferred string, ``user_confirmed=False``
        (no on_confirm callback), ``bytes_written`` matches the .bin
        size on disk."""
        result = CubeProgrammer(l476rg_ctx).flash_bin_no_address(blinky_bin)
        assert isinstance(result, FlashConfirmation)
        assert result.address == _INFERRED_FLASH_BASE
        assert result.address_inferred is True
        assert result.user_confirmed is False
        assert result.bytes_written == blinky_bin.stat().st_size
        assert result.signed is False

    def test_download_image_routes_bin_no_address_to_flash_bin_no_address(
        self,
        l476rg_ctx,
        blinky_bin: Path,
        restore_blinky_after,
    ) -> None:
        """download_image (CP-001 router) routes ``.bin`` without an
        explicit address through flash_bin_no_address.

        Asserts the routed result carries ``route_used="flash_bin_no_address"``
        (extension-router contract) AND inherits the inferred-address
        fields from flash_bin_no_address (address=0x08000000,
        address_inferred=True). This proves the router's ``replace()``
        call preserves the inner method's contract."""
        result = CubeProgrammer(l476rg_ctx).download_image(blinky_bin)
        assert isinstance(result, FlashConfirmation)
        assert result.route_used == "flash_bin_no_address"
        assert result.address == _INFERRED_FLASH_BASE
        assert result.address_inferred is True
        assert result.bytes_written == blinky_bin.stat().st_size


# ---------------------------------------------------------------------------
# On-disk BLINKY artifact fixtures (F-003 hex/srec, F-004, F-007)
# ---------------------------------------------------------------------------
#
# CubeIDE's Debug/ output emits BLINKY.bin / BLINKY.hex / BLINKY.srec
# alongside BLINKY.elf. The on-disk artifacts skip the per-test
# arm-none-eabi-objcopy step that ``blinky_bin`` (above) does — and
# crucially give us a real HEX (Intel) and SREC (Motorola S-record)
# alongside the raw .bin for exercising the format-agnostic paths of
# ``flash_file``.


_BLINKY_DEBUG_DIR = (
    Path("Projects/NUCLEO-L476RG/Examples/GPIO/BLINKY/STM32CubeIDE/Debug")
)
_BLINKY_BIN_RELATIVE = _BLINKY_DEBUG_DIR / "BLINKY.bin"
_BLINKY_HEX_RELATIVE = _BLINKY_DEBUG_DIR / "BLINKY.hex"
_BLINKY_SREC_RELATIVE = _BLINKY_DEBUG_DIR / "BLINKY.srec"

# Bank-2 base on dual-bank L476 — separate from BLINKY at 0x08000000.
# Writing here erases the bank-2 sectors that overlap the payload;
# leaves bank-1 BLINKY intact so downstream tests still see firmware
# at the boot vector.
_BANK2_BASE = "0x08080000"


@pytest.fixture
def blinky_bin_artifact(l476rg_ctx) -> Path:
    """On-disk BLINKY.bin from CubeIDE's Debug/ output."""
    path = (l476rg_ctx.cwd / _BLINKY_BIN_RELATIVE).resolve()
    if not path.is_file():
        pytest.skip(
            f"BLINKY.bin not built at {path}; run the cubeide BLINKY build "
            "(or `pytest -m hardware tests/test_cubeide_hardware.py::TestBuild`) "
            "to emit it"
        )
    return path


@pytest.fixture
def blinky_hex_artifact(l476rg_ctx) -> Path:
    """On-disk BLINKY.hex (Intel HEX) from CubeIDE's Debug/ output."""
    path = (l476rg_ctx.cwd / _BLINKY_HEX_RELATIVE).resolve()
    if not path.is_file():
        pytest.skip(f"BLINKY.hex not built at {path}; build BLINKY first")
    return path


@pytest.fixture
def blinky_srec_artifact(l476rg_ctx) -> Path:
    """On-disk BLINKY.srec (Motorola S-record) from CubeIDE's Debug/ output."""
    path = (l476rg_ctx.cwd / _BLINKY_SREC_RELATIVE).resolve()
    if not path.is_file():
        pytest.skip(f"BLINKY.srec not built at {path}; build BLINKY first")
    return path


@pytest.mark.hardware
class TestFlashBinAtAddress:
    """F-004 — ``flash_bin(path, address)`` writes a raw .bin to an
    explicit address. Distinct from F-005's address-inference path; the
    substrate validates the .bin extension + address regex before invoking
    the CLI."""

    def test_flash_bin_at_flash_base_succeeds(
        self,
        l476rg_ctx,
        blinky_bin_artifact: Path,
        restore_blinky_after,
    ) -> None:
        """Explicit 0x08000000 write places BLINKY where the boot vector
        finds it — same end-state as F-001 (.elf) and F-005 (inferred-addr
        .bin), but exercises the explicit-address code path that neither
        of those touches."""
        result = CubeProgrammer(l476rg_ctx).flash_bin(
            blinky_bin_artifact, _INFERRED_FLASH_BASE
        )
        assert isinstance(result, FlashConfirmation)
        assert result.address == _INFERRED_FLASH_BASE
        assert result.address_inferred is False
        assert result.signed is False
        assert result.bytes_written == blinky_bin_artifact.stat().st_size


@pytest.mark.hardware
class TestFlashData:
    """F-007 — ``flash_data(path, address)`` writes a non-firmware payload
    to an explicit address. Same CLI shape as F-004 but semantically a
    data blob (font, SVD baseline, OTA payload, etc.). No extension check
    on the substrate side; the address is the load-bearing requirement."""

    def test_flash_data_to_non_default_region_succeeds(
        self,
        l476rg_ctx,
        blinky_bin_artifact: Path,
        restore_blinky_after,
    ) -> None:
        """Write BLINKY.bin as a *data* payload at bank-2 base
        (0x08080000) — separate from BLINKY at 0x08000000, so the test
        proves the substrate doesn't assume a flash-base address. Bank 1
        stays intact for downstream tests (the CLI only erases sectors
        overlapping the write range)."""
        result = CubeProgrammer(l476rg_ctx).flash_data(
            blinky_bin_artifact, _BANK2_BASE
        )
        assert isinstance(result, FlashConfirmation)
        assert result.address == _BANK2_BASE
        assert result.address_inferred is False
        assert result.signed is False
        assert result.bytes_written == blinky_bin_artifact.stat().st_size


@pytest.mark.hardware
class TestFlashFileFormats:
    """F-003 — ``flash_file(path)`` accepts ELF/HEX/BIN/SREC; address is
    inferred by the CLI from the file format when not provided. Existing
    ELF coverage runs via every BLINKY teardown re-flash; these add the
    HEX + SREC paths so the format-agnostic claim is proven on bench."""

    def test_flash_file_hex_uses_embedded_address(
        self,
        l476rg_ctx,
        blinky_hex_artifact: Path,
        restore_blinky_after,
    ) -> None:
        """Intel HEX carries address records; ``flash_file`` passes the
        path through ``_flash_invoke`` with no positional address arg,
        relying on the CLI's format-detect. Substrate populates
        ``FlashConfirmation.address=""`` to flag "CLI default" (not
        inferred by substrate, not explicit from caller)."""
        result = CubeProgrammer(l476rg_ctx).flash_file(blinky_hex_artifact)
        assert isinstance(result, FlashConfirmation)
        assert result.bytes_written == blinky_hex_artifact.stat().st_size
        assert result.address == ""
        assert result.address_inferred is False
        assert result.signed is False

    def test_flash_file_srec_uses_embedded_address(
        self,
        l476rg_ctx,
        blinky_srec_artifact: Path,
        restore_blinky_after,
    ) -> None:
        """Motorola S-record carries address records like HEX. Substrate's
        ``flash_file`` docstring lists ELF/HEX/BIN/AXF; this test verifies
        SREC works via the same code path (CLI accepts ``-d <file.srec>``
        natively). If it ever breaks, the F-003 contract needs widening
        or tightening — substrate captures, doesn't filter by extension."""
        result = CubeProgrammer(l476rg_ctx).flash_file(blinky_srec_artifact)
        assert isinstance(result, FlashConfirmation)
        assert result.bytes_written == blinky_srec_artifact.stat().st_size
        assert result.address == ""
        assert result.address_inferred is False
        assert result.signed is False


@pytest.mark.hardware
class TestFlashToBank:
    """F-011 — `flash_to_bank` writes a payload to an explicit flash bank
    and stamps the bank number onto the FlashConfirmation. The L476RG is
    dual-bank (1 MB = 2×512 KB; bank-2 base 0x08080000), so this exercises
    the real bank-2 write path. The substrate validates bank ∈ {1, 2} and
    the address regex, flashes via `-d <bin> <addr>`, then tags
    result.bank. Bank-1 (where the boot image lives) is left intact —
    only the bank-2 sectors overlapping the payload are erased."""

    def test_flash_to_bank2_succeeds_and_tags_bank(
        self,
        l476rg_ctx,
        blinky_bin_artifact: Path,
        restore_blinky_after,
    ) -> None:
        """flash_to_bank(BLINKY.bin, 2, 0x08080000) writes into bank 2 and
        returns FlashConfirmation(bank=2, address=0x08080000, signed=False,
        address_inferred=False) with bytes_written matching the .bin."""
        result = CubeProgrammer(l476rg_ctx).flash_to_bank(
            blinky_bin_artifact, 2, _BANK2_BASE
        )
        assert isinstance(result, FlashConfirmation)
        assert result.bank == 2
        assert result.address == _BANK2_BASE
        assert result.address_inferred is False
        assert result.signed is False
        assert result.bytes_written == blinky_bin_artifact.stat().st_size


@pytest.mark.hardware
class TestVerifyOptionBytes:
    """DIAG-018 — `verify_option_bytes` reads the option bytes and diffs
    them against a caller-supplied expected dict. Read-only (no OB write),
    so non-destructive. Pairs with D-009 (`read_option_bytes`): the L476RG
    bench is unprotected, so RDP reads 0xAA (170). The `expected` dict is
    passed directly to the method — no descriptor field involved."""

    def test_verify_matches_unprotected_rdp(self, l476rg_ctx) -> None:
        """A correct expected value yields zero diffs. Also exercises the
        method's int/hex normalisation: "0xAA" must match the observed
        int 170."""
        cp = CubeProgrammer(l476rg_ctx)
        diff = cp.verify_option_bytes({"RDP": 170})
        assert diff.diffs == [], f"expected no mismatch; got {diff.diffs}"
        assert diff.observed.get("RDP") == 170
        # Hex-string form normalises to the same int per the method contract.
        diff_hex = cp.verify_option_bytes({"RDP": "0xAA"})
        assert diff_hex.diffs == [], f"hex form should match; got {diff_hex.diffs}"

    def test_verify_reports_mismatch(self, l476rg_ctx) -> None:
        """A wrong expected value surfaces a single OptionByteDiffEntry
        carrying the real observed value + the expected one. 0xCC (204) is
        RDP level 2; the unprotected bench reads 0xAA, so it mismatches."""
        cp = CubeProgrammer(l476rg_ctx)
        diff = cp.verify_option_bytes({"RDP": 204})
        assert len(diff.diffs) == 1, f"expected one mismatch; got {diff.diffs}"
        entry = diff.diffs[0]
        assert entry.field == "RDP"
        assert entry.expected_value == 204
        assert entry.observed_value == 170
