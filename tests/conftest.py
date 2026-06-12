"""Top-level pytest fixtures.

Hardware tests (per ``@pytest.mark.hardware`` / ``smoke_with_probe`` /
``hardware_destructive``) reach an attached ST-LINK probe and require:

  - Vendor CLIs resolved (``STM32_PROGRAMMER_CLI`` env or PATH).
  - The expected board attached, per ``manifest.jsonc`` capabilities.

Tests opt in via fixtures here. The default ``pytest`` invocation
(see ``pyproject.toml`` ``addopts``) excludes them; run them with
``pytest -m hardware`` or ``pytest -m smoke_with_probe``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_LOCK_PATH = _REPO_ROOT / ".stm32-substrate" / "probe.lock"
_L476RG_PROJECT_PATH = _REPO_ROOT / "tests" / "fixtures" / "projects" / "F-PROJ-NUCLEO-L476RG"


# ---------------------------------------------------------------------------
# Probe-lock (session-scoped — serialises SWD access across pytest runs)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def probe_lock() -> object:
    """Session-scoped exclusive lock on the singleton ST-LINK probe.

    Implemented via ``embedagents.stm32.platform.acquire_exclusive_lock`` —
    cross-platform per ADR-007 (fcntl on Linux, msvcrt on Windows). Raises
    ``BlockingIOError`` immediately when another pytest session is already
    holding the probe (no long waits per HIL M-019). Two concurrent
    ``pytest -m hardware`` invocations are a configuration error — wait
    for the other run to finish.

    Released when the test session ends.
    """
    from embedagents.stm32.platform import acquire_exclusive_lock

    cm = acquire_exclusive_lock(_PROBE_LOCK_PATH)
    cm.__enter__()
    yield cm
    cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Hardware context — vendor CLIs resolved, ready to invoke
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def hardware_ctx(probe_lock: object) -> "SubstrateContext":
    """SubstrateContext loaded against the L476RG F-PROJ descriptor.

    Skips the whole hardware suite if the vendor CLIs are not resolvable
    on this host (env var / candidates / PATH all fail). Depends on the
    session-scoped ``probe_lock`` so a concurrent pytest run can't snipe
    the probe mid-test.
    """
    from embedagents.stm32.context import SubstrateContext
    from embedagents.stm32.errors import ConfigurationError

    try:
        return SubstrateContext.from_environment(project_path=_L476RG_PROJECT_PATH)
    except ConfigurationError as exc:
        pytest.skip(
            f"hardware-suite skipped: substrate context not loadable on this "
            f"host — {exc.message} (hint: {exc.hint or '(none)'})"
        )


# ---------------------------------------------------------------------------
# Attached-board enumeration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def attached_boards(hardware_ctx: "SubstrateContext") -> frozenset[str]:
    """Return the set of attached board names per CubeProgrammer probe-list.

    Honors the ``STM32_SUBSTRATE_HARDWARE_BOARDS`` env-var override (CI /
    manual override) before calling ``list_probes()`` — useful when the
    operator knows what's attached but the probe enumeration is flaky.

    Empty set => skip every ``@pytest.mark.hardware`` test.
    """
    import os

    override = os.environ.get("STM32_SUBSTRATE_HARDWARE_BOARDS")
    if override:
        boards = {b.strip() for b in override.split(",") if b.strip()}
        return frozenset(boards)

    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    try:
        client = CubeProgrammer(hardware_ctx)
        probes = client.list_probes()
    except Exception as exc:  # pragma: no cover - defensive
        pytest.skip(f"hardware-suite skipped: list_probes() raised — {exc}")
    boards = {p.board_name for p in probes if p.board_name is not None}
    return frozenset(boards)


@pytest.fixture(scope="session")
def l476rg_ctx(
    hardware_ctx: "SubstrateContext",
    attached_boards: frozenset[str],
) -> "SubstrateContext":
    """SubstrateContext pinned to the attached NUCLEO-L476RG.

    Skips the requesting test when no L476RG is attached. Resolves the
    probe's SN via ``list_probes()`` and latches ``default_probe_sn`` so
    multi-probe disambiguation is unambiguous for downstream test calls.
    """
    if "NUCLEO-L476RG" not in attached_boards:
        pytest.skip(
            f"NUCLEO-L476RG not attached (detected: {sorted(attached_boards) or 'none'})"
        )

    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    client = CubeProgrammer(hardware_ctx)
    probes = client.list_probes()
    matching = [p for p in probes if p.board_name == "NUCLEO-L476RG"]
    if len(matching) != 1:
        pytest.skip(
            f"expected exactly one NUCLEO-L476RG probe, found {len(matching)} "
            f"({[p.stlink_sn for p in matching]}); set "
            "STM32_SUBSTRATE_HARDWARE_BOARDS to disambiguate."
        )
    sn = matching[0].stlink_sn
    object.__setattr__(hardware_ctx, "default_probe_sn", sn)
    return hardware_ctx


@pytest.fixture(scope="session")
def smoke_ctx() -> "SubstrateContext":
    """Probe-independent SubstrateContext for vendor-CLI smoke tests.

    Smoke tests just need the vendor CLIs resolvable on this host; they
    don't touch the SWD probe and don't depend on any attached board.
    Skip the suite when ``SubstrateContext.from_environment()`` raises
    (e.g. unsupported platform); per-test skips handle missing
    individual tools.
    """
    from embedagents.stm32.context import SubstrateContext
    from embedagents.stm32.errors import ConfigurationError

    try:
        return SubstrateContext.from_environment()
    except ConfigurationError as exc:
        pytest.skip(
            f"smoke-suite skipped: substrate context not loadable — "
            f"{exc.message} (hint: {exc.hint or '(none)'})"
        )


@pytest.fixture(scope="session")
def f401re_ctx(
    hardware_ctx: "SubstrateContext",
) -> "SubstrateContext":
    """SubstrateContext pinned to an attached NUCLEO-F401RE.

    The F401RE board's onboard ST-Link/V2-1 with older firmware
    (e.g. V2J28M17) reports an EMPTY ``Board Name`` field in
    ``STM32_Programmer_CLI -l`` output — unlike the newer L476/H7S78/N6
    boards whose ST-Links report ``NUCLEO-L476RG`` / ``STM32H7S78-DK``
    / ``STM32N6570-DK`` directly. Board-name-matching in the
    ``attached_boards`` set therefore can't see the F401RE.

    Fallback identification: connect to the lone probe and read the
    device_id from the banner (0x433 = STM32F401xD/xE; the F401RE chip
    on this board is the xE variant per RM0368) to confirm. Skips when
    zero probes attached, when device_id doesn't match, or when multiple
    probes attached without a disambiguating env var.

    Honors ``STM32_SUBSTRATE_F401RE_SN`` for explicit SN selection in
    multi-probe setups.
    """
    import os

    from embedagents.stm32.cubeprogrammer import CubeProgrammer
    from embedagents.stm32.errors import CubeProgrammerError

    client = CubeProgrammer(hardware_ctx)
    try:
        probes = client.list_probes()
    except CubeProgrammerError as exc:
        pytest.skip(f"list_probes() failed: {exc}")
    if not probes:
        pytest.skip("no ST-LINK probe attached")

    explicit_sn = os.environ.get("STM32_SUBSTRATE_F401RE_SN")
    if explicit_sn:
        matching = [p for p in probes if p.stlink_sn == explicit_sn]
        if not matching:
            pytest.skip(
                f"STM32_SUBSTRATE_F401RE_SN={explicit_sn!r} not in "
                f"attached probes ({[p.stlink_sn for p in probes]})"
            )
        candidate_sn = matching[0].stlink_sn
    elif len(probes) == 1:
        candidate_sn = probes[0].stlink_sn
    else:
        # Multiple probes attached and no env-var pick — try each: latch
        # the first whose banner reports the F401 device_id. NOTE: the
        # trial SN stays latched on the shared hardware_ctx — no restore
        # happens (TST-04's cross-fixture leakage; re-deferred).
        candidate_sn = None
        for p in probes:
            object.__setattr__(hardware_ctx, "default_probe_sn", p.stlink_sn)
            try:
                banner = CubeProgrammer(hardware_ctx).connect()
            except CubeProgrammerError:
                continue
            if banner.device_id == "0x433":
                candidate_sn = p.stlink_sn
                break
        if candidate_sn is None:
            pytest.skip(
                "multiple probes attached and none reports STM32F401xD/xE "
                "device_id 0x433; set STM32_SUBSTRATE_F401RE_SN to "
                "disambiguate."
            )
        return hardware_ctx

    object.__setattr__(hardware_ctx, "default_probe_sn", candidate_sn)
    try:
        banner = CubeProgrammer(hardware_ctx).connect()
    except CubeProgrammerError as exc:
        pytest.skip(f"connect to {candidate_sn} failed: {exc}")
    if banner.device_id != "0x433":
        pytest.skip(
            f"attached probe {candidate_sn} reports device_id "
            f"{banner.device_id!r}; expected 0x433 (STM32F401xD/xE — "
            f"the F401RE on NUCLEO-F401RE). "
            f"Is the NUCLEO-F401RE actually attached?"
        )
    return hardware_ctx


@pytest.fixture(scope="session")
def h7s78_dk_ctx(
    hardware_ctx: "SubstrateContext",
    attached_boards: frozenset[str],
) -> "SubstrateContext":
    """SubstrateContext pinned to the attached STM32H7S78-DK.

    Mirrors ``l476rg_ctx`` / ``n6dk_ctx``. Skips when no H7S78-DK is
    attached. The board enumerates as ``STM32H7S78-DK`` per
    CubeProgrammer's probe-list output. As with ``n6dk_ctx`` the
    underlying ``hardware_ctx`` descriptor is the L476RG fixture
    (descriptor fields unused for board-agnostic substrate ops); the
    mutation here only latches the probe SN for downstream calls.
    """
    if "STM32H7S78-DK" not in attached_boards:
        pytest.skip(
            f"STM32H7S78-DK not attached (detected: {sorted(attached_boards) or 'none'})"
        )

    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    client = CubeProgrammer(hardware_ctx)
    probes = client.list_probes()
    matching = [p for p in probes if p.board_name == "STM32H7S78-DK"]
    if len(matching) != 1:
        pytest.skip(
            f"expected exactly one STM32H7S78-DK probe, found {len(matching)} "
            f"({[p.stlink_sn for p in matching]}); set "
            "STM32_SUBSTRATE_HARDWARE_BOARDS to disambiguate."
        )
    sn = matching[0].stlink_sn
    object.__setattr__(hardware_ctx, "default_probe_sn", sn)
    return hardware_ctx


@pytest.fixture(scope="session")
def h747i_disco_ctx(
    hardware_ctx: "SubstrateContext",
    attached_boards: frozenset[str],
) -> "SubstrateContext":
    """SubstrateContext pinned to the attached STM32H747I-DISCO.

    Mirrors ``l476rg_ctx`` / ``h7s78_dk_ctx`` / ``n6dk_ctx``. Skips when
    no DISCO-H747XI is attached. The board enumerates as
    ``DISCO-H747XI`` (note: no ``NUCLEO`` / ``STM32`` prefix — ST's
    onboard ST-Link/V3 firmware reports the marketing name) per
    CubeProgrammer's probe-list output.

    Underlying ``hardware_ctx`` descriptor is the L476RG fixture; the
    mutation here only latches the probe SN so downstream calls target
    the H747I-DISCO. The H747XI is dual-core (Cortex-M7 @ 480 MHz +
    Cortex-M4 @ 240 MHz); CubeProgrammer defaults to connecting via
    the CM7 boot core.
    """
    if "DISCO-H747XI" not in attached_boards:
        pytest.skip(
            f"DISCO-H747XI not attached (detected: {sorted(attached_boards) or 'none'})"
        )

    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    client = CubeProgrammer(hardware_ctx)
    probes = client.list_probes()
    matching = [p for p in probes if p.board_name == "DISCO-H747XI"]
    if len(matching) != 1:
        pytest.skip(
            f"expected exactly one DISCO-H747XI probe, found {len(matching)} "
            f"({[p.stlink_sn for p in matching]}); set "
            "STM32_SUBSTRATE_HARDWARE_BOARDS to disambiguate."
        )
    sn = matching[0].stlink_sn
    object.__setattr__(hardware_ctx, "default_probe_sn", sn)
    return hardware_ctx


@pytest.fixture(scope="session")
def n6dk_ctx(
    hardware_ctx: "SubstrateContext",
    attached_boards: frozenset[str],
) -> "SubstrateContext":
    """SubstrateContext pinned to the attached STM32N6570-DK.

    Mirrors ``l476rg_ctx``. Skips when no N6-DK is attached. The N6 board
    enumerates as ``STM32N6570-DK`` per CubeProgrammer's probe-list
    output (verified on bench 2026-05-18).

    Note: the ``hardware_ctx`` underlying SubstrateContext loads the
    L476RG project descriptor, but the descriptor fields are unused for
    N6 signing+flash tests (signing is stateless; flash_external takes
    an explicit ``loader_path=``). The mutation here only latches the
    probe SN.
    """
    if "STM32N6570-DK" not in attached_boards:
        pytest.skip(
            f"STM32N6570-DK not attached (detected: {sorted(attached_boards) or 'none'})"
        )

    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    client = CubeProgrammer(hardware_ctx)
    probes = client.list_probes()
    matching = [p for p in probes if p.board_name == "STM32N6570-DK"]
    if len(matching) != 1:
        pytest.skip(
            f"expected exactly one STM32N6570-DK probe, found {len(matching)} "
            f"({[p.stlink_sn for p in matching]}); set "
            "STM32_SUBSTRATE_HARDWARE_BOARDS to disambiguate."
        )
    sn = matching[0].stlink_sn
    object.__setattr__(hardware_ctx, "default_probe_sn", sn)
    return hardware_ctx


# ---------------------------------------------------------------------------
# FAULTING firmware fixture (shared: cubeprogrammer DIAG-001 binary path +
# debug gdb path / A-014)
# ---------------------------------------------------------------------------
#
# Builds the dedicated F-PROJ-NUCLEO-L476RG FAULTING sub-project
# (gitignored user-provides tree under Projects/NUCLEO-L476RG/Examples/
# PWR/FAULTING/). The fixture injects canonical UDF #0 main.c content
# idempotently, builds + flashes via the substrate, hard-resets, then
# re-flashes BLINKY in teardown so downstream tests find the canonical
# L476 firmware. BLINKY's source stays untouched throughout.

_FAULTING_PROJECT = Path(
    "Projects/NUCLEO-L476RG/Examples/PWR/FAULTING"
)


_FAULTING_MAIN_C = """\
/* substrate-test FAULTING firmware: executes the UDF #0 instruction
 * (Permanently Undefined) which is guaranteed to raise a UsageFault on
 * Cortex-M4; UsageFault escalates to HardFault when not enabled (it's
 * disabled by default on reset). The fault state (CFSR, HFSR, BFSR)
 * is sticky in SCB registers and survives the HOTPLUG-mode connect
 * that analyze_hardfault performs.
 *
 * Defines the RTCHandle / UARTHandle symbols that the project's
 * stm32l4xx_it.c references — these stubs satisfy the linker even
 * though they're never initialised (main() faults before reaching
 * any IRQ-handler code path). */
#include "stm32l4xx_hal.h"

void SystemClock_Config(void);
void Error_Handler(void);

RTC_HandleTypeDef RTCHandle;
UART_HandleTypeDef UARTHandle;

int main(void) {
    HAL_Init();
    SystemClock_Config();
    /* Guaranteed fault: UDF #0 is Permanently Undefined Instruction
     * per Armv7-M B5.6.21 — always raises a UsageFault. */
    __asm volatile ("udf #0");
    while (1) {}
}

void SystemClock_Config(void) {}
void Error_Handler(void) { while (1) {} }
"""


@pytest.fixture
def faulting_firmware_flashed(l476rg_ctx, blinky_elf: Path):
    """Write the canonical UDF #0 main.c into the FAULTING sub-project,
    build it, flash + hard-reset → target faults shortly after reset
    and HardFault_Handler loops. Teardown re-flashes BLINKY so
    downstream tests find the canonical L476 firmware.

    The FAULTING source is gitignored user-provides; this fixture
    injects the canonical broken content idempotently at test entry.
    BLINKY's source is never touched.

    Yields the flashed FAULTING.elf path."""
    from embedagents.stm32.cubeide import CubeIDE
    from embedagents.stm32.cubeprogrammer import CubeProgrammer

    proj_root = (l476rg_ctx.cwd / _FAULTING_PROJECT).resolve()
    main_c = proj_root / "Src" / "main.c"
    cubeide_dir = proj_root / "STM32CubeIDE"
    if not (proj_root.is_dir() and main_c.is_file() and cubeide_dir.is_dir()):
        pytest.skip(
            f"FAULTING project not populated at {proj_root}; "
            "user-provides per RES-019 (see plan-test.md)."
        )

    # Workspace nuke (cleanup_stale_project still leaves Eclipse's
    # binary tree state in place per backlog #19; the auto-retry handles
    # it now but a fresh workspace avoids the retry round-trip).
    workspace = Path(l476rg_ctx.project.build.workspace)
    if not workspace.is_absolute():
        workspace = l476rg_ctx.cwd / workspace

    import shutil as _sh
    import time as _t

    try:
        if workspace.exists():
            _sh.rmtree(workspace, ignore_errors=True)
        main_c.write_text(_FAULTING_MAIN_C, encoding="utf-8", newline="\n")
        build_result = CubeIDE(l476rg_ctx).build(
            project=cubeide_dir, clean=True
        )
        if not build_result.success:
            pytest.skip(
                f"FAULTING firmware build failed (exit={build_result.exit_code}); "
                f"check {build_result.log_path}. Tail: "
                f"{build_result.console_output[-500:]}"
            )
        faulty_elf = build_result.artifact_path
        assert faulty_elf is not None and faulty_elf.is_file()
        # Flash + hard-reset so the new firmware actually starts running.
        cp = CubeProgrammer(l476rg_ctx)
        cp.flash_file(faulty_elf)
        cp.reset(hard=True)
        # Settle so HAL_Init → udf → HardFault_Handler executes.
        _t.sleep(2.0)
        yield faulty_elf
    finally:
        # Re-flash BLINKY so downstream tests (test_debug_hardware /
        # test_cubeprogrammer_hardware.TestMemoryReads / etc.) see a
        # valid firmware on the L476 instead of the faulting one.
        # Best-effort: failures swallowed so they don't mask the
        # original test's outcome.
        try:
            CubeProgrammer(l476rg_ctx).flash_file(blinky_elf)
        except Exception:
            pass
