"""``python -m embedagents.stm32`` entry point.

Mirrors the ``stm32`` console script (``pyproject [project.scripts]`` ->
``embedagents.stm32.cli:main``). This PATH-independent form is the fallback the
slash commands use on Windows, where a per-user ``pip install`` drops
``stm32.exe`` into a Scripts directory that is not on ``PATH`` by default;
``python -m embedagents.stm32`` works wherever the interpreter is reachable.
"""

from __future__ import annotations

import sys

from embedagents.stm32.cli import main

if __name__ == "__main__":
    sys.exit(main())
