"""STM32CubeMX wrapper.

Per RES-017 + the P-037 scope cut, this is a thin wrapper around
``STM32CubeMX -q <script>``. Single public method (``generate``), one
result type (``CubeMXResult``), one error class hierarchy
(``CubeMXError`` / ``CubeMXLauncherError`` — re-exported from
``stm32_substrate.errors``).

No IOC parser. No DeviceDB indexer. No output classifier. Substrate
invokes the tool, observes external signals (subprocess state, marker
file presence, log mtime), reports success / failure, hands the log
path to the caller on failure.

See the CubeMX API spec for the full spec.
"""

from __future__ import annotations

from stm32_substrate.cubemx.client import CubeMX
from stm32_substrate.cubemx.results import CubeMXResult, ProgressEvent

__all__ = ["CubeMX", "CubeMXResult", "ProgressEvent"]
