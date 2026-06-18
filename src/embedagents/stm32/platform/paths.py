"""Per-OS user directories for substrate scratch state.

Per ADR-007 (supersedes ADR-005), the choice of OS-specific base directory
lives here, not inline at the call sites — business-logic code asks for a
substrate-owned path and never branches on ``sys.platform`` itself.

Public surface:

- ``user_cache_root()`` — persistent per-user cache base
  (``<base>/stm32-substrate``). Linux honours ``$XDG_CACHE_HOME`` and falls
  back to ``~/.cache``; Windows uses ``%LOCALAPPDATA%`` (fallback
  ``~/AppData/Local``). Persistent (unlike the system temp dir, which is
  wiped on reboot / by storage cleaners), so deterministic per-project
  scratch — e.g. CubeIDE workspaces — keeps its incremental-build state
  across runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def user_cache_root() -> Path:
    """Persistent per-user cache base for substrate scratch state.

    Returns ``<platform cache base>/stm32-substrate``. The base is
    ``%LOCALAPPDATA%`` on Windows and ``$XDG_CACHE_HOME`` (else ``~/.cache``)
    elsewhere. The directory is not created here — callers append their own
    subtree and create it when needed.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            "~/AppData/Local"
        )
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "stm32-substrate"
