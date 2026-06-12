"""CubeIDE build-kwarg hardware tests — B-005/006/008/009/011/012/013/014.

Closes the last test-authorable Pass-1 Build gaps: the structural /
compiler-flag ``build()`` kwargs, each exercised against a real ST
example project via CubeIDE's headless build. These are the kwarg
combinations the BLINKY-only suite never reached.

Projects (user-provides per RES-019; gitignored — each is a stock ST
firmware-bundle example):

  B-005 debug_level        STM32CubeL4 .../FreeRTOS/FreeRTOS_MPU
  B-006 optimization       STM32CubeL4 .../FreeRTOS/FreeRTOS_ThreadCreation
  B-008 preset="size"      STM32CubeL4 .../Demonstrations/Adafruit_LCD_1_8_SD_Joystick
  B-009 preset="balanced"  STM32CubeL4 .../Examples/RTC/RTC_Alarm
  B-011 add_symbols        STM32CubeL4 .../Examples/CRC/CRC_Bytes_Stream_7bit_CRC
  B-012 add_libraries      n6-projects-lib-test/x-cube-n6-ai-multi-pose-estimation (CM55)
  B-013 add_sources        STM32CubeL4 .../Examples/RTC/RTC_Alarm
  B-014 add_include_paths  STM32CubeL4 .../Examples/RTC/RTC_TimeStamp

Three of these (B-011 / B-012 / B-013) are *negative→positive* round
trips: the project is put into (or starts in) a failing state, the build
is confirmed to fail, the kwarg is applied, and the build is confirmed to
pass — proving the edit is load-bearing rather than incidental.

No probe is touched (headless builds don't reach the target); the tests
live under ``hardware`` so they group with the bench suite. Each uses an
isolated ``tmp_path``-rooted ``build_ctx`` (no project descriptor, so the
Eclipse workspace is a fresh per-test dir) — deliberately decoupled from
the L476RG fixture descriptor so a fixture-tree rsync can't drag a stale
``build.workspace`` into the build. ``project=`` / ``configuration=`` are
always passed explicitly. Each test restores the project's ``.cproject``
(and removes any substrate-copied source) on teardown so the user's
fixture tree stays pristine. Invoke with ``pytest -m hardware``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from embedagents.stm32.cubeide import CubeIDE
from embedagents.stm32.cubeide.results import BuildResult

# ---------------------------------------------------------------------------
# Project locations (relative to repo root)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_L4 = _REPO_ROOT / "tests/fixtures/projects/STM32CubeL4/Projects/NUCLEO-L476RG"
_N6_LIB = _REPO_ROOT / "tests/fixtures/n6-projects-lib-test/x-cube-n6-ai-multi-pose-estimation"

_FREERTOS_MPU = _L4 / "Applications/FreeRTOS/FreeRTOS_MPU/STM32CubeIDE"
_FREERTOS_THREAD = _L4 / "Applications/FreeRTOS/FreeRTOS_ThreadCreation/STM32CubeIDE"
_ADAFRUIT = _L4 / "Demonstrations/Adafruit_LCD_1_8_SD_Joystick/STM32CubeIDE"
_RTC_ALARM = _L4 / "Examples/RTC/RTC_Alarm/STM32CubeIDE"
_RTC_ALARM_SRC = _L4 / "Examples/RTC/RTC_Alarm/Src/main.c"
_RTC_TAMPER = _L4 / "Examples/RTC/RTC_Tamper/STM32CubeIDE"
_CRC = _L4 / "Examples/CRC/CRC_Bytes_Stream_7bit_CRC/STM32CubeIDE"
_RTC_TIMESTAMP = _L4 / "Examples/RTC/RTC_TimeStamp/STM32CubeIDE"
_N6_PROJECT = _N6_LIB / "STM32CubeIDE"
_N6_LIB_A = _N6_LIB / "Lib/AI_Runtime/Lib/GCC/ARMCortexM55/NetworkRuntime1100_CM55_GCC.a"

_DEVICE_SYMBOL = "STM32L476xx"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def build_ctx(tmp_path: Path):
    """Isolated SubstrateContext for headless builds.

    Rooted at a fresh ``tmp_path`` with no project descriptor, so the
    resolved Eclipse workspace is ``tmp_path/.stm32-substrate-workspace``
    — unique per test (no cross-test import collisions) and independent of
    any committed fixture descriptor. Skips the suite if the vendor CLIs
    don't resolve on this host.
    """
    from embedagents.stm32.context import SubstrateContext
    from embedagents.stm32.errors import ConfigurationError

    # cwd=tmp_path isolates the workspace, but the repo's tool-paths config
    # lives under .claude/ — pass it explicitly (the upward search from
    # /tmp would never find it).
    tools_cfg = _REPO_ROOT / ".claude" / "stm32-tools.local.jsonc"
    try:
        return SubstrateContext.from_environment(
            project_path=tmp_path,
            tools_config_path=tools_cfg if tools_cfg.is_file() else None,
        )
    except ConfigurationError as exc:
        pytest.skip(
            f"cubeide build suite skipped: context not loadable — "
            f"{exc.message} (hint: {exc.hint or '(none)'})"
        )


def _require(project_dir: Path) -> None:
    """Skip cleanly when the user-provides project tree isn't populated."""
    if not (project_dir / ".cproject").is_file():
        pytest.skip(
            f"project not populated at {project_dir} (.cproject missing); "
            "user-provides per RES-019 — extract the ST firmware bundle."
        )


@contextmanager
def _pristine(project_dir: Path) -> Iterator[None]:
    """Snapshot/restore ``.cproject`` and remove substrate-created debris.

    Restores the exact ``.cproject`` bytes, deletes any
    ``.cproject.substrate-backup-*`` left by the editor, and removes files
    the test copied into the project dir (e.g. an add_sources main.c)."""
    cproject = project_dir / ".cproject"
    original = cproject.read_bytes()
    before = {p for p in project_dir.iterdir() if p.is_file()}
    try:
        yield
    finally:
        cproject.write_bytes(original)
        for p in project_dir.glob(".cproject.substrate-backup-*"):
            p.unlink()
        for p in {q for q in project_dir.iterdir() if q.is_file()} - before:
            p.unlink()


def _fail_tail(result: BuildResult) -> str:
    return (
        f"exit={result.exit_code}; see {result.log_path}. "
        f"Console tail:\n{result.console_output[-1500:]}"
    )


def _debug_compiler_symbols(cproject: Path) -> list[str]:
    """C-compiler definedsymbols values for the Debug configuration only."""
    root = ET.parse(cproject).getroot()
    out: list[str] = []
    for cconfig in root.iter("cconfiguration"):
        cfg = cconfig.find(".//configuration")
        if cfg is None or cfg.get("name") != "Debug":
            continue
        for opt in cconfig.iter("option"):
            if opt.get("superClass", "").endswith("c.compiler.option.definedsymbols"):
                out += [c.get("value") for c in opt.findall("listOptionValue")]
    return out


def _list_values(cproject: Path, suffix: str) -> list[str]:
    """All ``listOptionValue`` values whose option superClass ends with
    ``suffix`` (across all configurations)."""
    tree = ET.parse(cproject)
    out: list[str] = []
    for opt in tree.iter("option"):
        if opt.get("superClass", "").endswith(suffix):
            out += [c.get("value") for c in opt.findall("listOptionValue")]
    return out


# ---------------------------------------------------------------------------
# B-005 / B-006 — single compiler-flag edits
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestDebugLevel:
    """B-005 — build(debug_level=...) sets the active config's debuglevel
    option then builds clean."""

    def test_debug_level_g3_builds_clean(self, build_ctx) -> None:
        _require(_FREERTOS_MPU)
        with _pristine(_FREERTOS_MPU):
            result = CubeIDE(build_ctx).build(
                project=_FREERTOS_MPU,
                configuration="Debug",
                debug_level="-g3",
                clean=True,
            )
            if not result.success:
                pytest.fail(f"build(debug_level='-g3') failed: {_fail_tail(result)}")
            assert result.artifact_path is not None and result.artifact_path.is_file()
            assert result.settings_modification is not None
            change = result.settings_modification.changes[0]
            assert change.kind == "set_value"
            assert "debuglevel" in change.superclass_id


@pytest.mark.hardware
class TestOptimization:
    """B-006 — build(optimization=...) sets optimization.level then builds."""

    def test_optimization_o2_builds_clean(self, build_ctx) -> None:
        _require(_FREERTOS_THREAD)
        with _pristine(_FREERTOS_THREAD):
            result = CubeIDE(build_ctx).build(
                project=_FREERTOS_THREAD,
                configuration="Debug",
                optimization="-O2",
                clean=True,
            )
            if not result.success:
                pytest.fail(f"build(optimization='-O2') failed: {_fail_tail(result)}")
            assert result.artifact_path is not None and result.artifact_path.is_file()
            assert result.settings_modification is not None
            assert "optimization" in result.settings_modification.changes[0].superclass_id


# ---------------------------------------------------------------------------
# B-008 / B-009 — multi-option presets
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestPresetSize:
    """B-008 — preset='size' (-Os + -g1 + --gc-sections + best-effort
    newlib-nano). usenewlibnano is set_value_soft: absent on this stock
    project, so it soft-no-ops instead of raising."""

    def test_preset_size_builds_clean(self, build_ctx) -> None:
        _require(_ADAFRUIT)
        with _pristine(_ADAFRUIT):
            result = CubeIDE(build_ctx).build(
                project=_ADAFRUIT,
                configuration="Debug",
                preset="size",
                clean=True,
            )
            if not result.success:
                pytest.fail(f"preset='size' build failed: {_fail_tail(result)}")
            assert result.artifact_path is not None and result.artifact_path.is_file()
            assert result.settings_modification is not None
            assert result.settings_modification.rolled_back is False


@pytest.mark.hardware
class TestPresetBalanced:
    """B-009 — preset='balanced' (-O2 + -g3): set optimization.level +
    debuglevel, then build clean. RTC_Tamper builds clean stock (unlike
    RTC_Alarm, whose main.c is excluded — that's B-013)."""

    def test_preset_balanced_builds_clean(self, build_ctx) -> None:
        _require(_RTC_TAMPER)
        with _pristine(_RTC_TAMPER):
            result = CubeIDE(build_ctx).build(
                project=_RTC_TAMPER,
                configuration="Debug",
                preset="balanced",
                clean=True,
            )
            if not result.success:
                pytest.fail(f"preset='balanced' build failed: {_fail_tail(result)}")
            assert result.artifact_path is not None and result.artifact_path.is_file()
            superclasses = " ".join(
                c.superclass_id for c in result.settings_modification.changes
            )
            assert "optimization" in superclasses and "debuglevel" in superclasses


# ---------------------------------------------------------------------------
# B-011 — add_symbols (negative → positive round trip)
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAddSymbols:
    """B-011 — the CRC project's stock **Debug** config omits the device
    symbol STM32L476xx (only Release carries it), so the CMSIS device
    header (#error "Please select ... target device") fails the build.
    add_symbols=[STM32L476xx] restores a clean build — and proves the
    symbol lands in the real ...c.compiler.option.definedsymbols list (the
    regex-drift fix; the old preprocessor.def.symbols regex would have
    soft-no-op'd and left the build broken)."""

    def test_missing_device_symbol_fails_then_add_symbols_fixes(
        self, build_ctx
    ) -> None:
        _require(_CRC)
        cproject = _CRC / ".cproject"
        # Precondition the user described: Debug ships without the symbol.
        assert _DEVICE_SYMBOL not in _debug_compiler_symbols(cproject), (
            "expected the CRC Debug config to omit STM32L476xx out of the box"
        )
        with _pristine(_CRC):
            # 1) failing baseline — Debug lacks the device define
            failed = CubeIDE(build_ctx).build(
                project=_CRC, configuration="Debug", clean=True
            )
            assert failed.success is False, (
                "expected build to fail without the device symbol; "
                f"{_fail_tail(failed)}"
            )
            # 2) substrate adds the symbol → clean build
            fixed = CubeIDE(build_ctx).build(
                project=_CRC,
                configuration="Debug",
                add_symbols=[_DEVICE_SYMBOL],
                clean=True,
            )
            if not fixed.success:
                pytest.fail(f"add_symbols build failed: {_fail_tail(fixed)}")
            assert _DEVICE_SYMBOL in _debug_compiler_symbols(cproject)
            assert fixed.artifact_path is not None and fixed.artifact_path.is_file()


# ---------------------------------------------------------------------------
# B-014 — add_include_paths
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAddIncludePaths:
    """B-014 — add_include_paths appends to the real
    ...c.compiler.option.includepaths (plural) list (the regex-drift fix).

    Negative→positive: RTC_TimeStamp's stock include resolves to
    ``STM32CubeIDE/Inc`` from the headless build dir, but its headers live
    in ``RTC_TimeStamp/Inc`` (a sibling of STM32CubeIDE) — so the stock
    build fails with ``main.h: No such file``. Adding the project's real
    Inc dir (absolute, to sidestep build-dir-relative fragility) resolves
    the headers → clean build."""

    def test_missing_header_fails_then_add_include_fixes(self, build_ctx) -> None:
        _require(_RTC_TIMESTAMP)
        inc_dir = _RTC_TIMESTAMP.parent / "Inc"
        if not (inc_dir / "main.h").is_file():
            pytest.skip(f"RTC_TimeStamp Inc/main.h missing at {inc_dir}")
        with _pristine(_RTC_TIMESTAMP):
            cproject = _RTC_TIMESTAMP / ".cproject"
            # 1) failing baseline — main.h not on the resolvable include path
            failed = CubeIDE(build_ctx).build(
                project=_RTC_TIMESTAMP, configuration="Debug", clean=True
            )
            assert failed.success is False, (
                f"expected stock build to fail on missing main.h; "
                f"{_fail_tail(failed)}"
            )
            # 2) add the project's Inc dir → clean build
            fixed = CubeIDE(build_ctx).build(
                project=_RTC_TIMESTAMP,
                configuration="Debug",
                add_include_paths=[str(inc_dir)],
                clean=True,
            )
            if not fixed.success:
                pytest.fail(f"add_include_paths build failed: {_fail_tail(fixed)}")
            assert str(inc_dir) in _list_values(cproject, "includepaths")
            assert fixed.artifact_path is not None and fixed.artifact_path.is_file()


# ---------------------------------------------------------------------------
# B-013 — add_sources (negative → positive round trip)
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAddSources:
    """B-013 — RTC_Alarm excludes Example/User/main.c and never links it, so
    the stock project fails to link (no main). add_sources copies main.c
    into the project's scanned tree → clean build."""

    def test_missing_main_fails_then_add_sources_fixes(self, build_ctx) -> None:
        _require(_RTC_ALARM)
        if not _RTC_ALARM_SRC.is_file():
            pytest.skip(f"RTC_Alarm Src/main.c missing at {_RTC_ALARM_SRC}")
        with _pristine(_RTC_ALARM):
            # 1) failing baseline — main.c excluded + unlinked
            failed = CubeIDE(build_ctx).build(
                project=_RTC_ALARM, configuration="Debug", clean=True
            )
            assert failed.success is False, (
                f"expected link failure without main.c; {_fail_tail(failed)}"
            )
            # 2) copy main.c into the build → clean build
            fixed = CubeIDE(build_ctx).build(
                project=_RTC_ALARM,
                configuration="Debug",
                add_sources=[(_RTC_ALARM_SRC, Path("main.c"))],
                clean=True,
            )
            if not fixed.success:
                pytest.fail(f"add_sources build failed: {_fail_tail(fixed)}")
            assert (_RTC_ALARM / "main.c").is_file()
            assert fixed.artifact_path is not None and fixed.artifact_path.is_file()


# ---------------------------------------------------------------------------
# B-012 — add_libraries (negative → positive round trip; N6 / CM55)
# ---------------------------------------------------------------------------


@pytest.mark.hardware
class TestAddLibraries:
    """B-012 — the N6 AI project's .cproject already carries the -L search
    path but not the NetworkRuntime archive in its -l list, so the link
    fails on undefined network-runtime symbols. add_libraries appends the
    archive (``:NetworkRuntime1100_CM55_GCC.a``) → clean link.

    Heaviest of the eight: a Cortex-M55 build that needs the X-CUBE-N6
    GCC toolchain + the full project tree populated."""

    def test_missing_lib_fails_then_add_libraries_fixes(self, build_ctx) -> None:
        _require(_N6_PROJECT)
        if not _N6_LIB_A.is_file():
            pytest.skip(f"NetworkRuntime archive missing at {_N6_LIB_A}")
        with _pristine(_N6_PROJECT):
            # 1) failing baseline — archive not in the libraries (-l) list
            failed = CubeIDE(build_ctx).build(
                project=_N6_PROJECT, configuration="Debug", clean=True
            )
            assert failed.success is False, (
                f"expected link failure without the runtime archive; "
                f"{_fail_tail(failed)}"
            )
            # 2) substrate adds the archive → clean link
            fixed = CubeIDE(build_ctx).build(
                project=_N6_PROJECT,
                configuration="Debug",
                add_libraries=[_N6_LIB_A],
                clean=True,
            )
            if not fixed.success:
                pytest.fail(f"add_libraries build failed: {_fail_tail(fixed)}")
            assert ":NetworkRuntime1100_CM55_GCC.a" in _list_values(
                _N6_PROJECT / ".cproject", "linker.option.libraries"
            )
            assert fixed.artifact_path is not None and fixed.artifact_path.is_file()
