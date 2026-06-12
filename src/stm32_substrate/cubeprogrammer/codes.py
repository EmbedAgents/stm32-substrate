"""CubeProgrammer error codes + recoverability classification.

Mirrors UM2576 Appendix A. ``parse_error()`` (in ``parsers.py``) maps
stderr patterns + the subprocess exit code onto these enum members.

``is_recoverable(code)`` returns the boolean from the recoverability
matrix documented in ``v1/cubeprogrammer-api.md`` — used to drive
``CubeProgrammerError.recoverable`` and to decide whether the D-002
ladder should attempt a recovery walk.
"""

from __future__ import annotations

from enum import IntEnum


class CubeProgrammerErrorCode(IntEnum):
    """UM2576 Appendix A failure codes shared with gdbserver."""

    TARGET_CONNECT_ERR = 1                # port in use
    TARGET_DLL_ERR = 2                    # no ST-LINK detected
    TARGET_USB_COMM_ERR = 3
    TARGET_NO_DEVICE = 4                  # ST-LINK present, no board
    TARGET_UNKNOWN_MCU_TARGET = 5
    TARGET_FIRMWARE_OLD = 6               # update STSW-LINK007
    TARGET_RESET_ERR = 7
    TARGET_HELD_UNDER_RESET = 8
    TARGET_NOT_HALTED = 9
    TARGET_CMD_ERR = 10                   # erase rejected, etc.
    TARGET_HALT_ERR = 11
    TARGET_INTERNAL_ERR = 12
    TARGET_VERSION_ERR = 13
    TARGET_STATUS_ERR = 14
    TARGET_STLINK_SELECT_REQ = 16         # multiple ST-LINKs; need sn=
    TARGET_STLINK_SERIAL_NOT_FOUND = 17


# Recoverable members of the enum. Everything else (and ``None`` for
# unmapped codes) is non-recoverable per the api spec.
_RECOVERABLE: frozenset[CubeProgrammerErrorCode] = frozenset(
    {
        CubeProgrammerErrorCode.TARGET_NO_DEVICE,
        CubeProgrammerErrorCode.TARGET_UNKNOWN_MCU_TARGET,
        CubeProgrammerErrorCode.TARGET_HELD_UNDER_RESET,
        CubeProgrammerErrorCode.TARGET_NOT_HALTED,
    }
)


def is_recoverable(code: CubeProgrammerErrorCode | int | None) -> bool:
    """Return ``True`` iff the D-002 ladder may help recover from ``code``.

    Per ``v1/cubeprogrammer-api.md`` § "Recoverability matrix":

    - Unmapped codes (``None``, or an int we don't recognise) → ``False``
      (substrate doesn't know the recovery semantics).
    - Recoverable: 4 (NO_DEVICE), 5 (UNKNOWN_MCU_TARGET),
      8 (HELD_UNDER_RESET), 9 (NOT_HALTED).
    - Everything else → ``False``.
    """
    if code is None:
        return False
    if isinstance(code, int) and not isinstance(code, CubeProgrammerErrorCode):
        try:
            code = CubeProgrammerErrorCode(code)
        except ValueError:
            return False
    return code in _RECOVERABLE
