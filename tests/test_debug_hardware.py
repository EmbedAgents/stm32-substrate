"""Debug hardware tests — non-destructive.

These run against an attached NUCLEO-L476RG with the GPIO_IOToggle
blinky ELF (built from
``tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY``). Excluded
from the default ``pytest`` run; invoke with ``pytest -m hardware``.

Build prerequisite: run ``stm32 build --project
tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/Projects/NUCLEO-L476RG/Examples/GPIO/GPIO_IOToggle/STM32CubeIDE``
before invoking these tests; ELF-dependent tests skip cleanly when
the artifact is missing.

What's covered:

  - TestStartSession: start_session(elf_path=..., halt=True) ->
    session.target_halted=True; SessionHandle carries valid pids +
    port.
  - TestRawReads: read_registers (Cortex-M4 core regs); read_memory
    at the L476 SRAM1 base (0x20000000).
  - TestSvdPath: read_peripheral("RCC") returns SVD-decoded CFGR with
    bitfields (SWS clock-source-status field present + 2-bit width) —
    SVD device resolved from the descriptor chip (board.mcu), NOT the
    ELF stem (the ELF is named after the app, "BLINKY", not the chip).
  - TestCorePeripherals: read_peripheral("NVIC") + read_peripheral("SCB")
    resolve via the Cortex-M **core** SVD fallback (those live in
    Cortex-M4.svd, not the device SVD); "SCB" resolves through the
    "Control" alias and carries CFSR/HFSR/VTOR — the registers DIAG-008
    (NVIC), DIAG-009 (VTOR) and DIAG-001/decode-hardfault (SCB) depend on.
  - TestSessionTeardown: clean session.close() leaves gdbserver
    process dead per stm32_substrate.platform.process_alive poll.

Device-name resolution note: these used to need an ELF copy renamed to
``STM32L476.elf`` (the old ``_device_name_hint`` = ELF stem). The
substrate now derives the device from ``ctx.project.board.mcu``, so the
normal app-named ELF resolves the SVD — the rename fixture is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stm32_substrate.debug import Debug, PeripheralDump, RegisterDump, RegisterValue
from stm32_substrate.debug.results import (
    Breakpoint,
    CallStack,
    DebugSnapshot,
    RunResult,
)
from stm32_substrate.platform import process_alive


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BLINKY_ELF_RELATIVE = Path(
    "Projects/NUCLEO-L476RG/Examples/GPIO/BLINKY/STM32CubeIDE/Debug/BLINKY.elf"
)


@pytest.fixture(scope="session")
def blinky_elf(l476rg_ctx) -> Path:
    """Built BLINKY ELF path; skips the test when the F-PROJ blinky
    hasn't been built yet (substrate doesn't build on demand inside
    hardware tests)."""
    elf = (l476rg_ctx.cwd / _BLINKY_ELF_RELATIVE).resolve()
    if not elf.is_file():
        pytest.skip(
            "BLINKY.elf not built; run `stm32 build --project "
            "tests/fixtures/projects/F-PROJ-NUCLEO-L476RG/Projects/"
            "NUCLEO-L476RG/Examples/GPIO/BLINKY/STM32CubeIDE` first"
        )
    return elf


# ---------------------------------------------------------------------------
# TestStartSession
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestStartSession:
    def test_start_with_elf_and_halt(self, l476rg_ctx, blinky_elf: Path) -> None:
        """start_session(halt=True) returns a context-managed session
        with the target halted + valid pids/port on the SessionHandle."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            assert session.target_halted is True
            handle = session.session_handle()
            assert handle.gdbserver_pid > 0
            assert handle.gdb_pid > 0
            assert handle.gdb_port > 0
            assert handle.elf_path == blinky_elf
            assert handle.target_halted is True

    def test_attach_running_does_not_halt(
        self, l476rg_ctx, blinky_elf: Path
    ) -> None:
        """DBG-003 — attach_running (start_session halt=False) attaches to
        a running target without stopping it. The session opens with valid
        gdbserver/gdb pids + port, but target_halted is False and the
        firmware keeps executing (elf_path is for symbol resolution only,
        not a reflash). Distinct code path from the halt=True DBG-001
        case above."""
        debug = Debug(l476rg_ctx)
        with debug.attach_running(elf_path=blinky_elf) as session:
            assert session.target_halted is False
            handle = session.session_handle()
            assert handle.gdbserver_pid > 0
            assert handle.gdb_pid > 0
            assert handle.gdb_port > 0
            assert handle.target_halted is False


# ---------------------------------------------------------------------------
# TestRawReads
# ---------------------------------------------------------------------------


# L476RG memory map (RM0351 Rev 9):
#   Flash bank 1: 0x0800_0000 .. 0x0808_0000 (512 KB)
#   Flash bank 2: 0x0808_0000 .. 0x0810_0000 (512 KB)
#   SRAM1:        0x2000_0000 .. 0x2001_8000 (96 KB)
#   SRAM2:        0x1000_0000 .. 0x1000_8000 (32 KB, also 0x2001_8000)
_FLASH_BASE = 0x08000000
_FLASH_END = 0x08100000  # 1 MB total for L476RG
_SRAM1_BASE = "0x20000000"


@pytest.mark.hardware
class TestRawReads:
    def test_read_registers_cortex_m4(self, l476rg_ctx, blinky_elf: Path) -> None:
        """Cortex-M4 core register dump carries r0..r12, sp, lr, pc,
        xpsr at minimum. PC after halt should land in the L476 flash
        range (target is running blinky code from flash)."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            dump = session.read_registers()
            assert isinstance(dump, RegisterDump)
            required = {"r0", "r1", "r2", "r3", "sp", "lr", "pc", "xpsr"}
            missing = required - set(dump.values.keys())
            assert not missing, f"core regs missing from dump: {missing}"
            pc = dump.values["pc"]
            assert _FLASH_BASE <= pc < _FLASH_END, (
                f"pc=0x{pc:08x} not in L476 flash range "
                f"[0x{_FLASH_BASE:08x}, 0x{_FLASH_END:08x})"
            )

    def test_read_sram_64_bytes(self, l476rg_ctx, blinky_elf: Path) -> None:
        """read_memory at SRAM1 base returns the requested byte count
        with the address echoed back. We don't assert on content -
        SRAM holds whatever the blinky's runtime has stashed."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            result = session.read_memory(_SRAM1_BASE, 64)
            assert result.address == _SRAM1_BASE
            assert result.size == 64
            assert result.bytes_read == 64
            assert result.suspicious_unmapped is False


# ---------------------------------------------------------------------------
# TestSvdPath
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestSvdPath:
    def test_read_rcc_cfgr_svd_decoded(self, l476rg_ctx, blinky_elf: Path) -> None:
        """End-to-end SVD path: read_peripheral("RCC") returns a
        PeripheralDump whose CFGR register is decoded into fields per
        the L476 SVD. SWS (System clock switch Status) is a 2-bit
        field at offset 2 that reflects the active clock source -
        always present in any L4 SVD.

        Uses the **normal app-named** ELF (``BLINKY.elf``) — the SVD
        device is resolved from the descriptor chip (board.mcu =
        STM32L476RGTx), not the ELF stem. A regression here means the
        device-name resolution fell back to the (non-resolving) stem."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            dump = session.read_peripheral("RCC")
            assert isinstance(dump, PeripheralDump)
            assert dump.peripheral == "RCC"
            assert dump.suspicious_unmapped is False
            assert "CFGR" in dump.registers, (
                f"CFGR missing from RCC dump; got {list(dump.registers)}"
            )
            cfgr = dump.registers["CFGR"]
            assert isinstance(cfgr, RegisterValue)
            assert "SWS" in cfgr.fields, (
                f"SWS field missing from CFGR; got {list(cfgr.fields)}"
            )
            assert cfgr.fields["SWS"].bit_width == 2


# ---------------------------------------------------------------------------
# TestCorePeripherals — core-SVD fallback (NVIC / SCB) on real silicon
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestCorePeripherals:
    """Cortex-M core peripherals (NVIC / SCB) live in the **core** SVD
    (Cortex-M4.svd), not the device SVD — read_peripheral falls back to
    it via the device-family→core map. SCB additionally resolves through
    the "Control" alias (ST's core SVD names the System Control Block
    "Control"). These were 404-ing before the fix, which also broke
    decode-hardfault (DIAG-001) since it reads SCB."""

    def test_read_nvic_via_core_svd(self, l476rg_ctx, blinky_elf: Path) -> None:
        """DIAG-008 building block: read_peripheral("NVIC") resolves from
        the core SVD and carries the interrupt-set-enable registers."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            dump = session.read_peripheral("NVIC")
            assert isinstance(dump, PeripheralDump)
            assert dump.peripheral == "NVIC"
            assert dump.registers, "NVIC dump should carry registers"
            assert any("ISER" in reg for reg in dump.registers), (
                f"no ISER (interrupt-set-enable) register in NVIC dump; "
                f"got {sorted(dump.registers)[:8]}"
            )

    def test_read_scb_via_core_svd_alias(self, l476rg_ctx, blinky_elf: Path) -> None:
        """DIAG-009 + decode-hardfault building block: read_peripheral("SCB")
        resolves through the core-SVD "Control" alias and carries the fault
        registers (CFSR with decoded fault bitfields) + VTOR (vector table
        base). Labeled with the requested name "SCB", not the SVD's
        "Control"."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            dump = session.read_peripheral("SCB")
            assert isinstance(dump, PeripheralDump)
            assert dump.peripheral == "SCB"
            for reg in ("CFSR", "HFSR", "VTOR"):
                assert reg in dump.registers, (
                    f"{reg} missing from SCB dump; got {sorted(dump.registers)}"
                )
            cfsr = dump.registers["CFSR"]
            assert isinstance(cfsr, RegisterValue)
            # CFSR carries the MemManage/Bus/Usage fault-status bitfields;
            # IMPRECISERR (imprecise BusFault) is always defined on Cortex-M4.
            assert "IMPRECISERR" in cfsr.fields, (
                f"CFSR not field-decoded; got {sorted(cfsr.fields)[:8]}"
            )


# ---------------------------------------------------------------------------
# TestDecodeHardfault — DIAG-001 gdb path on real silicon
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestDecodeHardfault:
    """DIAG-001 (gdb path) end-to-end on silicon. ``decode-hardfault``
    composes the RAW fault bundle — SCB (CFSR/HFSR via the core-SVD
    "Control" alias) + CPU registers + callstack — as a ``DebugSnapshot``;
    Claude classifies the fault, the substrate encodes no rule (ADR-004).
    On a healthy (non-faulted) target the raw CFSR/HFSR fault registers
    read zero and the live PC is a valid flash address. Before the
    core-SVD fix this raised ``SVDLookupError`` (SCB not in the device
    SVD), so shipped DIAG-001 was broken on hardware."""

    def test_decode_hardfault_no_fault_on_healthy_target(
        self, l476rg_ctx, blinky_elf: Path
    ) -> None:
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            snap = session.snapshot(include_peripherals=["SCB"])
        # Substrate emits the raw bundle — no fault verdict. Verify the raw
        # SCB fault registers are clean on a healthy target.
        scb = next(d for d in snap.peripheral_dumps if d.peripheral == "SCB")
        cfsr = scb.registers["CFSR"].raw_value
        hfsr = scb.registers["HFSR"].raw_value
        assert cfsr == 0, f"healthy target has non-zero CFSR=0x{cfsr:08x}"
        assert hfsr == 0, f"healthy target has non-zero HFSR=0x{hfsr:08x}"
        # The live PC is captured even with no fault — a flash address.
        pc = snap.registers.values["pc"]
        assert _FLASH_BASE <= pc < _FLASH_END, (
            f"snapshot pc=0x{pc:08x} not in L476 flash range"
        )


# ---------------------------------------------------------------------------
# TestSessionTeardown
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestBreakpoints — DBG-005 set/run/remove cycle (backlog #5)
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestBreakpoints:
    """DBG-005 hardware coverage: set_breakpoint + remove_breakpoint
    round-trip on a real gdb session.

    Uses the blinky ELF's ``main`` symbol — the universal entry point
    after the startup vectors run. Substrate resolves the symbol to a
    flash address via ``-break-insert main`` and returns a populated
    Breakpoint dataclass.

    Note: a separate run_until_breakpoint exercise was attempted on
    this bench (set bp at main → -exec-continue → wait for *stopped)
    but the L476 + ST-LINK gdbserver combo emits *stopped with no
    ``reason`` field after the -exec-continue, with PC staying at
    Reset_Handler instead of walking to main. That's a probe/
    gdbserver interaction quirk, not a substrate parser bug — the
    substrate's wait_for_stopped + reason_map handle the no-reason
    case by returning halt_reason='unknown'. Exercising the full
    run-to-breakpoint cycle reliably would need probe-specific
    workarounds (e.g. monitor reset followed by an explicit step
    before -exec-continue); deferred until a clearer reproducer
    surfaces.

    Single session for both sub-checks: starting multiple gdb sessions
    back-to-back on the same probe wedged the ST-LINK firmware during
    bench-driven iteration; consolidating reduces probe state pressure."""

    def test_breakpoint_set_then_remove_round_trip(
        self, l476rg_ctx, blinky_elf: Path
    ) -> None:
        """Validates the deterministic half of DBG-005: symbol →
        Breakpoint dataclass → -break-delete → session still functional.

        The substrate's set_breakpoint sends ``-break-insert main`` and
        parses the returned bkpt record into a Breakpoint with number,
        location, and resolved address. remove_breakpoint sends
        ``-break-delete <number>`` and must not raise. After delete,
        the session should still respond to read_registers — proves
        the gdb subprocess wasn't left in a half-state."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            # --- (a) set_breakpoint resolves symbol → flash address ---
            bp = session.set_breakpoint("main")
            assert isinstance(bp, Breakpoint)
            assert bp.number > 0
            assert bp.location == "main"
            assert bp.address is not None
            main_addr = int(bp.address, 16)
            assert _FLASH_BASE <= main_addr < _FLASH_END, (
                f"main resolved to {bp.address}, outside L476 flash range "
                f"[0x{_FLASH_BASE:08x}, 0x{_FLASH_END:08x})"
            )

            # --- (b) remove_breakpoint round-trips cleanly ---
            session.remove_breakpoint(bp)  # should not raise

            # --- (c) gdb session still functional after delete ---
            dump = session.read_registers()
            assert "pc" in dump.values


# ---------------------------------------------------------------------------
# TestSnapshot — DIAG-021 composition (backlog #4)
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestSnapshot:
    """DIAG-021 composition test. snapshot() pulls together raw reads:
    registers + callstack + disasm-around-pc + peripheral dumps. Each
    of those raw reads is covered elsewhere; this test validates the
    composition shape + that all the pieces co-exist in one call."""

    def test_snapshot_carries_registers_callstack_disasm_and_peripherals(
        self, l476rg_ctx, blinky_elf: Path
    ) -> None:
        """SVD device resolved from the descriptor chip (board.mcu), so
        the normal app-named ELF works. Default include_peripherals=None
        pulls SCB; explicit ["RCC"] used here to validate the bigger
        peripheral path (RCC has more registers + fields than SCB).
        Asserts the DebugSnapshot contract: all composition fields
        populated, register PC matches callstack top frame, disasm
        contains at least the PC address."""
        debug = Debug(l476rg_ctx)
        with debug.start_session(elf_path=blinky_elf, halt=True) as session:
            snap = session.snapshot(include_peripherals=["RCC"])
            assert isinstance(snap, DebugSnapshot)
            # --- registers ---
            assert isinstance(snap.registers, RegisterDump)
            assert "pc" in snap.registers.values
            pc = snap.registers.values["pc"]
            assert _FLASH_BASE <= pc < _FLASH_END, (
                f"snapshot pc=0x{pc:08x} not in L476 flash range"
            )
            # --- callstack ---
            assert isinstance(snap.callstack, CallStack)
            assert len(snap.callstack.frames) >= 1, (
                "expected at least one frame in callstack"
            )
            # Top frame's PC should match the register dump's PC.
            top = snap.callstack.frames[0]
            top_pc = int(top.pc, 16) if top.pc.startswith("0x") else int(top.pc)
            assert abs(top_pc - pc) <= 8, (
                f"top-frame pc=0x{top_pc:08x} differs from register pc=0x{pc:08x}"
            )
            # --- disasm ---
            assert isinstance(snap.disasm_around_pc, str)
            assert len(snap.disasm_around_pc) > 0, "disasm should not be empty"
            # --- peripheral dumps ---
            assert len(snap.peripheral_dumps) == 1
            rcc = snap.peripheral_dumps[0]
            assert isinstance(rcc, PeripheralDump)
            assert rcc.peripheral == "RCC"
            assert rcc.registers, "RCC dump should have registers"
            # --- session handle attached ---
            assert snap.session.gdbserver_pid > 0
            assert snap.session.gdb_port > 0


@pytest.mark.hardware
class TestSessionTeardown:
    def test_close_kills_gdbserver(self, l476rg_ctx, blinky_elf: Path) -> None:
        """Explicit session.close() (no context-manager) terminates the
        gdbserver subprocess - verified via stm32_substrate.platform.
        process_alive on the captured pid. Mirrors the gdb-MI cleanup
        contract in src/stm32_substrate/debug/session.py:_close path."""
        debug = Debug(l476rg_ctx)
        session = debug.start_session(elf_path=blinky_elf, halt=True)
        gdbserver_pid = session.gdbserver_pid
        try:
            assert process_alive(gdbserver_pid), (
                f"gdbserver pid={gdbserver_pid} should be alive after start"
            )
        finally:
            session.close()
        assert not process_alive(gdbserver_pid), (
            f"gdbserver pid={gdbserver_pid} should be dead after close"
        )
