"""Eval tests for the ``/stm32build`` slash-command surface.

Validates that natural-language prompts about building a CubeIDE project
route through Claude into the substrate's ``stm32 build ...`` CLI
correctly. Per-prompt fixtures live under
``tests/fixtures/eval/F-EVAL-BUILD-*/transcript.jsonc``.

Layout convention (one file per slash command):

  tests/eval/test_stm32prog_eval.py                    — /stm32prog
  tests/eval/test_stm32build_eval.py   (this file)     — /stm32build
  tests/eval/test_stm32debug_eval.py                   — /stm32debug
  tests/eval/test_stm32project_eval.py                 — /stm32project
  tests/eval/test_stm32agent_eval.py                   — /stm32agent + VCP

Workflow for adding a new scenario:

  1. Define ``EvalScenario(name="F-EVAL-BUILD-<NAME>", ...)`` at module
     scope with realistic expectations (loose enough to survive Claude's
     non-determinism; strict enough to catch real regressions).
  2. Add a one-line ``test_<name>`` method calling
     ``eval_driver.run(scenario)`` + ``assert_scenario_passes(...)``.
  3. Record the canonical transcript by running once with
     ``STM32_EVAL_MODE=record pytest tests/eval/test_stm32build_eval.py::TestStm32BuildEval::test_<name> -m eval -v``.
     Costs ~$0.04/scenario at Sonnet 4.6.
  4. Open the recorded
     ``tests/fixtures/eval/F-EVAL-BUILD-<NAME>/transcript.jsonc``
     and eyeball: did Claude pick the right tool call? Is the
     final_text on-topic? If not, adjust the prompt or the
     system_prompt in LiveDriver and re-record.
  5. Commit both the test file AND the transcript. From then on,
     default ``pytest -m eval`` reads the transcript (replay mode);
     no API tokens spent on every run.

Designing ``expected_tool_calls``:

  - Match on the **deterministic** parts: the substrate CLI verb
    (``stm32\\s+build\\b``) is reliable across Claude runs. Arg ORDER
    is not deterministic — Claude may reorder kwargs.
  - For multi-constraint Bash arg matching against the single
    ``command`` key, use positive lookaheads:
        r"(?=.*stm32\\s+build\\b)(?=.*--config\\s+Release)"
    Each ``(?=...)`` matches any-order substring presence.
  - Don't over-pin. Asserting "Claude said exactly --config=Release"
    breaks when Claude uses --config Release (space-separated). Match
    the semantic, not the syntax.

Designing ``expected_final_text_contains``:

  - Pick **substantive cues** that prove Claude understood the
    outcome — the word "build" almost always appears when Claude
    reports a build result; "Release" / "Debug" when it confirms a
    config choice; ".elf" / "artifact" when it surfaces the output
    path. Avoid Claude's stylistic phrasing.
  - Substring match is case-sensitive; use lowercase common nouns
    ("build", "artifact") if the casing varies in Claude's output.
  - Bench-tolerance: tool-call matches are the strongest signal; keep
    final-text assertions loose so they don't break when the build
    actually fails (legitimate result) or when Claude paraphrases.

Disambiguation:

  Each scenario sets ``cwd`` to an F-PROJ fixture directory so Claude
  has unambiguous "this is an STM32 CubeIDE project" context even when
  the user prompt is plain NL without an "stm32" keyword. The fixture
  carries a ``stm32-project.jsonc`` descriptor that ``stm32 build``
  auto-discovers per R-002.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


# Canonical L476RG-BLINKY F-PROJ — sets unambiguous STM32 + CubeIDE
# context for free-form NL prompts. Resolves at import time so the
# Path is concrete by the time LiveDriver / RecordingDriver consumes
# it. ReplayDriver ignores cwd (it's reading a stored transcript).
PROJECT_CWD = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "projects"
    / "F-PROJ-NUCLEO-L476RG"
)


# ---------------------------------------------------------------------------
# Scenario 1 — default build (autodiscovered project)
# ---------------------------------------------------------------------------

BUILD_DEFAULT = EvalScenario(
    name="F-EVAL-BUILD-DEFAULT",
    user_prompt="Build my CubeIDE project.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude must call `stm32 build`. The
            # project path is autodiscovered from cwd / descriptor per
            # stm32build.md's "Base build" subcommand map.
            args_contains={"command": r"stm32\s+build\b"},
        ),
    ),
    # "build" is the domain word — Claude reporting on a build
    # operation will use it whether the build succeeded or failed.
    expected_final_text_contains=("build",),
)


# ---------------------------------------------------------------------------
# Scenario 2 — clean rebuild (--clean flag)
# ---------------------------------------------------------------------------

BUILD_CLEAN = EvalScenario(
    name="F-EVAL-BUILD-CLEAN",
    user_prompt="Do a clean rebuild — wipe the previous artifacts first.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints (any-order via lookaheads):
            #   1. verb is `stm32 build` (NOT `stm32 clean` — there is
            #      no such subcommand; clean is a flag).
            #   2. --clean flag is present.
            args_contains={
                "command": r"(?=.*stm32\s+build\b)(?=.*--clean)",
            },
        ),
    ),
    expected_final_text_contains=("build",),
)


# ---------------------------------------------------------------------------
# Scenario 3 — Release configuration (--config Release)
# ---------------------------------------------------------------------------

BUILD_RELEASE = EvalScenario(
    name="F-EVAL-BUILD-RELEASE",
    user_prompt="Build the project with the Release configuration.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints:
            #   1. verb is `stm32 build`.
            #   2. `--config Release` — case-sensitive (CubeIDE config
            #      names are capitalized). Tolerates `--config=Release`
            #      and `--config Release` via the `\s*=?\s*` bridge.
            args_contains={
                "command": (
                    r"(?=.*stm32\s+build\b)"
                    r"(?=.*--config\s*=?\s*Release\b)"
                ),
            },
        ),
    ),
    # Claude usually confirms the configuration choice by echoing
    # "Release" in the summary. "build" alone would also pass — kept
    # the stricter cue here because it validates Claude carried the
    # config name through.
    expected_final_text_contains=("Release",),
)


# ---------------------------------------------------------------------------
# Test class — one test per scenario
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestStm32BuildEval:
    """Default replay mode: reads each scenario's transcript from disk.

    To run live (costs ~$0.04 each):
        STM32_EVAL_MODE=live pytest tests/eval/test_stm32build_eval.py -m eval -v

    To re-record canonical transcripts after a substrate or model
    change (writes to tests/fixtures/eval/F-EVAL-BUILD-*/transcript.jsonc):
        STM32_EVAL_MODE=record pytest tests/eval/test_stm32build_eval.py -m eval -v
    """

    def test_build_default(self, eval_driver) -> None:
        """User asks 'Build my CubeIDE project.' → Claude calls
        ``stm32 build`` with no args (project autodiscovered from
        cwd) and reports the outcome."""
        result = eval_driver.run(BUILD_DEFAULT)
        assert_scenario_passes(result, BUILD_DEFAULT)

    def test_build_clean(self, eval_driver) -> None:
        """User asks for a clean rebuild → Claude calls
        ``stm32 build --clean`` (NOT a non-existent `stm32 clean`
        subcommand) and reports the outcome."""
        result = eval_driver.run(BUILD_CLEAN)
        assert_scenario_passes(result, BUILD_CLEAN)

    def test_build_release_config(self, eval_driver) -> None:
        """User asks for a Release-configuration build → Claude calls
        ``stm32 build --config Release`` and confirms the config
        choice in its summary."""
        result = eval_driver.run(BUILD_RELEASE)
        assert_scenario_passes(result, BUILD_RELEASE)
