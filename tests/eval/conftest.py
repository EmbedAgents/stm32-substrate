"""Eval-layer scaffolding — Pass-1 step 11.

The eval layer drives Claude through the substrate's five slash-command
surfaces (``/stm32prog``, ``/stm32build``, ``/stm32debug``,
``/stm32project``, ``/stm32agent``) end-to-end and asserts that the
right tool calls land + the final response contains the right
substantive cues.

Two driver modes select via the ``STM32_EVAL_MODE`` env var:

- ``replay`` (default): ``ReplayDriver`` loads a hand- or live-recorded
  transcript from ``tests/fixtures/eval/<scenario>/transcript.jsonc``
  and returns it verbatim. Cheap + deterministic + no API key. Useful
  for scaffolding tests + golden-comparison regression checks.
- ``live``: ``LiveDriver`` would invoke Claude through the Claude Code
  SDK (T-007). Stubbed here — raises ``NotImplementedError``; resolved
  in step 12 once the framework is picked (candidates:
  ``claude-agent-sdk``, ``inspect_ai``, raw ``anthropic`` SDK).

The ``EvalScenario`` / ``EvalResult`` contract is the layer's API; both
drivers conform to ``EvalDriver``. Module-specific eval suites import
from this conftest + parametrise scenarios per slash-command flow.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

import pytest


EVAL_FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "eval"


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallMatch:
    """Expected tool-call pattern.

    ``name`` matches exactly against ``ToolCall.name``. ``args_contains``
    keys must be present on the call; values are regex patterns matched
    against ``str(call.args[key])`` via ``re.search`` (None = any).
    """

    name: str
    args_contains: Mapping[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalScenario:
    """One eval input/output specification.

    ``max_turns`` / ``max_budget_usd`` override the LiveDriver defaults
    per scenario — T3 fix-loops need a bigger envelope than atomics
    (Phase-4 measured: B-021 succeeds at 21 tool calls / $0.88; the
    atomic defaults of 15 / $1.00 cap T3 loops mid-flight). ``timeout_s``
    is the live wall-clock bound on the whole scenario (TST-09 — was a
    dead knob; enforced via asyncio timeout in live/record mode only,
    replay ignores it).
    """

    name: str
    user_prompt: str
    allowed_tools: tuple[str, ...]
    cwd: Path | None = None
    expected_tool_calls: tuple[ToolCallMatch, ...] = ()
    expected_final_text_contains: tuple[str, ...] = ()
    timeout_s: float = 600.0
    max_turns: int | None = None
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Mapping[str, object]


@dataclass(frozen=True)
class EvalResult:
    """One driver invocation's captured output."""

    tool_calls: tuple[ToolCall, ...]
    final_text: str
    duration_s: float


class EvalDriver(Protocol):
    def run(self, scenario: EvalScenario) -> EvalResult: ...


# ---------------------------------------------------------------------------
# Replay driver — loads pre-recorded transcripts
# ---------------------------------------------------------------------------


class ReplayDriver:
    """Loads a transcript from ``tests/fixtures/eval/<name>/transcript.jsonc``.

    Schema (JSONC; ``//`` line comments stripped before parse):

    ```jsonc
    {
        "tool_calls": [
            {"name": "Bash", "args": {"command": "stm32 prog list-probes"}}
        ],
        "final_text": "Detected probe SN 066D... on COM3."
    }
    ```

    Missing fixture → ``pytest.skip`` with a hint. Replay duration is
    synthesised as ``0.0`` since nothing was actually invoked.
    """

    def __init__(self, fixture_root: Path = EVAL_FIXTURE_ROOT) -> None:
        self.fixture_root = fixture_root

    def run(self, scenario: EvalScenario) -> EvalResult:
        path = self.fixture_root / scenario.name / "transcript.jsonc"
        if not path.is_file():
            pytest.skip(
                f"replay transcript missing at {path}; record one with "
                f"the live driver (step 12) or hand-author for scaffolding"
            )
        body = _strip_jsonc(path.read_text(encoding="utf-8"))
        data = json.loads(body)
        tool_calls = tuple(
            ToolCall(name=c["name"], args=dict(c.get("args", {})))
            for c in data.get("tool_calls", [])
        )
        return EvalResult(
            tool_calls=tool_calls,
            final_text=str(data.get("final_text", "")),
            duration_s=0.0,
        )


# ---------------------------------------------------------------------------
# Live driver — Claude Code SDK; stubbed pending T-007 framework selection
# ---------------------------------------------------------------------------


_STEERING_PREFIX_MAP: tuple[tuple[str, str], ...] = (
    # Scenario-name prefix -> the slash-command file whose body steers
    # the live run (TST-10: live runs must measure the SHIPPED steering,
    # not a harness-private prompt). First match wins.
    ("F-EVAL-DEBUG", "stm32debug.md"),
    ("F-EVAL-DIAG", "stm32debug.md"),   # level-(a) recipes + DIAG019/020 T3
    ("F-EVAL-DBG", "stm32debug.md"),    # DBG008/009 T3 loops
    ("F-EVAL-PROG", "stm32prog.md"),
    ("F-EVAL-BUILD", "stm32build.md"),
    ("F-EVAL-B021", "stm32build.md"),
    ("F-EVAL-MX", "stm32project.md"),
    ("F-EVAL-VCP", "stm32agent.md"),    # VCP routes through /stm32agent
)


def _steering_command_file(scenario_name: str) -> str | None:
    """Resolve the command file that steers a live scenario, or None."""
    for prefix, filename in _STEERING_PREFIX_MAP:
        if scenario_name.startswith(prefix):
            path = Path(__file__).parents[2] / ".claude" / "commands" / filename
            return str(path) if path.is_file() else None
    return None


class LiveDriver:
    """Live mode: drive Claude through the ``claude-agent-sdk``.

    Step 12 (T-007) lands this — picks the official Anthropic SDK over
    ``inspect_ai`` (general-purpose, would require wrapping every slash
    command as an inspect-shaped tool) and the raw ``anthropic`` SDK
    (would reimplement Claude Code's slash-command routing). The agent
    SDK runs Claude *through* Claude Code's loop, which is the actual
    user-facing surface the substrate ships behind.

    Flow per ``run(scenario)``:

    1. Async-iterate ``query(prompt=scenario.user_prompt, options=...)``.
    2. ``ClaudeAgentOptions`` carries: ``allowed_tools``, ``cwd``,
       ``model``, ``permission_mode="bypassPermissions"`` (eval bypass
       — we trust the substrate's CLI), ``setting_sources=["project"]``
       (loads ``.claude/commands/`` so slash commands are available),
       ``max_budget_usd``, ``max_turns``.
    3. Walk ``AssistantMessage.content`` blocks: ``ToolUseBlock`` →
       ``ToolCall``; ``TextBlock`` → append to final-text buffer.
    4. ``ResultMessage`` carries cost + duration metadata; surfaced via
       ``EvalResult.duration_s`` (substrate doesn't expose cost in v1).
    5. Optional record-to-disk: if ``record_to`` is set, write the
       captured shape to a transcript JSONC for future replay.

    Env knobs:

    - ``ANTHROPIC_API_KEY`` (or Claude Code's own auth context if
      ``claude`` is logged in on this host).
    - ``STM32_EVAL_MODEL`` (default ``claude-sonnet-4-6``).
    - ``STM32_EVAL_MAX_BUDGET_USD`` (default ``0.50`` — single scenario).
    - ``STM32_EVAL_SYSTEM_PROMPT_FILE`` — path to a slash-command file
      (e.g. ``.claude/commands/stm32debug.md``); its body (frontmatter +
      ``$ARGUMENTS`` line stripped) replaces the hardcoded steering
      system prompt, so live runs measure the *shipped* command-file
      steering instead of the harness's own (TST-10; used by the
      Phase-3 lean-command-file experiment).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_budget_usd: float | None = None,
        max_turns: int | None = None,
        record_to: Path | None = None,
    ) -> None:
        self.model = model or os.environ.get("STM32_EVAL_MODEL", "claude-fable-5")
        # Defaults retuned for claude-fable-5 from the Phase-3 pilot
        # (RES-045): 23 measured scenarios — mean $0.254, max $0.41
        # against the old 0.50 cap; tool calls median 5, max 10 against
        # the old 10-turn cap (hit once fatally). 1.00 / 15 give the
        # observed tails real headroom without unbounding T3 loops.
        self.max_budget_usd = max_budget_usd or float(
            os.environ.get("STM32_EVAL_MAX_BUDGET_USD", "1.00")
        )
        self.max_turns = max_turns if max_turns is not None else int(
            os.environ.get("STM32_EVAL_MAX_TURNS", "15")
        )
        self.record_to = record_to

    def run(self, scenario: EvalScenario) -> EvalResult:
        import asyncio

        async def _bounded() -> EvalResult:
            # TST-09: scenario.timeout_s is the live wall-clock bound on
            # the whole run (was a dead knob). A hung subprocess inside
            # the nested agent (e.g. an unbounded stream) fails the
            # scenario here instead of wedging the suite.
            return await asyncio.wait_for(
                self._run_async(scenario), timeout=scenario.timeout_s
            )

        try:
            return asyncio.run(_bounded())
        except TimeoutError:
            pytest.fail(
                f"{scenario.name}: live run exceeded scenario.timeout_s="
                f"{scenario.timeout_s}s (TST-09 wall-clock bound)"
            )

    async def _run_async(self, scenario: EvalScenario) -> EvalResult:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                TextBlock,
                ToolUseBlock,
                query,
            )
        except ImportError:
            pytest.skip(
                "claude-agent-sdk not installed; "
                "install with `pip install -e .[eval]` to enable live mode."
            )

        tool_calls: list[ToolCall] = []
        final_text_parts: list[str] = []
        observed_duration_ms: float | None = None
        cost_usd: float | None = None

        # Make the `stm32` console script findable by the eval
        # subprocess — and make sure it is THIS interpreter's install.
        # The venv running pytest goes FIRST: a pipx/user-site shim for
        # a different checkout (e.g. the public-repo editable install)
        # must not shadow the mainline under test — Phase-4 live runs
        # discovered exactly that: ~/.local/bin/stm32 resolved to the
        # v0.1.0 embedagents tree. User-site stays as a fallback for
        # `pip install --user` setups.
        import sysconfig
        env_overrides: dict[str, str] = {}
        current_path = os.environ.get("PATH", "")
        venv_scripts = str(Path(sys.executable).parent)
        user_scripts = sysconfig.get_path("scripts", "nt_user") if sys.platform == "win32" else sysconfig.get_path("scripts", "posix_user")
        prefix = [p for p in (venv_scripts, user_scripts) if p and p not in current_path]
        if prefix:
            env_overrides["PATH"] = os.pathsep.join(prefix) + os.pathsep + current_path

        steer_file = os.environ.get(
            "STM32_EVAL_SYSTEM_PROMPT_FILE"
        ) or _steering_command_file(scenario.name)
        if steer_file:
            body = Path(steer_file).read_text(encoding="utf-8")
            if body.startswith("---"):
                # Strip the slash-command YAML frontmatter block.
                body = body.split("---", 2)[2]
            body = body.replace("User input: `$ARGUMENTS`", "").strip()
            system_prompt = (
                "You have access to the STM32 substrate's `stm32` CLI on "
                "PATH; invoke it via Bash.\n\n" + body
            )
        else:
            # Fallback for scenarios with no command-file mapping (e.g.
            # the scaffold self-test).
            system_prompt = (
                "You have access to the STM32 substrate's `stm32` CLI on PATH. "
                "Use Bash to invoke it: `stm32 prog ...` (flash / erase / reset "
                "/ raw memory), `stm32 build`, `stm32 debug ...` (sessions, "
                "register & peripheral inspection, fault decode), `stm32 vcp "
                "...` (serial). Prefer one bash call with the most specific "
                "subcommand for the user's intent."
            )

        options = ClaudeAgentOptions(
            allowed_tools=list(scenario.allowed_tools),
            cwd=str(scenario.cwd) if scenario.cwd else None,
            model=self.model,
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            # Per-scenario envelope overrides beat the driver defaults
            # (T3 loops need 25 turns / ~$2; atomics keep 15 / $1).
            max_budget_usd=scenario.max_budget_usd or self.max_budget_usd,
            max_turns=scenario.max_turns or self.max_turns,
            env=env_overrides,
            system_prompt=system_prompt,
        )

        start = time.monotonic()
        async for message in query(prompt=scenario.user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls.append(
                            ToolCall(name=block.name, args=dict(block.input))
                        )
                    elif isinstance(block, TextBlock):
                        final_text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                observed_duration_ms = message.duration_ms
                cost_usd = message.total_cost_usd
        elapsed_s = time.monotonic() - start
        duration_s = (
            observed_duration_ms / 1000.0 if observed_duration_ms else elapsed_s
        )

        result = EvalResult(
            tool_calls=tuple(tool_calls),
            final_text="\n".join(final_text_parts),
            duration_s=duration_s,
        )

        # One metrics line per live scenario (stdout; run pytest with -s
        # to stream). Feeds the budget-knob retune (Phase-3 #3 / Phase-4
        # pilot): measured cost + tool-call count + wall clock per
        # scenario on the actual model.
        print(
            f"[eval-live] {scenario.name}: model={self.model} "
            f"cost_usd={cost_usd} tool_calls={len(tool_calls)} "
            f"duration_s={duration_s:.1f}",
            flush=True,
        )

        if self.record_to is not None:
            transcript = {
                "_metadata": {
                    "model": self.model,
                    "cost_usd": cost_usd,
                    "duration_s": duration_s,
                },
                "tool_calls": [
                    {"name": c.name, "args": dict(c.args)} for c in tool_calls
                ],
                "final_text": result.final_text,
            }
            self.record_to.parent.mkdir(parents=True, exist_ok=True)
            self.record_to.write_text(
                json.dumps(transcript, indent=2, default=str),
                encoding="utf-8",
                newline="\n",
            )
        return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def eval_driver(request) -> EvalDriver:
    """Pick driver mode via ``STM32_EVAL_MODE``.

    - ``replay`` (default) → ``ReplayDriver``.
    - ``live`` → ``LiveDriver`` using ``claude-agent-sdk``.
    - ``record`` → ``LiveDriver`` that *also* writes the recorded
      transcript to ``tests/fixtures/eval/<scenario>/transcript.jsonc``
      (the same path ``ReplayDriver`` reads). Use to refresh the
      golden after a model upgrade or a substrate change.

    Live + record modes need ``claude-agent-sdk`` installed
    (``pip install -e .[eval]``) and either an ``ANTHROPIC_API_KEY``
    env var or a logged-in ``claude`` CLI on this host.
    """
    mode = os.environ.get("STM32_EVAL_MODE", "replay").lower()
    if mode == "replay":
        return ReplayDriver()
    if mode == "live":
        return LiveDriver()
    if mode == "record":
        # Closure-side hand-off: each test asks for ``eval_driver``; the
        # LiveDriver below writes its transcript per-scenario at run()
        # time. We inject the record path through a per-scenario
        # wrapper so each scenario lands at its own path.
        class _RecordingDriver:
            def __init__(self) -> None:
                self._sdk = LiveDriver()

            def run(self, scenario: EvalScenario) -> EvalResult:
                self._sdk.record_to = (
                    EVAL_FIXTURE_ROOT / scenario.name / "transcript.jsonc"
                )
                return self._sdk.run(scenario)

        return _RecordingDriver()
    pytest.skip(
        f"unknown STM32_EVAL_MODE={mode!r}; expected 'replay', 'live', or 'record'"
    )


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def assert_scenario_passes(result: EvalResult, scenario: EvalScenario) -> None:
    """Order-insensitive comparison.

    - Every ``expected_tool_calls`` entry must find at least one matching
      ``result.tool_calls`` entry (name exact; args_contains keys present
      with regex-search match on stringified value).
    - Every ``expected_final_text_contains`` substring must appear in
      ``result.final_text`` (case-sensitive).
    """
    for expected in scenario.expected_tool_calls:
        matches = [
            call for call in result.tool_calls
            if call.name == expected.name and _args_match(call.args, expected.args_contains)
        ]
        assert matches, (
            f"scenario {scenario.name!r}: no tool call matched "
            f"{expected!r}. Calls observed: "
            f"{[(c.name, dict(c.args)) for c in result.tool_calls]}"
        )
    for needle in scenario.expected_final_text_contains:
        assert needle in result.final_text, (
            f"scenario {scenario.name!r}: final_text missing substring "
            f"{needle!r}. final_text was:\n{result.final_text}"
        )


def _args_match(
    actual: Mapping[str, object],
    expected: Mapping[str, str | None],
) -> bool:
    for key, pattern in expected.items():
        if key not in actual:
            return False
        if pattern is None:
            continue
        if not re.search(pattern, str(actual[key])):
            return False
    return True


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` line comments. Quote-aware enough for typical JSONC;
    not a full lexer (no block comments, no escape handling for strings
    that span lines — none of which appear in our hand-authored
    transcripts)."""
    lines: list[str] = []
    for line in text.splitlines():
        # Skip pure-comment lines outright.
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        # Strip trailing ``//`` comment guarded by quote-counting.
        in_str = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            elif not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        lines.append(line[:cut].rstrip())
    return "\n".join(lines)
