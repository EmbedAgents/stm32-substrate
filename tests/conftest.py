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
    from stm32_substrate.context import SubstrateContext


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_LOCK_PATH = _REPO_ROOT / ".stm32-substrate" / "probe.lock"
_L476RG_PROJECT_PATH = _REPO_ROOT / "tests" / "fixtures" / "projects" / "F-PROJ-NUCLEO-L476RG"


# ---------------------------------------------------------------------------
# Probe-lock (session-scoped — serialises SWD access across pytest runs)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def probe_lock() -> object:
    """Session-scoped exclusive lock on the singleton ST-LINK probe.

    Implemented via ``stm32_substrate.platform.acquire_exclusive_lock`` —
    cross-platform per ADR-007 (fcntl on Linux, msvcrt on Windows). Raises
    ``BlockingIOError`` immediately when another pytest session is already
    holding the probe (no long waits per HIL M-019). Two concurrent
    ``pytest -m hardware`` invocations are a configuration error; the
    error tells the user to wait for the other run.

    Released when the test session ends.
    """
    from stm32_substrate.platform import acquire_exclusive_lock

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
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.errors import ConfigurationError

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

    from stm32_substrate.cubeprogrammer import CubeProgrammer

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

    from stm32_substrate.cubeprogrammer import CubeProgrammer

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
    from stm32_substrate.context import SubstrateContext
    from stm32_substrate.errors import ConfigurationError

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

    from stm32_substrate.cubeprogrammer import CubeProgrammer
    from stm32_substrate.errors import CubeProgrammerError

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
        # the first whose banner reports F401 device_id. Substrate
        # restores SN on no-match so other fixtures see clean state.
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

    from stm32_substrate.cubeprogrammer import CubeProgrammer

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

    from stm32_substrate.cubeprogrammer import CubeProgrammer

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

    from stm32_substrate.cubeprogrammer import CubeProgrammer

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
