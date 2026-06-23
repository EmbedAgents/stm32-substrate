"""Guard: OS-specific process/locking primitives live ONLY in platform/*.

Hard rule 3 (Linux + Windows v1) requires that ``os.kill`` / ``signal`` /
``fcntl`` / ``msvcrt`` / ``winreg`` / ``OpenProcess`` / ``TerminateProcess``
never appear in business-logic code paths -- they route through
``embedagents.stm32.platform.*`` wrappers so the rest of the package stays
OS-agnostic. Until now this rule was enforced only by convention/grep; this
test makes it executable.

AST-based (not text/regex) on purpose: a regex scan false-positives on the
docstrings/comments that legitimately *mention* these primitives (e.g.
``subprocess_runner`` documenting why it does NOT use ``os.kill``). Walking
import/attribute nodes flags real usage only.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "embedagents" / "stm32"

# The sanctioned home for every OS-specific primitive.
_PLATFORM_PKG = "platform"

# Importing any of these modules outside platform/ is a violation.
_FORBIDDEN_IMPORTS = {"fcntl", "msvcrt", "winreg", "signal"}
# os.<attr> process-control calls.
_FORBIDDEN_OS_ATTRS = {"kill", "killpg"}
# kernel32 / Win32 process-control calls (any receiver).
_FORBIDDEN_WIN_ATTRS = {"OpenProcess", "TerminateProcess"}


def _violations_in(path: pathlib.Path, tree: ast.AST, rel: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _FORBIDDEN_IMPORTS:
                    out.append(f"{rel}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in _FORBIDDEN_IMPORTS:
                out.append(f"{rel}:{node.lineno} from {node.module} import ...")
        elif isinstance(node, ast.Attribute):
            if (
                node.attr in _FORBIDDEN_OS_ATTRS
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                out.append(f"{rel}:{node.lineno} os.{node.attr}")
            if node.attr in _FORBIDDEN_WIN_ATTRS:
                out.append(f"{rel}:{node.lineno} .{node.attr}")
    return out


def test_os_specific_primitives_only_in_platform_package() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel_to_pkg = path.relative_to(SRC)
        if rel_to_pkg.parts and rel_to_pkg.parts[0] == _PLATFORM_PKG:
            continue  # platform/* is where these primitives belong
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(SRC.parent.parent))
        offenders.extend(_violations_in(path, tree, rel))

    assert not offenders, (
        "OS-specific primitives outside embedagents/stm32/platform/ "
        "(hard rule 3 - route through platform.* wrappers):\n  "
        + "\n  ".join(offenders)
    )
