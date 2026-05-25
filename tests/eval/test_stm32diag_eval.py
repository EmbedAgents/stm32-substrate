"""Eval tests for the DIAG-002…017 *level-(a)* diagnostic recipes.

These 14 prompts route through the **``/stm32debug``** slash command
(same surface as ``test_stm32debug_eval.py``) but form one coherent
family — the RES-030 *level-(a)* diagnostics — so they live in their
own file rather than bloating the lifecycle/recipe-primitive suite.

Level-(a) contract (RES-030 + ADR-004, "substrate captures, doesn't
interpret"):

  The substrate provides only the generic SVD-decoded raw read
  ``stm32 debug read-peripheral <NAME>`` (DBG-007 — coded + HW-proven
  on L476 RCC). Each DIAG question maps to a ``read-peripheral`` (or,
  for the vector-table case, a ``read-peripheral SCB`` + ``read-memory``
  pair); **Claude composes the register selection + the verdict.** There
  is no typed verdict method and no dedicated ``stm32 debug diag <name>``
  subcommand in v1 — that's level (b), deferred post-v1.

What these evals assert:

  - **Tool-call routing** is the strong signal: Claude picks the
    ``read-peripheral`` recipe and names the right peripheral. Matched
    via any-order ``(?=...)`` lookaheads against the single Bash
    ``command`` arg (arg/flag order is non-deterministic across Claude
    runs).
  - **Final-text cue** is loose (substring, case-sensitive, per
    ``assert_scenario_passes``) — the peripheral name, which Claude
    reliably echoes when summarising the dump. Kept loose so a
    legitimate "peripheral disabled / not configured" verdict (common
    on the BLINKY fixture, where most peripherals are untouched) still
    passes — the eval validates *routing + interpretation occurred*,
    not that a given peripheral happens to be configured.

Disambiguation: every scenario sets ``cwd`` to the L476RG F-PROJ so a
plain-NL prompt (no "stm32" keyword) carries unambiguous STM32 + probe
context; the descriptor lets the recipe autodiscover the ELF / device.

Transcripts: hand-authored stubs under
``tests/fixtures/eval/F-EVAL-DIAG-*/transcript.jsonc`` keep replay-mode
green out of the box. Re-record live on a bench with an attached
NUCLEO-L476RG to replace each stub with a real capture:

    STM32_EVAL_MODE=record pytest tests/eval/test_stm32diag_eval.py -m eval -v

  ~$0.04/scenario at Sonnet 4.6. The stub's ``_metadata.model`` reads
  ``"hand-authored"`` until then.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


# Canonical L476RG F-PROJ — unambiguous STM32 + ST-LINK context for
# free-form NL prompts. ReplayDriver ignores cwd (reads a stored
# transcript); LiveDriver / RecordingDriver use it for autodiscovery.
PROJECT_CWD = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "projects"
    / "F-PROJ-NUCLEO-L476RG"
)


# ---------------------------------------------------------------------------
# DIAG level-(a) specification table — single source of truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagSpec:
    """One DIAG-002…017 level-(a) eval specification.

    ``verb_re`` is the recipe-verb lookahead body (usually
    ``read-peripheral``; the vector-table case widens it). ``arg_re`` is
    the peripheral-name lookahead body (e.g. ``\\bRCC\\b``). ``cue`` is
    the final-text substring. The remaining fields feed the stub
    generator (see ``tools``-free ``conftest`` ReplayDriver) — they are
    not used by replay assertions directly.
    """

    diag_id: str
    slug: str
    prompt: str
    verb_re: str
    arg_re: str
    cue: str
    # CLI tool group after `stm32 ` — default the debug recipe surface;
    # widen for diagnostics Claude legitimately answers via another tool
    # (e.g. a flash-resident vector table read via `stm32 prog read-memory`).
    tool_re: str = "debug"


# Peripherals chosen to be representative for the L476RG bench:
# USART2 = the Nucleo's on-board VCP UART; SPI1/DMA1 exercise the
# "peripheral likely disabled on BLINKY → Claude reports not-configured"
# path; GPIOA carries PA2/PA3 (USART2 AF) and PA13/PA14 (SWD); DBGMCU /
# RCC / IWDG are always meaningful.
DIAG_SPECS: tuple[DiagSpec, ...] = (
    DiagSpec(
        diag_id="DIAG-002",
        slug="WATCHDOG",
        prompt=(
            "Is the watchdog firing on the attached target? Did we reset "
            "because of the IWDG or WWDG?"
        ),
        verb_re=r"read-peripheral",
        arg_re=r"\b(?:RCC|IWDG|WWDG)\b",
        cue="watchdog",
    ),
    DiagSpec(
        diag_id="DIAG-003",
        slug="CLOCK-TREE",
        prompt=(
            "Is the clock tree initialized properly? Are we actually "
            "running at the expected SYSCLK?"
        ),
        verb_re=r"read-peripheral",
        arg_re=r"\bRCC\b",
        cue="clock",
    ),
    DiagSpec(
        diag_id="DIAG-004",
        slug="PERIPH-CLOCK",
        prompt=(
            "Is the peripheral clock enabled for USART2? Did we forget the "
            "RCC enable bit?"
        ),
        verb_re=r"read-peripheral",
        arg_re=r"\bRCC\b",
        cue="USART2",
    ),
    DiagSpec(
        diag_id="DIAG-005",
        slug="GPIO-AF-MODE",
        prompt="Are the GPIO pins for USART2 set to Alternate Function mode?",
        verb_re=r"read-peripheral",
        arg_re=r"\bGPIOA\b",
        cue="GPIOA",
    ),
    DiagSpec(
        diag_id="DIAG-006",
        slug="AF-NUMBER",
        prompt=(
            "Is the alternate-function number correct for USART2 on its "
            "assigned pins?"
        ),
        verb_re=r"read-peripheral",
        arg_re=r"\bGPIOA\b",
        cue="GPIOA",
    ),
    DiagSpec(
        diag_id="DIAG-007",
        slug="BUSY-FLAG",
        prompt="Is the SPI1 BUSY flag stuck in its status register?",
        verb_re=r"read-peripheral",
        arg_re=r"\bSPI1\b",
        cue="SPI1",
    ),
    DiagSpec(
        diag_id="DIAG-008",
        slug="NVIC",
        prompt="Are the NVIC interrupts enabled for USART2?",
        verb_re=r"read-peripheral",
        arg_re=r"\bNVIC\b",
        cue="NVIC",
    ),
    DiagSpec(
        diag_id="DIAG-009",
        slug="VECTOR-TABLE",
        prompt=(
            "Is the ISR for USART2 actually registered in the vector "
            "table, or is it still pointing at the default handler?"
        ),
        # Vector-table check is the one non-pure-read-peripheral DIAG. The
        # vector table is flash-resident, so Claude legitimately reads the
        # slot directly with `stm32 prog read-memory` (no gdbserver needed)
        # — or via debug read-peripheral SCB (VTOR) + debug read-memory.
        # Accept either tool, and the read-mem / read-memory spellings.
        tool_re=r"(?:debug|prog)",
        verb_re=r"(?:read-peripheral|read-mem(?:ory)?)",
        arg_re=r"",
        cue="Handler",  # Default_Handler / USART2_IRQHandler in the verdict
    ),
    DiagSpec(
        diag_id="DIAG-010",
        slug="DRIVER-MODE",
        prompt="Is SPI1 running in interrupt, polling, or DMA mode?",
        verb_re=r"read-peripheral",
        arg_re=r"\bSPI1\b",
        cue="SPI1",
    ),
    DiagSpec(
        diag_id="DIAG-011",
        slug="UART-PARITY",
        prompt="Is the parity bit configured in USART2 the way the host expects?",
        verb_re=r"read-peripheral",
        arg_re=r"\bUSART2\b",
        cue="parity",
    ),
    DiagSpec(
        diag_id="DIAG-013",
        slug="DMA-ARMED",
        prompt="Is DMA configured and armed for SPI1 RX?",
        verb_re=r"read-peripheral",
        arg_re=r"\bDMA1\b",
        cue="DMA",
    ),
    DiagSpec(
        diag_id="DIAG-015",
        slug="PE-BIT",
        # Disambiguate from the RCC clock-enable (SPI1EN, that's DIAG-004):
        # DIAG-015 is the SPI peripheral-enable bit SPE in SPI1's own CR1
        # register. Name the register so Claude reads SPI1, not RCC.
        prompt=(
            "Is SPI1 itself enabled — did the SPE (SPI peripheral-enable) "
            "bit in SPI1's CR1 control register get set?"
        ),
        verb_re=r"read-peripheral",
        arg_re=r"\bSPI1\b",
        cue="SPI1",
    ),
    DiagSpec(
        diag_id="DIAG-016",
        slug="SWD-PINS",
        prompt=(
            "Did we accidentally reconfigure the SWD pins (PA13/PA14) as "
            "plain GPIOs early in main()?"
        ),
        verb_re=r"read-peripheral",
        arg_re=r"\bGPIOA\b",
        cue="GPIOA",
    ),
    DiagSpec(
        diag_id="DIAG-017",
        slug="DEBUG-PORT",
        prompt="Is the debug port disabled in software? Check DBGMCU.",
        verb_re=r"read-peripheral",
        arg_re=r"\bDBGMCU\b",
        cue="DBGMCU",
    ),
)


def _command_regex(spec: DiagSpec) -> str:
    """Build the any-order lookahead regex for the Bash ``command`` arg."""
    parts = [rf"(?=.*stm32\s+{spec.tool_re}\s+{spec.verb_re}\b)"]
    if spec.arg_re:
        parts.append(rf"(?=.*{spec.arg_re})")
    return "".join(parts)


def _scenario(spec: DiagSpec) -> EvalScenario:
    return EvalScenario(
        name=f"F-EVAL-DIAG-{spec.diag_id.split('-')[1]}-{spec.slug}",
        user_prompt=spec.prompt,
        allowed_tools=("Bash",),
        cwd=PROJECT_CWD,
        expected_tool_calls=(
            ToolCallMatch(name="Bash", args_contains={"command": _command_regex(spec)}),
        ),
        expected_final_text_contains=(spec.cue,),
    )


DIAG_SCENARIOS: tuple[EvalScenario, ...] = tuple(_scenario(s) for s in DIAG_SPECS)


# ---------------------------------------------------------------------------
# Test class — one parametrized test over the level-(a) family
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestStm32DiagEval:
    """DIAG-002…017 level-(a): NL diagnostic → ``read-peripheral`` recipe.

    Default replay mode reads each scenario's transcript from disk. To
    run live (costs ~$0.04 each, needs an attached NUCLEO-L476RG):
        STM32_EVAL_MODE=live pytest tests/eval/test_stm32diag_eval.py -m eval -v
    To re-record canonical transcripts:
        STM32_EVAL_MODE=record pytest tests/eval/test_stm32diag_eval.py -m eval -v
    """

    @pytest.mark.parametrize(
        "scenario", DIAG_SCENARIOS, ids=[s.name for s in DIAG_SCENARIOS]
    )
    def test_diag_level_a_recipe(self, eval_driver, scenario) -> None:
        """A DIAG-002…017 question routes to ``stm32 debug read-peripheral
        <NAME>`` (vector-table case: ``read-peripheral SCB`` + ``read-memory``)
        and Claude interprets the SVD-decoded dump."""
        result = eval_driver.run(scenario)
        assert_scenario_passes(result, scenario)
