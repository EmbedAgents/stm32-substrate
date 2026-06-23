"""STM32_SigningTool_CLI wrapper.

Single-method module per RES-015. Public surface: ``SigningTool`` class
+ ``SigningResult`` dataclass + ``SigningToolError`` exception (re-exported
from ``embedagents.stm32.errors``).

F-013 only — substrate doesn't validate device family (vendor CLI
reports its own error). See ``v1/signing-api.md`` for the full spec.

Cross-module consumers:

- ``cubeprogrammer.flash_signed_pair(sign_unsigned=True)`` constructs a
  ``SigningTool`` per leg to materialise unsigned inputs into trusted
  binaries before flashing.
- F-015 (N6 flash + boot) — Claude-composed per RES-044 (no ``compound/``
  subpackage).
"""

from __future__ import annotations

from embedagents.stm32.signing.client import SigningTool
from embedagents.stm32.signing.results import SigningResult

__all__ = ["SigningTool", "SigningResult"]
