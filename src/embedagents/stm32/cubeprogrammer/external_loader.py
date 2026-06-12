"""F-010 external loader (``.stldr``) discovery.

Per RES-020 + MR-3, v1 filters loaders by **filename substring match**
against the device family prefix extracted from ``banner.device_name``.
The DeviceDB-based address-range → memory-type derivation that the old
plan called for is deferred — substrate uses filename patterns only, and
wrong-loader-for-address combos surface as ``TARGET_CMD_ERR`` from the
vendor CLI (not a substrate pre-check).

Public surface:

- ``extract_family_prefix(device_name)`` — pull a substring like
  ``"STM32L4"`` / ``"STM32H7S"`` from a banner device name.
- ``discover_external_loader(...)`` — scan
  ``<programmer_path>/bin/ExternalLoader/`` for matching ``.stldr`` files.
"""

from __future__ import annotations

import re
from pathlib import Path


# Capture ``STM32`` + family letter + family digit + optional subfamily
# letter. Examples:
#   STM32L47xxx/L48xxx     → STM32L4
#   STM32H7Sx              → STM32H7S
#   STM32N657              → STM32N6
#   STM32U5                → STM32U5
_FAMILY_RE = re.compile(r"^(STM32[A-Z][0-9][A-Z]?)")


def extract_family_prefix(device_name: str) -> str:
    """Extract a family-prefix substring suitable for loader filter.

    Falls back to the raw ``device_name`` when the regex does not match —
    callers downstream still attempt the substring search; mismatches
    surface as empty discovery results.
    """
    m = _FAMILY_RE.match(device_name)
    return m.group(1) if m else device_name


def discover_external_loader(
    *,
    programmer_path: Path,
    device_family: str,
    explicit: Path | None = None,
) -> list[Path]:
    """Return matching ``.stldr`` candidates.

    Args:
        programmer_path: ``ctx.tools.cube_programmer_cli`` path. The
            external-loader directory is derived as
            ``<programmer_path>.parent / "ExternalLoader"`` (matching the
            standard CubeProgrammer install layout).
        device_family: family prefix (typically the output of
            ``extract_family_prefix`` on the banner's device_name) used
            as a case-insensitive substring filter against each
            ``.stldr`` filename.
        explicit: if set, returns ``[explicit]`` (after a file-existence
            check). Skips the family filter entirely — the caller is
            responsible for choosing the right loader.

    Returns:
        Empty list when the directory is missing, the explicit override
        does not exist, or no filename matches the family substring.
        Single-element list for unique matches. Multi-element list when
        several loaders fit — caller routes through an ``on_loader_choice``
        callback or refuses with a loud error.
    """
    if explicit is not None:
        return [explicit] if explicit.is_file() else []

    loader_dir = programmer_path.parent / "ExternalLoader"
    if not loader_dir.is_dir():
        return []

    needle = device_family.lower()
    matches: list[Path] = []
    for entry in sorted(loader_dir.glob("*.stldr")):
        if needle in entry.name.lower():
            matches.append(entry)
    return matches
