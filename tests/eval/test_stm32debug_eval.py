"""Eval tests for the ``/stm32debug`` slash-command surface.

Validates that natural-language prompts about debug operations route
through Claude into the substrate's ``stm32 debug ...`` recipe-CLI
correctly. Per-prompt fixtures live under
``tests/fixtures/eval/F-EVAL-DEBUG-*/transcript.jsonc``.

Layout convention (one file per slash command):

  tests/eval/test_stm32prog_eval.py                    — /stm32prog
  tests/eval/test_stm32build_eval.py                   — /stm32build
  tests/eval/test_stm32debug_eval.py   (this file)     — /stm32debug
  tests/eval/test_stm32project_eval.py                 — /stm32project
  tests/eval/test_stm32agent_eval.py                   — /stm32agent + VCP

Recipe-CLI model (per RES-026, 2026-05-21):

  Every ``stm32 debug ...`` invocation is one-shot — spawns a fresh
  gdbserver + arm-gdb, performs a complete composed operation, tears
  down, emits JSON. No cross-invocation session continuity. Recipe
  subcommands map 1:1 to prompts Claude actually invokes:

    start ELF                   DBG-001 / DBG-003 / DBG-012
    svd-path DEVICE             D-008 input
    check-variable --at ...     DBG-004 (bp + run + compare_variable)
    check-register --at ...     DBG-005 (bp + run + compare_register)
    read-registers              DBG-006
    read-peripheral NAME        DBG-007
    read-memory --address ...   raw read primitive
    callstack [--full]          raw read primitive
    snapshot [--include-*]      DIAG-021
    decode-hardfault            DIAG-001 gdb path

  Stateful multi-step workflows (set N breakpoints, run, hit, inspect,
  set more, continue) have no CLI surface — Python via Bash heredoc is
  the canonical path. Eval scenarios validate the Claude → recipe-CLI
  mapping; stateful Python composition is exercised by other layers.

Workflow for adding a new scenario:

  1. Define ``EvalScenario(name="F-EVAL-DEBUG-<NAME>", ...)`` at module
     scope with realistic expectations (loose enough to survive
     Claude's non-determinism; strict enough to catch real regressions).
  2. Add a one-line ``test_<name>`` method calling
     ``eval_driver.run(scenario)`` + ``assert_scenario_passes(...)``.
  3. Record the canonical transcript by running once with
     ``STM32_EVAL_MODE=record pytest tests/eval/test_stm32debug_eval.py::TestStm32DebugEval::test_<name> -m eval -v``.
     Costs ~$0.04/scenario at Sonnet 4.6.
  4. Open the recorded
     ``tests/fixtures/eval/F-EVAL-DEBUG-<NAME>/transcript.jsonc``
     and eyeball: did Claude pick the right recipe subcommand? Is the
     final_text on-topic? If not, adjust the prompt or the
     ``system_prompt`` in LiveDriver and re-record.
  5. Commit both the test file AND the transcript. From then on,
     default ``pytest -m eval`` reads the transcript (replay mode);
     no API tokens spent on every run.

Designing ``expected_tool_calls``:

  - Match on the **deterministic** parts: the substrate CLI verb
    (``stm32\\s+debug\\s+<recipe>\\b``) is reliable across Claude
    runs. Arg ORDER is not deterministic — Claude may reorder flags.
  - For multi-constraint Bash arg matching against the single
    ``command`` key, use positive lookaheads:
        r"(?=.*stm32\\s+debug\\s+check-variable\\b)(?=.*--at\\s+main)"
    Each ``(?=...)`` matches any-order substring presence.
  - Don't over-pin. Asserting an exact ELF path breaks when Claude
    autodiscovers from the descriptor.

Designing ``expected_final_text_contains``:

  - **Substring match, NOT regex** (per ``assert_scenario_passes``).
    Plain words only — no alternation, no regex metacharacters.
  - Pick **substantive cues** that prove Claude understood the outcome
    — "session" / "halted" for lifecycle; ".svd" for SVD lookup;
    "RCC" / "register" for peripheral reads; "fault" for hardfault
    decode. Lowercase common nouns survive Claude's stylistic phrasing.
  - Bench-tolerance: tool-call matches are the strongest signal; keep
    final-text assertions loose so they don't break when the operation
    actually fails (legitimate result) or when Claude paraphrases.

Disambiguation:

  Each lifecycle scenario sets ``cwd`` to an F-PROJ fixture so Claude
  has unambiguous "this is an STM32 project" context even when the
  user prompt is plain NL without an "stm32" keyword. The fixture's
  ``stm32-project.jsonc`` descriptor lets ``stm32 debug start``
  autodiscover the ELF per R-002.

  The N6 scenario reuses an identical user prompt to the default
  start scenario; the only differentiator is ``cwd`` pointing at an
  N6 F-PROJ. Per the design call ratified 2026-05-21, the
  ``firmware.device_family`` field in the descriptor + the project
  cwd is what tells Claude to add ``--n6-dev-mode``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


# Canonical L476RG-BLINKY F-PROJ — sets unambiguous STM32 + ST-LINK
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


# Canonical N6570-DK F-PROJ — same role as PROJECT_CWD but points at an
# STM32N6 project so the descriptor's ``firmware.device_family`` cues
# Claude into adding ``--n6-dev-mode`` (DBG-012). Forward-looking
# fixture path; replay mode skips cleanly until the transcript exists.
PROJECT_N6_CWD = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "n6-projects"
    / "x-cube-n6-ai-multi-pose-estimation"
)


# ---------------------------------------------------------------------------
# Scenario 1 — DBG-001 default start (halt at entry; autodiscovered ELF)
# ---------------------------------------------------------------------------

DEBUG_START = EvalScenario(
    name="F-EVAL-DEBUG-START",
    user_prompt="Start a debug session.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude calls `stm32 debug start`. The
            # ELF path is autodiscovered from cwd / descriptor per
            # stm32debug.md's "Session lifecycle" map. ``--no-halt``
            # is the opt-out; default is halt-at-entry.
            args_contains={"command": r"stm32\s+debug\s+start\b"},
        ),
    ),
    # "session" is the broadest cue — Claude reporting on a debug
    # start will use it whether the session came up or failed.
    expected_final_text_contains=("session",),
)


# ---------------------------------------------------------------------------
# Scenario 2 — DBG-003 attach to a running target (--no-halt)
# ---------------------------------------------------------------------------

DEBUG_ATTACH = EvalScenario(
    name="F-EVAL-DEBUG-ATTACH",
    # Lead with "running" twice + the explicit "while it continues running"
    # cue so Claude tends to echo the word in its summary. Earlier
    # phrasing ("Attach to the running target without halting it.") got
    # paraphrased to "attach without halting" with no "running" echo.
    user_prompt=(
        "The target is currently running. Attach a debug session "
        "without halting it — let it keep running."
    ),
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints (any-order via lookaheads):
            #   1. verb is `stm32 debug start` (attach_running routes
            #      through start with --no-halt; there is no
            #      `stm32 debug attach`).
            #   2. --no-halt flag is present (NB: NOT `halt=False`).
            args_contains={
                "command": (
                    r"(?=.*stm32\s+debug\s+start\b)"
                    r"(?=.*--no-halt\b)"
                ),
            },
        ),
    ),
    expected_final_text_contains=("running",),
)


# ---------------------------------------------------------------------------
# Scenario 3 — DBG-012 N6 dev-mode start (cwd induces --n6-dev-mode)
# ---------------------------------------------------------------------------

DEBUG_N6_START = EvalScenario(
    name="F-EVAL-DEBUG-N6-START",
    user_prompt="Start a debug session.",
    allowed_tools=("Bash",),
    # cwd points at the N6 fixture; the descriptor's
    # ``firmware.device_family`` cues Claude. Same terse user prompt as
    # Scenario 1 — the only differentiator is cwd. This validates the
    # safety-check path: Claude correctly invokes `stm32 debug start`,
    # the substrate refuses with ``ConfigurationError(
    # "descriptor declares STM32N6 ... requires --n6-dev-mode")``,
    # Claude surfaces the BOOT-switch hint to the human. Claude
    # does **not** silently flip ``--n6-dev-mode`` on its own because
    # the BOOT switch is human-controlled hardware state. The
    # confirmed-route is tested by ``DEBUG_N6_START_CONFIRMED`` below.
    cwd=PROJECT_N6_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude attempts `stm32 debug start`. The
            # substrate refuses (per design); Claude surfaces the BOOT
            # hint rather than re-invoking.
            args_contains={"command": r"stm32\s+debug\s+start\b"},
        ),
    ),
    # Claude's safety reaction mentions BOOT (or the substrate's hint
    # bubbles through). Case-sensitive substring match.
    expected_final_text_contains=("BOOT",),
)


# ---------------------------------------------------------------------------
# Scenario 3b — DBG-012 N6 dev-mode start, BOOT confirmed in prompt
# ---------------------------------------------------------------------------

DEBUG_N6_START_CONFIRMED = EvalScenario(
    name="F-EVAL-DEBUG-N6-START-CONFIRMED",
    # Human-supplied BOOT confirmation is the prompt cue that unlocks
    # the ``--n6-dev-mode`` flag. The substrate refuses without it
    # (descriptor declares N6); with the prompt asserting the physical
    # state, Claude carries that confirmation through to the CLI flag.
    user_prompt=(
        "The BOOT switch on the N6 board is set to dev mode. "
        "Start a debug session."
    ),
    allowed_tools=("Bash",),
    cwd=PROJECT_N6_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints:
            #   1. verb is `stm32 debug start`.
            #   2. --n6-dev-mode flag is present (the human's prompt
            #      confirmation translates to the substrate-required
            #      flag).
            args_contains={
                "command": (
                    r"(?=.*stm32\s+debug\s+start\b)"
                    r"(?=.*--n6-dev-mode\b)"
                ),
            },
        ),
    ),
    # ``n6-dev-mode`` literal echo confirms Claude carried the flag
    # name (and the BOOT-switch semantic) through to the summary.
    expected_final_text_contains=("n6-dev-mode",),
)


# ---------------------------------------------------------------------------
# Scenario 4 — D-008 SVD lookup (svd-path subcommand)
# ---------------------------------------------------------------------------

SVD_PATH_L476 = EvalScenario(
    name="F-EVAL-DEBUG-SVD-PATH-L476",
    user_prompt="Where is the SVD file for the STM32L476RG?",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints:
            #   1. verb is `stm32 debug svd-path`.
            #   2. device name STM32L476 appears in the command.
            #      SvdDb canonicalisation strips the package suffix, so
            #      STM32L476 / STM32L476RG both resolve the same.
            args_contains={
                "command": (
                    r"(?=.*stm32\s+debug\s+svd-path\b)"
                    r"(?=.*STM32L476)"
                ),
            },
        ),
    ),
    # SvdDb returns a path ending in `.svd`; Claude typically echoes
    # the resolved filename in its summary.
    expected_final_text_contains=(".svd",),
)


# ---------------------------------------------------------------------------
# Scenario 5 — DBG-004 check-variable at breakpoint
# ---------------------------------------------------------------------------

CHECK_VARIABLE_AT_MAIN = EvalScenario(
    name="F-EVAL-DEBUG-CHECK-VAR-AT-MAIN",
    user_prompt=(
        "Check that the variable uart_buf_count is 0 when execution reaches main."
    ),
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Four constraints any-order:
            #   1. verb is `stm32 debug check-variable`.
            #   2. --at carries `main` (location).
            #   3. --var carries the variable name.
            #   4. --expected carries 0.
            args_contains={
                "command": (
                    r"(?=.*stm32\s+debug\s+check-variable\b)"
                    r"(?=.*--at\s+\S*main\b)"
                    r"(?=.*--var\s+uart_buf_count\b)"
                    r"(?=.*--expected\s+0\b)"
                ),
            },
        ),
    ),
    # ComparisonResult JSON carries "matches" / "observed" / "expected"
    # — any reasonable Claude paraphrase uses "match" or echoes the
    # variable name.
    expected_final_text_contains=("uart_buf_count",),
)


# ---------------------------------------------------------------------------
# Scenario 6 — DBG-005 check-register at breakpoint
# ---------------------------------------------------------------------------

CHECK_REGISTER_AT_FN = EvalScenario(
    name="F-EVAL-DEBUG-CHECK-REG-AT-FN",
    # Phase-4 reword: "when SystemClock_Config returns" sent a rigorous
    # model hunting the actual return site (disasm, *addr breakpoints)
    # past the turn cap on BOTH lean and verbose steering — while the
    # assertion expects the entry-symbol form. Anchor at the symbol.
    user_prompt=(
        "Verify that register r0 equals 0x1 when execution reaches "
        "SystemClock_Config."
    ),
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            args_contains={
                "command": (
                    r"(?=.*stm32\s+debug\s+check-register\b)"
                    r"(?=.*--at\s+\S*SystemClock_Config\b)"
                    r"(?=.*--reg\s+r0\b)"
                    r"(?=.*--expected\s+(?:0x0*1\b|1\b))"
                ),
            },
        ),
    ),
    expected_final_text_contains=("r0",),
)


# ---------------------------------------------------------------------------
# Scenario 7 — DBG-007 peripheral inspect
# ---------------------------------------------------------------------------

READ_PERIPHERAL_RCC = EvalScenario(
    name="F-EVAL-DEBUG-READ-PERIPHERAL-RCC",
    user_prompt="Inspect the RCC registers on the attached target.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints:
            #   1. verb is `stm32 debug read-peripheral`.
            #   2. RCC appears as the peripheral name argument.
            args_contains={
                "command": (
                    r"(?=.*stm32\s+debug\s+read-peripheral\b)"
                    r"(?=.*\bRCC\b)"
                ),
            },
        ),
    ),
    # Claude echoes "RCC" when summarising the peripheral dump.
    expected_final_text_contains=("RCC",),
)


# ---------------------------------------------------------------------------
# Scenario 8 — DIAG-021 debug snapshot
# ---------------------------------------------------------------------------

SNAPSHOT = EvalScenario(
    name="F-EVAL-DEBUG-SNAPSHOT",
    user_prompt="Take a debug snapshot of the attached target.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude calls `stm32 debug snapshot`.
            # --include-peripheral flags are optional; substrate's
            # default set covers SCB at minimum.
            args_contains={"command": r"stm32\s+debug\s+snapshot\b"},
        ),
    ),
    # "snapshot" is the strongest domain cue. Substring match.
    expected_final_text_contains=("snapshot",),
)


# ---------------------------------------------------------------------------
# Scenario 9 — DIAG-001 gdb-path hardfault decode
# ---------------------------------------------------------------------------

DECODE_HARDFAULT = EvalScenario(
    name="F-EVAL-DEBUG-DECODE-HARDFAULT",
    user_prompt=(
        "The target appears to have hardfaulted. With the debug session "
        "available, decode the fault."
    ),
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude calls `stm32 debug decode-hardfault`.
            # NOT `stm32 prog hardfault` (which is the binary-only path
            # per M-012 dual-tool routing). The user prompt mentions
            # "debug session available" to disambiguate.
            args_contains={"command": r"stm32\s+debug\s+decode-hardfault\b"},
        ),
    ),
    # HardFaultDecode JSON carries "fault" vocabulary; Claude echoes.
    expected_final_text_contains=("fault",),
)


# ---------------------------------------------------------------------------
# Test class — one test per scenario
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestStm32DebugEval:
    """Default replay mode: reads each scenario's transcript from disk.

    To run live (costs ~$0.04 each):
        STM32_EVAL_MODE=live pytest tests/eval/test_stm32debug_eval.py -m eval -v

    To re-record canonical transcripts after a substrate or model
    change (writes to tests/fixtures/eval/F-EVAL-DEBUG-*/transcript.jsonc):
        STM32_EVAL_MODE=record pytest tests/eval/test_stm32debug_eval.py -m eval -v
    """

    def test_debug_start_default(self, eval_driver) -> None:
        """User asks 'Start a debug session.' → Claude calls
        ``stm32 debug start <elf>`` (ELF autodiscovered from cwd) and
        reports the lifecycle outcome."""
        result = eval_driver.run(DEBUG_START)
        assert_scenario_passes(result, DEBUG_START)

    def test_debug_attach_running(self, eval_driver) -> None:
        """User asks to attach without halting → Claude calls
        ``stm32 debug start <elf> --no-halt`` (NOT a non-existent
        `stm32 debug attach`; attach_running routes through `start`)."""
        result = eval_driver.run(DEBUG_ATTACH)
        assert_scenario_passes(result, DEBUG_ATTACH)

    def test_debug_n6_dev_mode(self, eval_driver) -> None:
        """User asks 'Start a debug session.' with cwd pointing at an
        N6 fixture → Claude invokes ``stm32 debug start``; substrate
        refuses (descriptor declares STM32N6, no --n6-dev-mode);
        Claude surfaces the BOOT-switch hint instead of silently
        flipping the flag. Validates the HIL safety-check path."""
        result = eval_driver.run(DEBUG_N6_START)
        assert_scenario_passes(result, DEBUG_N6_START)

    def test_debug_n6_dev_mode_confirmed(self, eval_driver) -> None:
        """User prompt explicitly confirms BOOT switch is in dev mode →
        Claude carries that confirmation through to
        ``stm32 debug start --n6-dev-mode``. Validates the routing
        path once the human supplies the physical-state confirmation
        the substrate's safety check requires."""
        result = eval_driver.run(DEBUG_N6_START_CONFIRMED)
        assert_scenario_passes(result, DEBUG_N6_START_CONFIRMED)

    def test_svd_path_lookup(self, eval_driver) -> None:
        """User asks 'Where is the SVD for STM32L476RG?' → Claude
        calls ``stm32 debug svd-path STM32L476RG`` and surfaces the
        resolved .svd path."""
        result = eval_driver.run(SVD_PATH_L476)
        assert_scenario_passes(result, SVD_PATH_L476)

    def test_check_variable_recipe(self, eval_driver) -> None:
        """User asks 'check that X is Y at Z' (DBG-004) → Claude calls
        ``stm32 debug check-variable --at Z --var X --expected Y`` (one
        composed-flow call; substrate handles bp + run + compare)."""
        result = eval_driver.run(CHECK_VARIABLE_AT_MAIN)
        assert_scenario_passes(result, CHECK_VARIABLE_AT_MAIN)

    def test_check_register_recipe(self, eval_driver) -> None:
        """User asks 'verify register X equals Y when Z' (DBG-005) →
        ``stm32 debug check-register --at Z --reg X --expected Y``."""
        result = eval_driver.run(CHECK_REGISTER_AT_FN)
        assert_scenario_passes(result, CHECK_REGISTER_AT_FN)

    def test_read_peripheral_rcc(self, eval_driver) -> None:
        """User asks 'inspect the RCC' (DBG-007) → ``stm32 debug
        read-peripheral RCC``. Single composed-flow call: start, halt,
        SVD-decoded dump, close."""
        result = eval_driver.run(READ_PERIPHERAL_RCC)
        assert_scenario_passes(result, READ_PERIPHERAL_RCC)

    def test_snapshot_recipe(self, eval_driver) -> None:
        """User asks for a debug snapshot (DIAG-021) → ``stm32 debug
        snapshot``. Composite registers + callstack + peripherals +
        disasm in one call."""
        result = eval_driver.run(SNAPSHOT)
        assert_scenario_passes(result, SNAPSHOT)

    def test_decode_hardfault_recipe(self, eval_driver) -> None:
        """User says 'decode the hardfault (debug session available)' →
        ``stm32 debug decode-hardfault`` (gdb path per M-012, NOT
        ``stm32 prog hardfault`` which is the binary-only path)."""
        result = eval_driver.run(DECODE_HARDFAULT)
        assert_scenario_passes(result, DECODE_HARDFAULT)
