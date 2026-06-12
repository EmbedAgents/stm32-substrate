"""Scaffolding-level eval tests — exercise the driver protocol end-to-end.

These are not substantive product tests. They prove the eval-layer
plumbing (``EvalDriver`` Protocol, scenario/result dataclasses,
assertion helpers, fixture loader) hangs together so subsequent eval
suites (T3 algorithms, slash-command flows) can build on top.

Real per-prompt eval tests will live under ``tests/eval/<module>/`` once
step 12 picks a framework + the live driver lands.
"""

from __future__ import annotations

import pytest

from tests.eval.conftest import (
    EvalScenario,
    LiveDriver,
    ReplayDriver,
    ToolCallMatch,
    assert_scenario_passes,
)


SCAFFOLD_LIST_PROBES = EvalScenario(
    name="F-EVAL-SCAFFOLD-LIST-PROBES",
    user_prompt="What ST-LINK probes are attached?",
    allowed_tools=("Bash",),
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            args_contains={"command": r"stm32\s+prog\s+list-probes"},
        ),
    ),
    # Bench-state-tolerant: the recorded transcript carries whatever
    # board was attached at recording time (currently STM32N6570-DK;
    # was NUCLEO-L476RG in the hand-authored canonical). Replay reads
    # the on-disk transcript; the assertion just confirms Claude
    # reported *some* ST-LINK board, not which one.
    expected_final_text_contains=("ST-LINK",),
)


@pytest.mark.eval
class TestEvalScaffoldReplay:
    """Replay driver against the hand-authored transcript fixture."""

    def test_replay_loads_and_passes_scenario(self, eval_driver) -> None:
        result = eval_driver.run(SCAFFOLD_LIST_PROBES)
        assert_scenario_passes(result, SCAFFOLD_LIST_PROBES)

    def test_replay_explicit_driver_construction(self) -> None:
        """Independent of the env-var-selected fixture: replay driver
        works when constructed directly (used by golden-comparison
        regression checks)."""
        driver = ReplayDriver()
        result = driver.run(SCAFFOLD_LIST_PROBES)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "Bash"
        assert "stm32 prog list-probes" in str(result.tool_calls[0].args["command"])
        # Bench-state-tolerant: recorded transcript may name any attached
        # ST-LINK board (L476RG / N6570-DK / H7S78-DK / etc).
        assert "ST-LINK" in result.final_text


@pytest.mark.eval
class TestEvalScaffoldAssertions:
    """Assertion helper edge cases — exercised against synthetic results
    constructed in-test so they don't depend on a recorded transcript."""

    def test_missing_tool_call_fails(self) -> None:
        from tests.eval.conftest import EvalResult, ToolCall

        result = EvalResult(
            tool_calls=(ToolCall(name="Read", args={"path": "/etc/hosts"}),),
            final_text="ok",
            duration_s=0.0,
        )
        scenario = EvalScenario(
            name="synthetic",
            user_prompt="x",
            allowed_tools=("Bash",),
            expected_tool_calls=(ToolCallMatch(name="Bash"),),
        )
        with pytest.raises(AssertionError, match="no tool call matched"):
            assert_scenario_passes(result, scenario)

    def test_missing_final_text_substring_fails(self) -> None:
        from tests.eval.conftest import EvalResult

        result = EvalResult(tool_calls=(), final_text="hello", duration_s=0.0)
        scenario = EvalScenario(
            name="synthetic",
            user_prompt="x",
            allowed_tools=(),
            expected_final_text_contains=("world",),
        )
        with pytest.raises(AssertionError, match="missing substring"):
            assert_scenario_passes(result, scenario)

    def test_args_contains_regex_matches(self) -> None:
        from tests.eval.conftest import EvalResult, ToolCall

        result = EvalResult(
            tool_calls=(
                ToolCall(name="Bash", args={"command": "stm32 prog flash a.bin"}),
            ),
            final_text="",
            duration_s=0.0,
        )
        scenario = EvalScenario(
            name="synthetic",
            user_prompt="x",
            allowed_tools=("Bash",),
            expected_tool_calls=(
                ToolCallMatch(
                    name="Bash", args_contains={"command": r"prog\s+flash"}
                ),
            ),
        )
        assert_scenario_passes(result, scenario)


@pytest.mark.eval
class TestEvalScaffoldLiveDriver:
    """LiveDriver wraps ``claude-agent-sdk``. The actual live invocation
    is gated behind STM32_EVAL_MODE=live to avoid burning API tokens in
    default test runs; this test just exercises the construction path +
    confirms the SDK import probe works."""

    def test_live_driver_construct_uses_env_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_EVAL_MODEL", raising=False)
        monkeypatch.delenv("STM32_EVAL_MAX_BUDGET_USD", raising=False)
        monkeypatch.delenv("STM32_EVAL_MAX_TURNS", raising=False)
        driver = LiveDriver()
        # Defaults retuned for claude-fable-5 (RES-045 Phase-3 pilot).
        assert driver.model == "claude-fable-5"
        assert driver.max_budget_usd == 1.00
        assert driver.max_turns == 15
        assert driver.record_to is None

    def test_live_driver_construct_honors_env_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STM32_EVAL_MODEL", "claude-opus-4-7")
        monkeypatch.setenv("STM32_EVAL_MAX_BUDGET_USD", "2.50")
        driver = LiveDriver()
        assert driver.model == "claude-opus-4-7"
        assert driver.max_budget_usd == 2.50

    def test_steering_command_file_prefix_map(self) -> None:
        """Live steering resolves to the SHIPPED command file by scenario
        prefix (TST-10); unmapped prefixes fall back to None."""
        from tests.eval.conftest import _steering_command_file

        debug = _steering_command_file("F-EVAL-DEBUG-START")
        assert debug is not None and debug.endswith("stm32debug.md")
        diag = _steering_command_file("F-EVAL-DIAG-002-WATCHDOG")
        assert diag is not None and diag.endswith("stm32debug.md")
        vcp = _steering_command_file("F-EVAL-VCP005-RAISE-BAUD")
        assert vcp is not None and vcp.endswith("stm32agent.md")
        assert _steering_command_file("F-EVAL-SCAFFOLD-SMOKE") is None

    def test_live_driver_skips_when_sdk_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If claude-agent-sdk isn't installed, the driver skips with a
        useful install hint rather than crashing with ImportError."""
        import sys

        # Force the import inside _run_async to fail.
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        with pytest.raises(pytest.skip.Exception, match="claude-agent-sdk"):
            LiveDriver().run(SCAFFOLD_LIST_PROBES)
