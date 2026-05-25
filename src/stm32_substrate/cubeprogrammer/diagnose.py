"""D-002 SWD recovery ladder.

Walks up to 20 ``mode × frequency`` combinations looking for one that
brings the target back. The ladder *is* the retry policy (M-017); no
extra retries are layered on top. ``target_responsive=False`` is a valid
result — not an exception.

The walk is bounded by ``programmer.diagnose_timeout_s`` (default 120 s,
per RES-020). The 20 × 30 s = 600 s worst-case without the cap was a HIL
"no long waits" violation; substrate now bails with
``bailed_on_timeout=True`` rather than silently waiting.

Public surface:

- ``LADDER_MODES`` — the five connect modes tried in order.
- ``LADDER_FREQS_KHZ`` — the four SWD frequencies (None = device default).
- ``run_diagnose(client, *, timeout_s)`` — execute the ladder.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from stm32_substrate.cubeprogrammer.codes import CubeProgrammerErrorCode
from stm32_substrate.cubeprogrammer.results import RecoveryAttempt, RecoveryResult
from stm32_substrate.errors import CubeProgrammerError

if TYPE_CHECKING:
    from stm32_substrate.cubeprogrammer.client import CubeProgrammer


LADDER_MODES: list[str] = ["NORMAL", "UR", "HOTPLUG", "POWERDOWN", "hwRstPulse"]
LADDER_FREQS_KHZ: list[int | None] = [None, 4000, 1800, 480]

# Codes that are not D-002-recoverable — abort the ladder immediately on
# either of these rather than burning attempts. ``TARGET_DLL_ERR`` means
# no probe is connected at all; ``TARGET_FIRMWARE_OLD`` requires a user
# action (STSW-LINK007 update).
_FATAL_CODES: frozenset[int] = frozenset(
    {
        int(CubeProgrammerErrorCode.TARGET_DLL_ERR),
        int(CubeProgrammerErrorCode.TARGET_FIRMWARE_OLD),
    }
)


def run_diagnose(
    client: "CubeProgrammer", *, timeout_s: float
) -> RecoveryResult:
    """Iterate 5 modes × 4 frequencies and return on first success.

    Args:
        client: a ``CubeProgrammer`` instance. Used for its
            ``_raw_connect()`` building block and ``ctx.logger``.
        timeout_s: overall wall-clock cap for the ladder. Set on a
            per-call basis from ``programmer.diagnose_timeout_s``.

    Behaviour:

    - Returns on first successful connect with ``target_responsive=True``
      and the winning ``recovery_method`` / ``swd_freq_khz_used``.
    - Aborts the ladder on ``TARGET_DLL_ERR`` / ``TARGET_FIRMWARE_OLD``
      (not D-002-recoverable; the attempts log carries the diagnostic).
    - Bails with ``bailed_on_timeout=True`` if total elapsed exceeds
      ``timeout_s`` before completion.
    - Always returns a ``RecoveryResult``; never raises.
    """
    log = client.ctx.logger.getChild("cubeprogrammer.diagnose")
    attempts: list[RecoveryAttempt] = []
    start = time.monotonic()

    for freq in LADDER_FREQS_KHZ:
        for mode in LADDER_MODES:
            # ``>=`` (not ``>``) so a zero/negative budget bails before the
            # first attempt deterministically across platforms: Linux's
            # fine monotonic clock yields a tiny positive elapsed, but
            # Windows' coarser clock can read exactly 0.0, which ``> 0.0``
            # would (wrongly) let through. ``>=`` also correctly bails when
            # elapsed lands exactly on a positive cap.
            if time.monotonic() - start >= timeout_s:
                log.warning(
                    "diagnose_micro: bailing after %d attempts — overall "
                    "cap (%.1fs) exceeded",
                    len(attempts),
                    timeout_s,
                )
                return RecoveryResult(
                    target_responsive=False,
                    recovery_method=None,
                    swd_freq_khz_used=None,
                    attempts_log=attempts,
                    bailed_on_timeout=True,
                )

            try:
                banner = client._raw_connect(mode=mode, freq_khz=freq)
            except CubeProgrammerError as ex:
                attempts.append(
                    RecoveryAttempt(
                        mode=mode,
                        freq_khz=freq or 0,
                        success=False,
                        error_code=ex.error_code,
                        error_message=ex.message,
                    )
                )
                log.info(
                    "[mode=%s freq=%s] failed: %s",
                    mode,
                    freq if freq is not None else "default",
                    ex.message,
                )
                if ex.error_code is not None and ex.error_code in _FATAL_CODES:
                    log.warning(
                        "diagnose_micro: aborting ladder — %s is not "
                        "recoverable",
                        ex.error_code,
                    )
                    return RecoveryResult(
                        target_responsive=False,
                        recovery_method=None,
                        swd_freq_khz_used=None,
                        attempts_log=attempts,
                    )
                continue

            attempts.append(
                RecoveryAttempt(
                    mode=mode,
                    freq_khz=freq or 0,
                    success=True,
                    error_code=None,
                    error_message=None,
                )
            )
            log.info(
                "[mode=%s freq=%s] success after %d attempt(s)",
                mode,
                freq if freq is not None else "default",
                len(attempts),
            )
            return RecoveryResult(
                target_responsive=True,
                recovery_method=mode,
                swd_freq_khz_used=banner.swd_freq_khz,
                attempts_log=attempts,
            )

    # Exhausted the full 20-attempt ladder without success.
    return RecoveryResult(
        target_responsive=False,
        recovery_method=None,
        swd_freq_khz_used=None,
        attempts_log=attempts,
    )
