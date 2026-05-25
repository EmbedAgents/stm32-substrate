"""Eval tests for the ``/stm32agent`` slash-command surface — VCP intents.

Validates that natural-language VCP prompts (tail / send / reconnect /
close) route through Claude into the substrate's ``stm32 vcp ...``
CLI correctly. Per-prompt fixtures live under
``tests/fixtures/eval/F-EVAL-VCP-*/transcript.jsonc``.

Layout convention (one file per slash command):

  tests/eval/test_stm32prog_eval.py                    — /stm32prog
  tests/eval/test_stm32build_eval.py                   — /stm32build
  tests/eval/test_stm32debug_eval.py                   — /stm32debug
  tests/eval/test_stm32project_eval.py                 — /stm32project
  tests/eval/test_stm32agent_eval.py    (this file)    — /stm32agent + VCP

Scope (v1): VCP-001 (tail), VCP-002 (send), VCP-003 (reconnect) +
``vcp close`` (port-handoff per RES-014 Q5). Compound flows (CP-*) and
T3 prompts (VCP-004/005) ship in Pass 2 / later waves.

Disambiguation:

  Every scenario sets ``cwd`` to the L476RG F-PROJ so the descriptor's
  ``firmware.board: NUCLEO-L476RG`` cues the substrate's probe→port
  resolution (MR-2 closure per RES-020) when multiple ST-LINKs are
  attached. Single-probe benches resolve trivially; multi-probe benches
  resolve via the SN→board map built from ``cubeprogrammer.list_probes()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.conftest import (
    EvalScenario,
    ToolCallMatch,
    assert_scenario_passes,
)


# L476RG F-PROJ — descriptor's ``firmware.board: NUCLEO-L476RG`` enables
# the substrate's probe→port resolution when multiple ST-LINKs are
# attached (MR-2 closure per RES-020). Single-probe benches also work
# (auto-pick).
PROJECT_CWD = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "projects"
    / "F-PROJ-NUCLEO-L476RG"
)


# ---------------------------------------------------------------------------
# Scenario 1 — VCP-001 tail (snapshot mode; --follow not requested)
# ---------------------------------------------------------------------------

VCP_TAIL = EvalScenario(
    name="F-EVAL-VCP-TAIL",
    # Plain-NL prompt; no --port / --baud / --last-n cues — substrate
    # autodiscovers from descriptor + runtime defaults.
    user_prompt="Tail the VCP output from the target.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Only constraint: Claude calls `stm32 vcp tail`. No
            # --follow lookahead — the prompt doesn't ask to stream
            # forever (snapshot mode is the safer default for a
            # one-shot eval invocation).
            args_contains={"command": r"stm32\s+vcp\s+tail\b"},
        ),
    ),
    # "VCP" is the strongest domain cue Claude carries through.
    expected_final_text_contains=("VCP",),
)


# ---------------------------------------------------------------------------
# Scenario 2 — VCP-002 send (explicit payload)
# ---------------------------------------------------------------------------

VCP_SEND = EvalScenario(
    name="F-EVAL-VCP-SEND",
    # Quote the payload so Claude carries it verbatim to the `send`
    # positional arg. Without an explicit payload, "VCP send line"
    # is ambiguous and Claude may pick a placeholder or ask.
    user_prompt="Send the line 'ping' over the VCP and read the reply.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            # Two constraints (any-order via lookaheads):
            #   1. verb is `stm32 vcp send`.
            #   2. the payload `ping` appears in the command (it's
            #      the positional arg after `send`).
            args_contains={
                "command": (
                    r"(?=.*stm32\s+vcp\s+send\b)"
                    r"(?=.*\bping\b)"
                ),
            },
        ),
    ),
    # Claude usually echoes the payload word ("ping") when summarising
    # the round-trip.
    expected_final_text_contains=("ping",),
)


# ---------------------------------------------------------------------------
# Scenario 3 — VCP-003 reconnect (post-reset)
# ---------------------------------------------------------------------------

VCP_RECONNECT = EvalScenario(
    name="F-EVAL-VCP-RECONNECT",
    user_prompt="Reconnect to the VCP after the target reset.",
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            args_contains={"command": r"stm32\s+vcp\s+reconnect\b"},
        ),
    ),
    # Claude echoes "reconnect" or "VCP" in the summary.
    expected_final_text_contains=("reconnect",),
)


# ---------------------------------------------------------------------------
# Scenario 4 — vcp close (port handoff per RES-014 Q5)
# ---------------------------------------------------------------------------

VCP_CLOSE = EvalScenario(
    name="F-EVAL-VCP-CLOSE",
    # Mention an external tool (minicom / screen / Cutecom) so Claude
    # disambiguates "close the VCP" from "close the debug session".
    user_prompt=(
        "Release the VCP port so I can open it in minicom."
    ),
    allowed_tools=("Bash",),
    cwd=PROJECT_CWD,
    expected_tool_calls=(
        ToolCallMatch(
            name="Bash",
            args_contains={"command": r"stm32\s+vcp\s+close\b"},
        ),
    ),
    # Claude echoes "VCP" (and usually "release" / "close" / "port").
    expected_final_text_contains=("VCP",),
)


# ---------------------------------------------------------------------------
# Test class — one test per scenario
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestStm32AgentVcpEval:
    """Default replay mode: reads each scenario's transcript from disk.

    To run live (costs ~$0.04 each):
        STM32_EVAL_MODE=live pytest tests/eval/test_stm32agent_eval.py -m eval -v

    To re-record canonical transcripts after a substrate or model
    change (writes to tests/fixtures/eval/F-EVAL-VCP-*/transcript.jsonc):
        STM32_EVAL_MODE=record pytest tests/eval/test_stm32agent_eval.py -m eval -v
    """

    def test_vcp_tail(self, eval_driver) -> None:
        """User asks 'tail the VCP' → Claude calls ``stm32 vcp tail``
        (port + baud autodiscovered)."""
        result = eval_driver.run(VCP_TAIL)
        assert_scenario_passes(result, VCP_TAIL)

    def test_vcp_send(self, eval_driver) -> None:
        """User asks to send a quoted payload → Claude calls
        ``stm32 vcp send 'ping'``."""
        result = eval_driver.run(VCP_SEND)
        assert_scenario_passes(result, VCP_SEND)

    def test_vcp_reconnect(self, eval_driver) -> None:
        """User asks to reconnect after reset → Claude calls
        ``stm32 vcp reconnect``."""
        result = eval_driver.run(VCP_RECONNECT)
        assert_scenario_passes(result, VCP_RECONNECT)

    def test_vcp_close(self, eval_driver) -> None:
        """User asks to release the port for external tool handoff →
        Claude calls ``stm32 vcp close``."""
        result = eval_driver.run(VCP_CLOSE)
        assert_scenario_passes(result, VCP_CLOSE)
