"""STM32 substrate — deterministic Python wrapper around ST's vendor CLIs.

``__version__`` is derived from the installed package metadata so it can never
drift from ``pyproject.toml``. In a bare source checkout (package not installed)
it falls back to a sentinel.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("embedagents-stm32")
except PackageNotFoundError:  # source checkout, not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
