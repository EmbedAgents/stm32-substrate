"""Guard: no non-ASCII characters in runtime string literals (RES-053).

User-facing error ``message``/``hint`` strings and log messages print to the
console; on a Windows OEM/cp1252 console any non-ASCII character (em-dash,
``>=``/``<=`` math glyphs, ellipsis, arrows, ``section`` glyph) renders as
mojibake. Hard rule 3 makes Windows first-class, so runtime strings stay ASCII.

Docstrings and comments are exempt — they are not emitted at runtime.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "embedagents" / "stm32"


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """ids() of string Constant nodes that are docstrings (module/class/func)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def test_no_nonascii_in_runtime_string_literals() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                bad = sorted({c for c in node.value if ord(c) > 127})
                if bad:
                    rel = path.relative_to(SRC.parent.parent)
                    offenders.append(
                        f"{rel}:{node.lineno} {bad!r} in {node.value[:60]!r}"
                    )

    assert not offenders, (
        "non-ASCII in runtime string literals (mangles on Windows cp1252 "
        "consoles; ASCII-ify per RES-053):\n  " + "\n  ".join(offenders)
    )
