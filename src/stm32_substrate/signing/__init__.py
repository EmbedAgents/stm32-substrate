"""STM32_SigningTool_CLI wrapper.

Single-method module per RES-015. Public surface: ``SigningTool`` class
+ ``SigningResult`` dataclass + ``SigningToolError`` exception (re-exported
from ``stm32_substrate.errors``).

F-013 only — substrate doesn't validate device family (vendor CLI
reports its own error). See the signing API spec for the full spec.

Cross-module consumers:

- ``cubeprogrammer.flash_signed_pair(sign_unsigned=True)`` constructs a
  ``SigningTool`` per leg to materialise unsigned inputs into trusted
  binaries before flashing.
- ``compound/n6_flash_boot.py`` (F-015 — Pass 2).
"""

from __future__ import annotations

from stm32_substrate.signing.client import SigningTool
from stm32_substrate.signing.results import SigningResult

__all__ = ["SigningTool", "SigningResult"]
