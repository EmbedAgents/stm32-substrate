"""Thin logger wrapper for the substrate.

Per ``v1/api-conventions.md`` § "Logging and progress streaming":

- Hierarchical loggers per tool: ``embedagents.stm32``,
  ``embedagents.stm32.cubeprogrammer``, ``embedagents.stm32.cubeide``, …
- Levels: DEBUG (full subprocess output, argv, XML diffs), INFO (normal
  milestones), WARNING (substrate-detected issues that aren't failures),
  ERROR (failures; usually raised as ``SubstrateError`` and logged).
- Library does NOT configure handlers — the CLI does. Importing this
  module is side-effect-free.

Structured fields convention: when call sites want to attach structured
context (tool, prompt_id, duration_s, marker), they pass them via the
stdlib ``extra={"tool": ...}`` keyword. Custom formatters in the CLI
layer extract these.
"""

from __future__ import annotations

import logging

_ROOT_NAME = "embedagents.stm32"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a substrate-namespaced logger.

    Examples:
        ``get_logger()`` → ``embedagents.stm32``
        ``get_logger("cubeprogrammer")`` → ``embedagents.stm32.cubeprogrammer``
        ``get_logger("embedagents.stm32.debug.session")`` → unchanged (already namespaced)
    """
    if name is None or name == _ROOT_NAME:
        return logging.getLogger(_ROOT_NAME)
    if name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
