"""Eval tests for the ``/stm32prog`` slash-command surface.

Validates that natural-language prompts about probe discovery, memory
reads, and target control route through Claude into the substrate's
``stm32 prog ...`` CLI correctly. Per-prompt fixtures live under
``tests/fixtures/eval/F-EVAL-PROG-*/transcript.jsonc``.

Layout convention (one file per slash command):

  tests/eval/test_stm32prog_eval.py    (this file)    — /stm32prog
  tests/eval/test_stm32build_eval.py                  — /stm32build
  tests/eval/test_stm32debug_eval.py                  — /stm32debug
  tests/eval/test_stm32project_eval.py                — /stm32project
  tests/eval/test_stm32agent_eval.py                  — /stm32agent + VCP

Workflow for adding a new scenario:

  1. Define ``EvalScenario(name="F-EVAL-PROG-<NAME>", ...)`` at module
     scope with realistic expectations (loose enough to survive Claude's
     non-determinism; strict enough to catch real regressions).
  2. Add a one-line ``test_<name>`` method calling
     ``eval_driver.run(scenario)`` + ``assert_scenario_passes(...)``.
  3. Record the canonical transcript by running once with
     ``STM32_EVAL_MODE=record pytest tests/eval/test_stm32prog_eval.py::
     TestStm32ProgEval::test_<name> -m eval -v``. Costs ~$0.04/scenario
     at Sonnet 4.6.
  4. Open the recorded ``tests/fixtures/eval/F-EVAL-PROG-<NAME>/
     transcript.jsonc`` and eyeball: did Claude pick the right tool
     call? Is the final_text on-topic? If not, adjust the prompt or
     the system_prompt in LiveDriver and re-record.
  5. Commit both the test file AND the transcript. From then on,
     default ``pytest -m eval`` reads the transcript (replay mode);
     no API tokens spent on every run.

Designing ``expected_tool_calls``:

  - Match on the **deterministic** parts: the substrate CLI verb
    (``stm32\\s+prog\\s+<verb>``) is reliable across Claude runs.
    Arg ORDER is not deterministic — Claude may reorder kwargs.
  - For multi-constraint Bash arg matching against the single
    ``command`` key, use positive lookaheads:
        r"(?=.*stm32\\s+prog\\s+read-mem)(?=.*0x0*8000000)(?=.*256)"
    Each ``(?=...)`` matches any-order substring presence.
  - Don't over-pin. Asserting "Claude said exactly --addr=X --size=Y"
    breaks when Claude uses --addr X (space-separated). Match the
    semantic, not the syntax.

Designing ``expected_final_text_contains``:

  - Pick **substantive cues** that prove Claude understood the
    outcome — board name + serial number for discovery; the address
    + a hex byte for memory reads; "reset issued" or similar for
    target control. Avoid Claude's stylistic phrasing.
  - Substring match is case-sensitive; use lowercase common nouns
    ("probe", "flash", "byte") if the casing varies in Claude's
    output.
  - Bench-tolerance: if a scenario's expected text depends on what's
    physically attached, use a domain-name substring ("ST-LINK")
    rather than a board-specific match ("NUCLEO-L476RG").
"""

from __future__ import annotations

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


# ---------------------------------------------------------------------------
# Scenario 1 — list probes (zero-arg discovery)
# ---------------------------------------------------------------------------

LIST_PROBES = EvalScenario(
    name="F-EVAL-PROG-LIST-PROBES",
    user_prompt="What ST-LINK probes are attached?",
    allowed_tools=("Bash",),
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude must call `stm32 prog list-probes`.
            # Whitespace tolerance via \s+ in case Claude uses tabs.
            args_contains={"command": r"stm32\s+prog\s+list-probes"},
        ),
    ),
    # Bench-state-tolerant: any attached probe yields "ST-LINK" in the
    # final summary. If you want stricter, change to "NUCLEO-L476RG"
    # — but then re-record any time the bench board swaps.
    expected_final_text_contains=("ST-LINK",),
)


# ---------------------------------------------------------------------------
# Scenario 2 — read 256 bytes from flash@0x08000000 (multi-arg structured)
# ---------------------------------------------------------------------------

READ_FLASH_256 = EvalScenario(
    name="F-EVAL-PROG-READ-FLASH-256",
    user_prompt="Read 256 bytes from flash starting at 0x08000000 and tell me what's there.",
    allowed_tools=("Bash",),
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Three constraints OR'd into a single positive-lookahead
            # regex (each (?=...) is an any-order substring check):
            #   1. verb must be `stm32 prog read-mem` (or read-memory).
            #   2. address must be 0x08000000 (allow leading-zero drop).
            #   3. size must be 256 (Claude may use --size 256 or 256).
            args_contains={
                "command": (
                    r"(?=.*stm32\s+prog\s+read-mem)"
                    r"(?=.*0x0*8000000)"
                    r"(?=.*\b256\b)"
                ),
            },
        ),
    ),
    # Substantive cues that Claude reported the bytes: should mention
    # "byte" or "flash" or a hex digit pattern. Loose — Claude may
    # render the dump in many ways.
    expected_final_text_contains=("byte",),
)


# ---------------------------------------------------------------------------
# Scenario 3 — hard reset (boolean-flag-like intent)
# ---------------------------------------------------------------------------

RESET_HARD = EvalScenario(
    name="F-EVAL-PROG-RESET-HARD",
    user_prompt="Issue a hardware reset to the attached board.",
    allowed_tools=("Bash",),
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints:
            #   1. `stm32 prog reset` verb.
            #   2. --hard flag (the user said "hardware reset" which
            #      maps to the substrate's --hard flag, not the default
            #      soft reset).
            args_contains={
                "command": r"(?=.*stm32\s+prog\s+reset)(?=.*--hard)",
            },
        ),
    ),
    # Either "reset" alone OR "hard" — Claude should confirm the
    # action landed. Some completions just print the JSON result; the
    # "reset" substring should appear in either flavor.
    expected_final_text_contains=("reset",),
)


# ---------------------------------------------------------------------------
# Test class — one test per scenario
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestStm32ProgEval:
    """Default replay mode: reads each scenario's transcript from disk.

    To run live (costs ~$0.04 each):
        STM32_EVAL_MODE=live pytest tests/eval/test_stm32prog_eval.py -m eval -v

    To re-record canonical transcripts after a substrate or model
    change (writes to tests/fixtures/eval/F-EVAL-PROG-*/transcript.jsonc):
        STM32_EVAL_MODE=record pytest tests/eval/test_stm32prog_eval.py -m eval -v
    """

    def test_list_probes(self, eval_driver) -> None:
        """User asks 'what probes are attached?' → Claude calls
        ``stm32 prog list-probes`` and reports the board(s)."""
        result = eval_driver.run(LIST_PROBES)
        assert_scenario_passes(result, LIST_PROBES)

    def test_read_flash_256_bytes(self, eval_driver) -> None:
        """User asks for a specific memory read → Claude maps the
        natural-language args (256 bytes, 0x08000000) to the substrate
        CLI's positional/flag form."""
        result = eval_driver.run(READ_FLASH_256)
        assert_scenario_passes(result, READ_FLASH_256)

    def test_reset_hard(self, eval_driver) -> None:
        """User says 'hardware reset' → Claude knows to pass --hard,
        not just `stm32 prog reset` (which defaults to soft)."""
        result = eval_driver.run(RESET_HARD)
        assert_scenario_passes(result, RESET_HARD)
