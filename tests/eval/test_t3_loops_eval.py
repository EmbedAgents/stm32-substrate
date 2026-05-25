"""Eval tests for the Wave-3a non-compound T3 prompts (RES-031).

Seven T3 prompts ship in Pass 1 (ADR-003) and route across three slash
commands — but they share the **T3 Claude-in-loop fix discipline**
(the T3 fix-discipline prelude), so they live together here:

    B-021    /stm32build    build-fix loop, then confirm on-target (killer)
    DBG-008  /stm32debug    stack overflow → bump stack → retry
    DBG-009  /stm32debug    malloc failure → bump heap → retry
    DIAG-019 /stm32debug    classify crash: memory-corruption vs interrupt (killer)
    DIAG-020 /stm32debug    cache-corruption classify (killer)
    VCP-004  /stm32agent    garbage output → match host/device UART config
    VCP-005  /stm32agent    slow output → raise baud, validate

What's different from the atomic / DIAG level-(a) evals
-------------------------------------------------------

These are **not single recipe-CLI verbs**. Claude is the loop controller
(RES-026): it composes substrate single-shot primitives (build / flash /
debug-read / fault-decode), reads the raw captures, edits source, and
drives the next iteration. Two consequences for the assertions:

  1. **Tool-call shape is partly non-deterministic.** The *diagnostic*
     legs (DIAG-019/020) map to concrete recipe CLIs
     (``decode-hardfault`` / ``snapshot`` / ``read-peripheral``) and are
     asserted tightly. The *fix-loop* legs (DBG-008/009 gather) may be
     composed via a **Python ``DebugSession`` heredoc** rather than
     recipe-CLI calls (per RES-026, long-lived breakpoints/reads belong
     in Python) — so their tool-call regex accepts *either* the CLI verb
     *or* the ``DebugSession`` Python token. Re-calibrate these against a
     live record if Claude's composition style shifts.
  2. **Final-text cue carries more weight** than for atomics: the loop's
     substantive vocabulary ("stack", "heap", "fault", "cache", "baud",
     "build") is the cross-run-stable signal that Claude understood the
     job. Substring match, case-sensitive (per ``assert_scenario_passes``).

Device-conditional verify (prelude §4): for the fix-loops that reflash
(B-021, DBG-008/009, VCP-005), a clean build is *not* "resolved" when a
device is attached — the loop must flash + re-observe. The stubs show the
on-target verify leg; a no-device live record degrades it to a "built
clean, not verified on silicon" note.

DIAG-020 on the L476 bench: the L476 is Cortex-M4 (no L1 cache), so the
correct level outcome is ``cache_present: false`` / ``not_applicable``
(redirect to MPU attributes + barriers). That *is* a valid, recordable
classification — it validates Claude recognises the cacheless-core
early-return rather than hallucinating cache analysis. A cache-*present*
record needs a Cortex-M7 board (H7 / H7RS) — queued for when an
M7 F-PROJ descriptor lands.

Transcripts: hand-authored stubs under
``tests/fixtures/eval/F-EVAL-{B021,DBG008,...}/transcript.jsonc`` keep
replay green. Re-record live on a bench (attached NUCLEO-L476RG; ~$0.07+
each — multi-turn loops cost more than atomics):

    STM32_EVAL_MODE=record pytest tests/eval/test_t3_loops_eval.py -m eval -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


PROJECT_CWD = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "projects"
    / "F-PROJ-NUCLEO-L476RG"
)


# ---------------------------------------------------------------------------
# B-021 — build-fix loop, then confirm on-target (killer feature)
# ---------------------------------------------------------------------------

B021_BUILD_FIX = EvalScenario(
    name="F-EVAL-B021-BUILD-FIX",
    user_prompt="The build's broken — get it compiling, then flash it and prove it runs.",
    allowed_tools=("Bash", "Edit", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        # The build is intrinsic to the loop and reliably goes through
        # the `stm32 build` CLI verb (B-001/B-002).
        ToolCallMatch(name="Bash", args_contains={"command": r"(?=.*stm32\s+build\b)"}),
    ),
    expected_final_text_contains=("build",),
)


# ---------------------------------------------------------------------------
# DBG-008 — stack overflow → bump stack → retry
# ---------------------------------------------------------------------------

DBG008_STACK_OVERFLOW = EvalScenario(
    name="F-EVAL-DBG008-STACK-OVERFLOW",
    user_prompt="I think we're blowing the stack — check for an overflow, and if you find one, bump the stack and retry.",
    allowed_tools=("Bash", "Edit", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        # Gather may be a recipe-CLI debug read OR a Python DebugSession
        # heredoc (RES-026). Accept either; the `stm32 build` rebuild leg
        # is a fallback signal.
        ToolCallMatch(
            name="Bash",
            args_contains={
                "command": r"(?=.*(?:stm32\s+debug|DebugSession|stm32\s+build))"
            },
        ),
    ),
    expected_final_text_contains=("stack",),
)


# ---------------------------------------------------------------------------
# DBG-009 — malloc failure → bump heap → retry
# ---------------------------------------------------------------------------

DBG009_MALLOC_FAILURE = EvalScenario(
    name="F-EVAL-DBG009-MALLOC-FAILURE",
    user_prompt="We're running out of heap — check whether malloc is failing, and grow the heap until allocations succeed.",
    allowed_tools=("Bash", "Edit", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            args_contains={
                "command": r"(?=.*(?:stm32\s+debug|DebugSession|stm32\s+build))"
            },
        ),
    ),
    expected_final_text_contains=("heap",),
)


# ---------------------------------------------------------------------------
# DIAG-019 — classify crash: memory-corruption vs interrupt (killer)
# ---------------------------------------------------------------------------

DIAG019_CLASSIFY_CRASH = EvalScenario(
    name="F-EVAL-DIAG019-CLASSIFY-CRASH",
    # "with the debug session" disambiguates the gdb path from the
    # binary-only `stm32 prog -hf` path (M-012).
    user_prompt="The firmware crashed. With the debug session available, classify it — is this memory corruption or an interrupt problem?",
    allowed_tools=("Bash", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        # Read-only gather: the evidence bundle comes from the
        # decode-hardfault + snapshot recipe CLIs (concrete verbs).
        ToolCallMatch(
            name="Bash",
            args_contains={
                "command": r"(?=.*stm32\s+debug\s+(?:decode-hardfault|snapshot)\b)"
            },
        ),
    ),
    expected_final_text_contains=("fault",),
)


# ---------------------------------------------------------------------------
# DIAG-020 — cache-corruption classify (killer); L476 = cacheless path
# ---------------------------------------------------------------------------

DIAG020_CACHE_CORRUPTION = EvalScenario(
    name="F-EVAL-DIAG020-CACHE-CORRUPTION",
    user_prompt="My DMA buffer is reading stale data — I think it's a cache coherency bug. Debug it.",
    allowed_tools=("Bash", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        # On the L476 (Cortex-M4) the gather is the core/cache check —
        # either `stm32 prog cores` (D-007 family) or a debug
        # read-peripheral SCB (CCR) read. Accept either.
        ToolCallMatch(
            name="Bash",
            args_contains={
                "command": r"(?=.*stm32\s+(?:prog\s+cores|debug))"
            },
        ),
    ),
    expected_final_text_contains=("cache",),
)


# ---------------------------------------------------------------------------
# VCP-004 — garbage output → match host/device UART config
# ---------------------------------------------------------------------------

VCP004_GARBAGE_OUTPUT = EvalScenario(
    name="F-EVAL-VCP004-GARBAGE-OUTPUT",
    user_prompt="The serial terminal is spitting garbage — read the device's UART config and fix the mismatch so it's readable.",
    allowed_tools=("Bash", "Edit", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        # Step 1 reads the device-side USART config live via the
        # read-peripheral recipe — a concrete, reliable verb.
        ToolCallMatch(
            name="Bash",
            args_contains={
                "command": r"(?=.*stm32\s+debug\s+read-peripheral\b)(?=.*USART)"
            },
        ),
    ),
    expected_final_text_contains=("baud",),
)


# ---------------------------------------------------------------------------
# VCP-005 — slow output → raise baud, validate
# ---------------------------------------------------------------------------

VCP005_RAISE_BAUD = EvalScenario(
    name="F-EVAL-VCP005-RAISE-BAUD",
    user_prompt="Serial is too slow — crank it up to the fastest baud both ends support, then make sure it still works.",
    allowed_tools=("Bash", "Edit", "Read"),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        # The device-side baud change rebuilds firmware (`stm32 build`)
        # and/or the host reader is reopened (`stm32 vcp`). Accept either.
        ToolCallMatch(
            name="Bash",
            args_contains={"command": r"(?=.*(?:stm32\s+build|stm32\s+vcp))"},
        ),
    ),
    expected_final_text_contains=("baud",),
)


# ---------------------------------------------------------------------------
# Test class — one test per T3 prompt
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestT3LoopsEval:
    """Wave-3a non-compound T3 fix-loops + classifiers (RES-031).

    Default replay mode reads each transcript from disk. Live / record:
        STM32_EVAL_MODE=live   pytest tests/eval/test_t3_loops_eval.py -m eval -v
        STM32_EVAL_MODE=record pytest tests/eval/test_t3_loops_eval.py -m eval -v
    """

    def test_b021_build_fix_loop(self, eval_driver) -> None:
        """B-021: 'get it compiling and prove it runs' → Claude runs the
        build-fix loop (`stm32 build` → read errors → edit → rebuild) and,
        with a device attached, flashes + re-observes before declaring it
        resolved (prelude §4)."""
        result = eval_driver.run(B021_BUILD_FIX)
        assert_scenario_passes(result, B021_BUILD_FIX)

    def test_dbg008_stack_overflow(self, eval_driver) -> None:
        """DBG-008: gather stack bounds + high-water (DebugSession or
        recipe CLI), detect overflow, bump `_Min_Stack_Size`, rebuild +
        reflash, re-gather to confirm margin."""
        result = eval_driver.run(DBG008_STACK_OVERFLOW)
        assert_scenario_passes(result, DBG008_STACK_OVERFLOW)

    def test_dbg009_malloc_failure(self, eval_driver) -> None:
        """DBG-009: gather heap model + free bytes, detect malloc failure,
        bump `_Min_Heap_Size` / `configTOTAL_HEAP_SIZE`, rebuild +
        reflash, re-gather. Flags a leak (free-bytes trending to zero)
        rather than looping forever."""
        result = eval_driver.run(DBG009_MALLOC_FAILURE)
        assert_scenario_passes(result, DBG009_MALLOC_FAILURE)

    def test_diag019_classify_crash(self, eval_driver) -> None:
        """DIAG-019: gather the fault bundle (decode-hardfault + snapshot +
        exception context) and classify memory-corruption vs
        interrupt-related. Read-only; the verdict is Claude's from the
        evidence (ADR-004 — substrate encodes no rules)."""
        result = eval_driver.run(DIAG019_CLASSIFY_CRASH)
        assert_scenario_passes(result, DIAG019_CLASSIFY_CRASH)

    def test_diag020_cache_corruption(self, eval_driver) -> None:
        """DIAG-020: on the L476 (Cortex-M4, no L1 cache) the correct
        outcome is cache_present:false / not_applicable — redirect to MPU
        attributes + barriers. Validates Claude recognises the
        cacheless-core early-return. (Cache-present record needs an M7
        board.)"""
        result = eval_driver.run(DIAG020_CACHE_CORRUPTION)
        assert_scenario_passes(result, DIAG020_CACHE_CORRUPTION)

    def test_vcp004_garbage_output(self, eval_driver) -> None:
        """VCP-004: read the device USART config (`read-peripheral USART2`
        → baud/parity/framing), compare against the host reader, reopen
        the host to match (fix_side: host, no reflash), re-tail to
        validate."""
        result = eval_driver.run(VCP004_GARBAGE_OUTPUT)
        assert_scenario_passes(result, VCP004_GARBAGE_OUTPUT)

    def test_vcp005_raise_baud(self, eval_driver) -> None:
        """VCP-005: compute the highest baud both ends support, edit the
        firmware baud + rebuild + reflash, reopen the host reader at the
        new rate, validate (step down on garbage, bounded by
        t3.max_iterations)."""
        result = eval_driver.run(VCP005_RAISE_BAUD)
        assert_scenario_passes(result, VCP005_RAISE_BAUD)
