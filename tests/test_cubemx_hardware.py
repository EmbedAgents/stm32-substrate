"""CubeMX smoke test - real ``STM32CubeMX -q`` invocation.

Marked ``smoke`` (not ``hardware``) because CubeMX generate is a pure
code-generation step: it needs the vendor CLI installed but no
attached probe, no board. The test fixture is the hand-authored
NUCLEO-L476RG RTC project under
``tests/fixtures/cubemx-projects/nucleo-l476rg-rtc/``; the .ioc
targets STM32L476RGT3 with RTC + USART2 enabled.

Excluded from the default ``pytest`` run; invoke with
``pytest -m smoke``. Expected wall-clock: 30 s - 3 min depending on
CubeMX cold-start + JVM warm-up + the IOC's peripheral count.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubemx import CubeMX, CubeMXResult
from stm32_substrate.errors import ConfigurationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent
_RTC_IOC_FIXTURE = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "cubemx-projects"
    / "nucleo-l476rg-rtc"
    / "nucleo-l476rg-rtc.ioc"
)


@pytest.fixture(scope="session")
def cubemx_ctx() -> SubstrateContext:
    """Probe-independent SubstrateContext for cubemx smoke tests.

    CubeMX doesn't touch hardware, so we don't take the shared
    ``probe_lock`` and don't gate on ``attached_boards``. Just need
    the CubeMX path to resolve - fail early with a skip if it doesn't.
    """
    try:
        ctx = SubstrateContext.from_environment()
    except ConfigurationError as exc:
        pytest.skip(f"cubemx-smoke skipped: {exc.message}")
    if ctx.tools.cubemx_executable is None:
        pytest.skip(
            "cubemx-smoke skipped: CubeMX executable not resolvable "
            "(set STM32CUBEMX_PATH env or cubemx.path in tools config)"
        )
    return ctx


@pytest.fixture
def rtc_ioc_in_tmp(tmp_path: Path) -> Path:
    """Copy the canonical RTC fixture .ioc into tmp_path so the
    generate output (Drivers/, Inc/, Src/, .project, .cproject) lands
    in a per-test tmp area, not in the repo. CubeMX overwrites in
    place by default - keeping it under tmp_path keeps the repo
    clean between runs."""
    if not _RTC_IOC_FIXTURE.is_file():
        pytest.skip(f"missing fixture IOC: {_RTC_IOC_FIXTURE}")
    dst = tmp_path / _RTC_IOC_FIXTURE.name
    shutil.copy2(_RTC_IOC_FIXTURE, dst)
    return dst


# ---------------------------------------------------------------------------
# TestGenerate
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestGenerate:
    def test_generate_creates_cubeide_project_tree(
        self, cubemx_ctx: SubstrateContext, rtc_ioc_in_tmp: Path
    ) -> None:
        """Happy path: generate the NUCLEO-L476RG RTC project; substrate
        confirms success via the .cproject marker file + a generated
        CubeIDE project tree (Drivers/, Inc/, Src/, .project)."""
        result = CubeMX(cubemx_ctx).generate(
            ioc_path=rtc_ioc_in_tmp,
            output_path=rtc_ioc_in_tmp.parent,
            project_name="nucleo-l476rg-rtc",
            timeout_s=300.0,  # 5 min upper bound; substrate may extend
        )
        assert isinstance(result, CubeMXResult)
        if not result.success:
            pytest.fail(
                f"CubeMX generate failed "
                f"(exit_code={result.exit_code}, "
                f"timed_out={result.timed_out}, "
                f"duration={result.duration_s:.1f}s, "
                f"extensions_used={result.extensions_used}). "
                f"Substrate log: {result.log_path}. "
                f"CubeMX log: {result.cubemx_log_path}."
            )
        assert result.output_dir is not None
        # output_dir = Eclipse project root (<output>/<name>/<toolchain>/)
        # per CubeMX's STM32CubeIDE double-nesting; .cproject + .project
        # live here.
        assert (result.output_dir / ".cproject").is_file(), (
            f"expected .cproject marker in {result.output_dir}; "
            f"contents: {sorted(result.output_dir.iterdir())}"
        )
        assert (result.output_dir / ".project").is_file()
        # The source tree (Drivers/, Inc/, Src/) lives one level up at
        # <output>/<name>/. Substrate's output_dir points at the Eclipse
        # root; the source-tree root is its parent.
        source_root = result.output_dir.parent
        assert (source_root / "Drivers").is_dir(), (
            f"expected Drivers/ at {source_root}; "
            f"got {sorted(source_root.iterdir())}"
        )
        assert (source_root / "Core" / "Inc").is_dir() or (
            source_root / "Inc"
        ).is_dir(), (
            "expected either Core/Inc (modern layout) or Inc (legacy); "
            f"got {sorted(source_root.iterdir())}"
        )
        assert (source_root / "Core" / "Src").is_dir() or (
            source_root / "Src"
        ).is_dir()
