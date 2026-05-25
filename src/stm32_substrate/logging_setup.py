"""Thin logger wrapper for the substrate.

Per the API conventions § "Logging and progress streaming":

- Hierarchical loggers per tool: ``stm32_substrate``,
  ``stm32_substrate.cubeprogrammer``, ``stm32_substrate.cubeide``, …
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

_ROOT_NAME = "stm32_substrate"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a substrate-namespaced logger.

    Examples:
        ``get_logger()`` → ``stm32_substrate``
        ``get_logger("cubeprogrammer")`` → ``stm32_substrate.cubeprogrammer``
        ``get_logger("stm32_substrate.debug.session")`` → unchanged (already namespaced)
    """
    if name is None or name == _ROOT_NAME:
        return logging.getLogger(_ROOT_NAME)
    if name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
