"""Board bring-up recipe suite — the substrate's core recipe surface, run
against *whatever board is attached* + its project.

Onboarding a new board is one project + one command:

  1. Author a ``stm32-project.jsonc`` for the board (``board.mcu`` +
     ``firmware.board`` matching the ST-LINK probe's ``board_name``, a
     buildable ``build`` section, and ``debug.elf_path``). Put it in a
     project directory.
  2. Attach the board.
  3. Run:
       STM32_BOARD_PROJECT=<that dir> pytest -m hardware tests/test_board_recipe_suite.py -v

The suite exercises the recipe surface the eval scenarios route to —
directly against the substrate (no Claude, no eval layer) — plus a
foundation tier of cheap, non-destructive bring-up checks (~28 total):

  - discovery: connect (banner) · cores · ping-swd · board-name ·
    memory-layout (flash size) · **svd-for-attached** · read-option-bytes
  - build: headless CubeIDE build of the project
  - prog: flash (ELF) · reset · halt/resume round-trip
  - debug reads: read-peripheral × {RCC GPIOA USART2 SPI1 NVIC SCB DBGMCU
    DMA1 IWDG WWDG} · read-registers · read-memory · callstack · snapshot
    · breakpoint round-trip · decode-hardfault (gdb path)
  - vcp: port discovery

Deliberately NOT here (run-every-board smoke pass): destructive ops
(erase / OB-write / RDP — they live in the opt-in `hardware_destructive`
suite); VCP send/tail (needs board-specific echo firmware); format-
specific flash (bin/hex/srec/pair/bank — needs per-board artifacts /
a dual-bank part; ELF flash is the universal check).

Board portability:

  - **`svd-for-attached` is the foundation check**: if no SVD resolves
    for the attached chip, peripheral decode can't work — this FAILS
    loudly rather than letting the peripheral reads silently skip.
  - A peripheral merely *absent* from this board's SVD (a part without
    WWDG, a different USART) **skips** (`peripheral-not-in-svd`); but a
    `read-peripheral` that can't resolve the SVD at all (`svd-not-found`
    — the device-name-resolution bug class) **fails**.
  - Core peripherals (RCC/GPIOA/SCB/NVIC/DBGMCU) are universal; the rest
    are common but optional.
  - Format-specific flash (bin/hex/srec) + flash-pair + flash-to-bank are
    board-fixture-specific (they need per-board artifacts / a dual-bank
    part) and live in ``test_cubeprogrammer_hardware.py``; this suite
    flashes the project's built ELF as the universal flash check.

Probe discipline: the prog/build/flash checks run first (they own the
SWD probe); then a *single* session-scoped debug session opens for all
the reads (one gdbserver spawn for the whole suite, not one per read).
VCP discovery uses pyserial (the CDC port), not the SWD probe.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from embedagents.stm32.errors import GDBError, SVDLookupError


# Common-STM32 peripheral set the 21-scenario recipe surface reads. Core
# ones (RCC/GPIOA/SCB/NVIC/DBGMCU) are universal; the rest are common but
# may be absent on a given part → that read skips, not fails.
RECIPE_PERIPHERALS = (
    "RCC", "GPIOA", "USART2", "SPI1", "NVIC",
    "SCB", "DBGMCU", "DMA1", "IWDG", "WWDG",
)


# ---------------------------------------------------------------------------
# Fixtures — resolve "the board under test" from STM32_BOARD_PROJECT
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def board_ctx(probe_lock: object):
    """SubstrateContext for the project named by ``STM32_BOARD_PROJECT``,
    pinned to the attached probe whose ``board_name`` matches the
    descriptor. Skips the whole suite when the env var is unset, the
    context won't load, or the declared board isn't attached."""
    proj = os.environ.get("STM32_BOARD_PROJECT")
    if not proj:
        pytest.skip(
            "set STM32_BOARD_PROJECT=<dir with stm32-project.jsonc> to run "
            "the board recipe suite (one project per board)"
        )

    from embedagents.stm32.context import SubstrateContext
    from embedagents.stm32.errors import ConfigurationError

    try:
        ctx = SubstrateContext.from_environment(project_path=Path(proj))
    except ConfigurationError as exc:
        pytest.skip(
            f"board recipe suite: context not loadable from {proj!r} — "
            f"{exc.message} (hint: {exc.hint or '(none)'})"
        )

    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    firmware = getattr(ctx.project, "firmware", None)
    board = getattr(ctx.project, "board", None)
    board_name = (
        getattr(firmware, "board", None)
        or getattr(board, "name", None)
    )

    try:
        probes = CubeProgrammer(ctx).list_probes()
    except Exception as exc:  # pragma: no cover - defensive
        pytest.skip(f"board recipe suite: list_probes() raised — {exc}")

    matching = (
        [p for p in probes if p.board_name == board_name]
        if board_name else list(probes)
    )
    if not matching:
        detected = [p.board_name for p in probes]
        pytest.skip(
            f"declared board {board_name!r} not attached "
            f"(detected: {detected or 'none'})"
        )
    object.__setattr__(ctx, "default_probe_sn", matching[0].stlink_sn)
    return ctx


@pytest.fixture(scope="session")
def board_build(board_ctx):
    """Build the project once (if it has a ``build`` section). ``None``
    when the descriptor is prebuilt-only (no build section)."""
    if getattr(board_ctx.project, "build", None) is None:
        return None
    from embedagents.stm32.cubeide import CubeIDE

    return CubeIDE(board_ctx).build()


@pytest.fixture(scope="session")
def board_elf(board_ctx, board_build) -> Path:
    """The artifact to flash + debug: the freshly-built ELF, else the
    descriptor's ``debug.elf_path``. Skips when neither is available."""
    if board_build is not None and board_build.success and board_build.artifact_path:
        return Path(board_build.artifact_path)
    dbg = getattr(board_ctx.project, "debug", None)
    elf_rel = getattr(dbg, "elf_path", None) if dbg else None
    if elf_rel:
        elf = (board_ctx.cwd / elf_rel).resolve()
        if elf.is_file():
            return elf
    pytest.skip(
        "no built artifact (build failed or no build section) and no "
        "usable debug.elf_path in the descriptor"
    )


@pytest.fixture(scope="session")
def board_session(board_ctx, board_elf):
    """One shared debug session for every read recipe — a single gdbserver
    spawn for the suite. Skips the read tests cleanly if the session can't
    start (e.g. ST-LINK firmware too old)."""
    from embedagents.stm32.debug import Debug
    from embedagents.stm32.errors import GDBError

    debug = Debug(board_ctx)
    try:
        session = debug.start_session(elf_path=board_elf, halt=True)
    except GDBError as exc:
        pytest.skip(f"debug session would not start: {exc.message} (hint: {exc.hint})")
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The recipe suite
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestBoardRecipeSuite:
    """The recipe surface + a foundation tier (~28 checks) against the
    attached board. Run with ``STM32_BOARD_PROJECT=<dir> pytest -m hardware
    tests/test_board_recipe_suite.py -v``."""

    # ---- prog (own the SWD probe; run before the debug session) --------

    def test_prog_connect(self, board_ctx) -> None:
        """D-001 — connect returns a banner with device id / CPU / flash."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        banner = CubeProgrammer(board_ctx).connect()
        assert banner.device_id, "connect banner missing device_id"
        assert banner.device_cpu, "connect banner missing device_cpu"
        assert banner.flash_size_kb and banner.flash_size_kb > 0

    def test_prog_cores(self, board_ctx) -> None:
        """D-007 — cores() reports the primary Cortex-M core."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        result = CubeProgrammer(board_ctx).cores()
        assert result.primary_core, "cores() returned no primary_core"
        assert result.primary_core.startswith("Cortex-M")

    def test_ping_swd(self, board_ctx) -> None:
        """D-006 — the SWD interface responds."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        result = CubeProgrammer(board_ctx).ping_swd()
        assert result.value is True, f"SWD ping failed: {result.reason}"

    def test_board_name(self, board_ctx) -> None:
        """D-003 — the probe→board name resolves."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        assert CubeProgrammer(board_ctx).board_name(), "board_name() returned empty"

    def test_memory_layout(self, board_ctx) -> None:
        """D-004 — the device DB recognises the part (flash size > 0)."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        layout = CubeProgrammer(board_ctx).memory_layout()
        assert layout.flash_size_kb and layout.flash_size_kb > 0

    def test_svd_for_attached(self, board_ctx) -> None:
        """D-008 — **foundation check**: an SVD resolves for the attached
        chip. A miss here means peripheral decode can't work, so this
        FAILS loudly (rather than letting the read-peripheral tests skip
        silently) — it guards the device-name-resolution bug class."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        svd = CubeProgrammer(board_ctx).svd_for_attached()
        assert svd.svd_path is not None, (
            f"no SVD resolved for the attached chip (banner: {svd.device_name!r}) "
            f"— peripheral decode will not work on this board"
        )
        assert svd.svd_path.is_file()

    def test_read_option_bytes(self, board_ctx) -> None:
        """D-009 — non-destructive option-byte read."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        ob = CubeProgrammer(board_ctx).read_option_bytes()
        assert ob.observed, "read_option_bytes() returned no option bytes"

    def test_build(self, board_build) -> None:
        """B-001 — headless CubeIDE build of the project."""
        if board_build is None:
            pytest.skip("descriptor has no build section; using prebuilt debug.elf_path")
        assert board_build.success, (
            f"build failed (exit {board_build.exit_code}); see {board_build.log_path}"
        )
        assert board_build.artifact_path and Path(board_build.artifact_path).is_file()

    def test_prog_flash_elf(self, board_ctx, board_elf: Path) -> None:
        """F-003 — flash the built ELF via CubeProgrammer."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        result = CubeProgrammer(board_ctx).flash_file(board_elf)
        # flash_file returns a FlashConfirmation on success (failures raise).
        assert result.bytes_written > 0, f"flash_file({board_elf.name}) wrote 0 bytes"

    def test_reset(self, board_ctx) -> None:
        """F-016 — software reset issues cleanly."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        result = CubeProgrammer(board_ctx).reset()
        assert result.reset_issued is True

    def test_halt_resume_round_trip(self, board_ctx) -> None:
        """F-017/F-018 — halt then resume both confirm (basic run-control)."""
        from embedagents.stm32.cubeprogrammer import CubeProgrammer

        client = CubeProgrammer(board_ctx)
        halt_result = client.halt()
        try:
            assert halt_result.data.get("halted") is True
        finally:
            resume_result = client.resume()
            assert resume_result.data.get("running") is True

    # ---- debug reads (shared session) ----------------------------------

    @pytest.mark.parametrize("peripheral", RECIPE_PERIPHERALS)
    def test_read_peripheral(self, board_session, peripheral: str) -> None:
        """DBG-007 + DIAG-002…017 surface — SVD-decoded peripheral dump.
        Skips when the peripheral isn't in this board's SVD."""
        try:
            dump = board_session.read_peripheral(peripheral)
        except SVDLookupError as exc:
            # Board genuinely lacks this peripheral → skip. But an SVD that
            # won't resolve at all (svd-not-found — the device-name bug
            # class) is a real failure, not a portability skip.
            if exc.gdb_marker == "peripheral-not-in-svd":
                pytest.skip(f"{peripheral} not in this board's SVD: {exc.message}")
            raise
        assert dump.peripheral == peripheral
        assert dump.registers, f"{peripheral} dump carried no registers"

    def test_read_registers(self, board_session) -> None:
        """DBG-006 — CPU register dump from the halted target."""
        dump = board_session.read_registers()
        assert "pc" in dump.values, "register dump missing pc"

    def test_read_memory(self, board_ctx, board_session) -> None:
        """Raw memory read at the flash base (descriptor firmware.flash_address)."""
        fw = getattr(board_ctx.project, "firmware", None)
        flash_base = getattr(fw, "flash_address", None) or "0x08000000"
        result = board_session.read_memory(flash_base, 64)
        assert result.bytes_read == 64, f"expected 64 bytes, got {result.bytes_read}"
        assert result.hex_dump, "memory read produced no hex dump"

    def test_callstack(self, board_session) -> None:
        """Callstack of the halted target — at least one frame."""
        cs = board_session.callstack()
        assert len(cs.frames) >= 1, "callstack had no frames"

    def test_snapshot(self, board_session) -> None:
        """DIAG-021 — composite snapshot (registers + callstack + disasm)."""
        snap = board_session.snapshot()
        assert "pc" in snap.registers.values
        assert len(snap.callstack.frames) >= 1

    def test_breakpoint_round_trip(self, board_session) -> None:
        """DBG-004/005 building block — set a breakpoint at `main`, confirm
        it resolves to an address, remove it cleanly. Exercises gdb
        breakpoint capability on this board (distinct from the raw reads).
        Skips if the firmware has no `main` symbol."""
        try:
            bp = board_session.set_breakpoint("main")
        except GDBError as exc:
            pytest.skip(f"could not set breakpoint at main (no symbol?): {exc.message}")
        assert bp.number > 0
        assert bp.address is not None, "breakpoint at main resolved no address"
        board_session.remove_breakpoint(bp)  # must not raise

    def test_decode_hardfault(self, board_session) -> None:
        """DIAG-001 gdb path — compose the raw SCB + registers + callstack
        bundle (a ``DebugSnapshot``); Claude classifies, substrate encodes
        no rule (ADR-004). Healthy target → raw CFSR/HFSR read zero.
        (Exercises the core-SVD ``SCB``→``Control`` path.)"""
        try:
            snap = board_session.snapshot(include_peripherals=["SCB"])
        except SVDLookupError as exc:
            pytest.skip(f"core SVD unavailable for this board: {exc.message}")
        scb = next(
            (d for d in snap.peripheral_dumps if d.peripheral == "SCB"), None
        )
        assert scb is not None, "snapshot did not carry the SCB dump"
        assert scb.registers["CFSR"].raw_value == 0, "healthy target has CFSR bits set"
        assert scb.registers["HFSR"].raw_value == 0, "healthy target has HFSR bits set"

    # ---- vcp (CDC port, not the SWD probe) -----------------------------

    def test_vcp_discovery(self, board_ctx) -> None:
        """VCP-001 input — discover the board's ST-LINK CDC (VCP) port."""
        from embedagents.stm32.vcp import VCP

        ports = VCP(board_ctx).discover_vcp_ports()
        assert isinstance(ports, (list, tuple)), "discover_vcp_ports() returned non-sequence"
        # Presence isn't guaranteed (some boards/probes expose no CDC) —
        # the recipe must at least run + return an enumerable result.
