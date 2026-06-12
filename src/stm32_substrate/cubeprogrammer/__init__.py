"""STM32_Programmer_CLI wrapper.

Public surface: ``CubeProgrammer`` class + the prompt-specific result
dataclasses + ``CubeProgrammerErrorCode`` enum. Maps 1:1 to D-* and F-*
prompts, plus DIAG-001 binary-only path, DIAG-018, VCP-007 SWO, and CP-001.

See ``v1/cubeprogrammer-api.md`` for the full method list, signatures,
and CLI subcommand mapping.
"""

from __future__ import annotations

from stm32_substrate.cubeprogrammer.client import CubeProgrammer
from stm32_substrate.cubeprogrammer.codes import (
    CubeProgrammerErrorCode,
    is_recoverable,
)
from stm32_substrate.cubeprogrammer.results import (
    BankInfo,
    BannerResult,
    BooleanResult,
    Confirmation,
    CoresResult,
    EraseConfirmation,
    FlashConfirmation,
    HardFaultDecode,
    ITMRecord,
    MemoryLayoutResult,
    MemoryReadResult,
    OptionByteDiffEntry,
    OptionBytesDiff,
    OptionBytesResult,
    PairFlashResult,
    ProbeRecord,
    RecoveryAttempt,
    RecoveryResult,
    ResetConfirmation,
    SVDResult,
)

__all__ = [
    "CubeProgrammer",
    "CubeProgrammerErrorCode",
    "is_recoverable",
    "BannerResult",
    "BankInfo",
    "BooleanResult",
    "Confirmation",
    "CoresResult",
    "EraseConfirmation",
    "FlashConfirmation",
    "HardFaultDecode",
    "ITMRecord",
    "MemoryLayoutResult",
    "MemoryReadResult",
    "OptionByteDiffEntry",
    "OptionBytesDiff",
    "OptionBytesResult",
    "PairFlashResult",
    "ProbeRecord",
    "RecoveryAttempt",
    "RecoveryResult",
    "ResetConfirmation",
    "SVDResult",
]
