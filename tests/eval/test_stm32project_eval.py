"""Eval tests for the ``/stm32project`` slash-command surface.

Validates that natural-language prompts about (re)generating a CubeIDE
project from a `.ioc` file route through Claude into the substrate's
``stm32 mx generate`` CLI correctly. Per-prompt fixtures live under
``tests/fixtures/eval/F-EVAL-MX-*/transcript.jsonc``.

Layout convention (one file per slash command):

  tests/eval/test_stm32prog_eval.py                    — /stm32prog
  tests/eval/test_stm32build_eval.py                   — /stm32build
  tests/eval/test_stm32debug_eval.py                   — /stm32debug
  tests/eval/test_stm32project_eval.py (this file)     — /stm32project
  tests/eval/test_stm32agent_eval.py                   — /stm32agent + VCP

Scope (per P-037 cubemx cut): only MX-001 (`stm32 mx generate`) is in
v1. MX-002/003/004/006 + B-017 are `[out]`. This file therefore stays
small — one scenario covers the entire `/stm32project` surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


# IOC fixture cwd carries a `stm32-project.jsonc` with `cubemx.ioc_path`
# so `stm32 mx generate` (no positional) autodiscovers per the rule
# documented in .claude/commands/stm32project.md. The on-disk .ioc is
# nucleo-l476rg-rtc.ioc; the descriptor names it so Claude doesn't
# need to Glob.
PROJECT_CWD = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "cubemx-projects"
    / "nucleo-l476rg-rtc"
)


# ---------------------------------------------------------------------------
# Scenario 1 — MX-001 default regenerate (autodiscovered IOC)
# ---------------------------------------------------------------------------

MX_DEFAULT = EvalScenario(
    name="F-EVAL-MX-DEFAULT",
    user_prompt="Generate my project from the ioc file.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude calls `stm32 mx generate`. The
            # IOC path is autodiscovered from cwd / descriptor per
            # stm32project.md's "Regenerate" subcommand map. Claude
            # may either invoke bare (autodiscovery) or with an
            # explicit IOC path — both match this regex.
            args_contains={"command": r"stm32\s+mx\s+generate\b"},
        ),
    ),
    # "generate" is the strongest domain cue — Claude reporting on a
    # CubeMX generate operation uses it whether the result succeeded
    # or failed. Case-sensitive substring match.
    expected_final_text_contains=("generate",),
)


# ---------------------------------------------------------------------------
# Test class — one test per scenario
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestStm32ProjectEval:
    """Default replay mode: reads each scenario's transcript from disk.

    To run live (costs ~$0.04 each):
        STM32_EVAL_MODE=live pytest tests/eval/test_stm32project_eval.py -m eval -v

    To re-record canonical transcripts after a substrate or model
    change (writes to tests/fixtures/eval/F-EVAL-MX-*/transcript.jsonc):
        STM32_EVAL_MODE=record pytest tests/eval/test_stm32project_eval.py -m eval -v
    """

    def test_mx_default(self, eval_driver) -> None:
        """User asks 'Generate my project from the ioc file.' → Claude
        calls ``stm32 mx generate`` (IOC autodiscovered from the
        descriptor's ``cubemx.ioc_path`` field) and reports the
        outcome."""
        result = eval_driver.run(MX_DEFAULT)
        assert_scenario_passes(result, MX_DEFAULT)
