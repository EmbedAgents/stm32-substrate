"""``SigningResult`` — F-013 success result.

Per the signing API spec § "Result type". Substrate captures, doesn't
interpret (ADR-004): ``log_path`` carries the verbatim
``STM32_SigningTool_CLI`` stdout + stderr; callers / Claude read the
raw text for trouble-shooting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SigningResult:
    """Outcome of a successful ``SigningTool.sign_binary()`` invocation."""

    input_path: Path
    output_path: Path
    bytes_in: int
    bytes_out: int
    load_address: str
    entry_point: str | None
    image_type: Literal["ssbl", "fsbl", "teeh", "teed", "teex", "copro"]
    header_version: Literal["1", "2", "2.1", "2.2", "2.3"]
    option_flags: str | None
    no_auth_flag: bool
    align_applied: bool
    device_family: str | None
    duration_s: float
    log_path: Path
