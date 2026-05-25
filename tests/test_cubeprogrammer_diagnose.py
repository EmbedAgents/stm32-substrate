"""B5d tests — D-002 SWD recovery ladder.

Tests mock ``CubeProgrammer._raw_connect`` directly rather than going
through ``run_tool`` so they're fast + deterministic. The ladder algorithm
is the unit under test, not the CLI plumbing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer
from stm32_substrate.cubeprogrammer.codes import CubeProgrammerErrorCode
from stm32_substrate.cubeprogrammer.diagnose import (
    LADDER_FREQS_KHZ,
    LADDER_MODES,
    run_diagnose,
)
from stm32_substrate.cubeprogrammer.results import BannerResult, RecoveryResult
from stm32_substrate.errors import CubeProgrammerError


@pytest.fixture()
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubstrateContext:
    fake_cli = tmp_path / "STM32_Programmer_CLI"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
    return SubstrateContext.from_environment(project_path=tmp_path)


def _good_banner(*, mode: str = "NORMAL", freq_khz: int = 4000) -> BannerResult:
    return BannerResult(
        stlink_sn="066BFF",
        stlink_fw="V3J11M3",
        board_name="NUCLEO-L476RG",
        voltage_v=3.28,
        swd_freq_khz=freq_khz,
        device_id="0x415",
        device_name="STM32L47xxx/L48xxx",
        device_type="MCU",
        device_cpu="Cortex-M4",
        flash_size_kb=1024,
        mode_used=mode,  # type: ignore[arg-type]
    )


def _err(
    *, error_code: CubeProgrammerErrorCode | int | None, message: str = "failed"
) -> CubeProgrammerError:
    return CubeProgrammerError(
        message=message,
        code=int(error_code) if error_code is not None else None,
        error_code=int(error_code) if error_code is not None else None,
    )


# ---------------------------------------------------------------------------
# Ladder constants
# ---------------------------------------------------------------------------


class TestLadderConstants:
    def test_modes_canonical(self) -> None:
        assert LADDER_MODES == ["NORMAL", "UR", "HOTPLUG", "POWERDOWN", "hwRstPulse"]

    def test_freqs_canonical(self) -> None:
        assert LADDER_FREQS_KHZ == [None, 4000, 1800, 480]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestFirstModeSuccess:
    def test_normal_default_freq_wins(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect", return_value=_good_banner()) as mocked:
            result = run_diagnose(client, timeout_s=120.0)
        assert isinstance(result, RecoveryResult)
        assert result.target_responsive is True
        assert result.recovery_method == "NORMAL"
        assert result.swd_freq_khz_used == 4000
        assert len(result.attempts_log) == 1
        assert result.attempts_log[0].success is True
        # First call: mode=NORMAL, freq_khz=None (device default).
        assert mocked.call_count == 1
        kwargs = mocked.call_args.kwargs
        assert kwargs == {"mode": "NORMAL", "freq_khz": None}


class TestLastAttemptSuccess:
    def test_hwrstpulse_at_480_wins_after_full_walk(
        self, ctx: SubstrateContext
    ) -> None:
        # 5*4 = 20 attempts total. First 19 raise non-fatal errors;
        # the 20th (last freq × last mode = freq=480, mode=hwRstPulse)
        # succeeds.
        side_effects: list = [
            _err(error_code=CubeProgrammerErrorCode.TARGET_NO_DEVICE)
            for _ in range(19)
        ]
        side_effects.append(_good_banner(mode="hwRstPulse", freq_khz=480))

        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect", side_effect=side_effects):
            result = run_diagnose(client, timeout_s=120.0)
        assert result.target_responsive is True
        assert result.recovery_method == "hwRstPulse"
        assert result.swd_freq_khz_used == 480
        assert len(result.attempts_log) == 20
        # All but the last attempt are failures.
        assert all(a.success is False for a in result.attempts_log[:-1])
        assert result.attempts_log[-1].success is True


class TestAllFail:
    def test_returns_target_responsive_false(self, ctx: SubstrateContext) -> None:
        side_effects = [
            _err(error_code=CubeProgrammerErrorCode.TARGET_NO_DEVICE) for _ in range(20)
        ]
        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect", side_effect=side_effects):
            result = run_diagnose(client, timeout_s=120.0)
        assert result.target_responsive is False
        assert result.recovery_method is None
        assert result.swd_freq_khz_used is None
        assert len(result.attempts_log) == 20
        assert result.bailed_on_timeout is False


# ---------------------------------------------------------------------------
# Early-abort on fatal codes
# ---------------------------------------------------------------------------


class TestEarlyAbortDllErr:
    def test_aborts_after_first_attempt(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch.object(
            client,
            "_raw_connect",
            side_effect=_err(error_code=CubeProgrammerErrorCode.TARGET_DLL_ERR),
        ) as mocked:
            result = run_diagnose(client, timeout_s=120.0)
        assert result.target_responsive is False
        assert mocked.call_count == 1
        assert len(result.attempts_log) == 1
        assert (
            result.attempts_log[0].error_code
            == CubeProgrammerErrorCode.TARGET_DLL_ERR
        )


class TestEarlyAbortFirmwareOld:
    def test_aborts_after_first_attempt(self, ctx: SubstrateContext) -> None:
        client = CubeProgrammer(ctx)
        with patch.object(
            client,
            "_raw_connect",
            side_effect=_err(
                error_code=CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD
            ),
        ) as mocked:
            result = run_diagnose(client, timeout_s=120.0)
        assert result.target_responsive is False
        assert mocked.call_count == 1


# ---------------------------------------------------------------------------
# Timeout bail (RES-020)
# ---------------------------------------------------------------------------


class TestOverallTimeout:
    def test_bails_when_cap_exceeded_mid_ladder(self, ctx: SubstrateContext) -> None:
        """Mock ``time.monotonic`` to control elapsed values: ``start``
        records 0.0, the first iteration's pre-check sees elapsed=0
        (passes), then the second iteration's pre-check sees elapsed=60
        which trips the 30s cap."""
        side_effects = [
            _err(error_code=CubeProgrammerErrorCode.TARGET_NO_DEVICE)
            for _ in range(20)
        ]
        # monotonic() calls: 1 for start, 1 per iteration pre-check.
        clock_values = iter([0.0, 0.0, 60.0])
        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect", side_effect=side_effects):
            with patch(
                "stm32_substrate.cubeprogrammer.diagnose.time.monotonic",
                side_effect=lambda: next(clock_values),
            ):
                result = run_diagnose(client, timeout_s=30.0)
        assert result.target_responsive is False
        assert result.bailed_on_timeout is True
        # First iteration's attempt ran (logged); second iteration's
        # pre-check tripped the cap before its attempt could fire.
        assert len(result.attempts_log) == 1

    def test_immediate_bail_with_zero_timeout(self, ctx: SubstrateContext) -> None:
        """``timeout_s=0.0`` is the degenerate edge case: the first
        pre-check is ``elapsed >= 0.0``, true regardless of clock
        resolution, so the ladder bails before the first attempt runs.
        (The bail uses ``>=`` precisely so this holds on Windows' coarse
        monotonic clock, where elapsed can read exactly 0.0 — not only on
        Linux's fine clock.) ``attempts_log`` is empty; ``bailed_on_timeout``
        is True."""
        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect") as mocked:
            result = run_diagnose(client, timeout_s=0.0)
        assert result.bailed_on_timeout is True
        assert result.attempts_log == []
        assert mocked.call_count == 0


# ---------------------------------------------------------------------------
# Recoverable error continues the ladder
# ---------------------------------------------------------------------------


class TestRecoverableContinues:
    def test_no_device_continues_to_next_attempt(
        self, ctx: SubstrateContext
    ) -> None:
        """TARGET_NO_DEVICE is recoverable per the matrix — the ladder
        keeps walking after it."""
        side_effects: list = [
            _err(error_code=CubeProgrammerErrorCode.TARGET_NO_DEVICE),
            _good_banner(mode="UR"),
        ]
        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect", side_effect=side_effects):
            result = run_diagnose(client, timeout_s=120.0)
        assert result.target_responsive is True
        assert result.recovery_method == "UR"
        assert len(result.attempts_log) == 2


# ---------------------------------------------------------------------------
# Method-level integration: diagnose_micro() reads the timeout knob
# ---------------------------------------------------------------------------


class TestDiagnoseMicroMethod:
    def test_calls_run_diagnose_with_default_cap(
        self, ctx: SubstrateContext
    ) -> None:
        client = CubeProgrammer(ctx)
        with patch.object(client, "_raw_connect", return_value=_good_banner()):
            result = client.diagnose_micro()
        assert result.target_responsive is True

    def test_honors_diagnose_timeout_s_runtime_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        fake_cli = tmp_path / "STM32_Programmer_CLI"
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(fake_cli))
        defaults = {
            "version": 1,
            "programmer": {"diagnose_timeout_s": 60},
        }
        (tmp_path / "stm32-runtime-defaults.jsonc").write_text(
            json.dumps(defaults)
        )
        ctx2 = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeProgrammer(ctx2)

        with patch(
            "stm32_substrate.cubeprogrammer.client.diagnose.run_diagnose"
        ) as mocked:
            mocked.return_value = RecoveryResult(
                target_responsive=True,
                recovery_method="NORMAL",
                swd_freq_khz_used=4000,
            )
            client.diagnose_micro()
        assert mocked.call_args.kwargs["timeout_s"] == 60.0
