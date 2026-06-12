"""Minimal JSONC loader (strip ``//`` + ``/* ... */`` comments, accept trailing commas).

Substrate uses JSONC for human-edited config files per SC-005. The stdlib
``json`` module does not accept comments, so we strip them in a single pass
that is aware of string literals (a ``"// not a comment"`` string is
preserved untouched).

Public surface:
    ``load_jsonc(text)`` — parse a JSONC string and return the value.
    ``load_jsonc_file(path)`` — read + parse a JSONC file.

The stripper handles the common cases used by ST-tooling configs:

- ``//`` line comments
- ``/* ... */`` block comments (single-line and multi-line)
- trailing commas immediately before ``}`` or ``]``
- string escapes (``\\"``) inside strings

It is intentionally not a full JSON5 parser. For v1, simpler is better
(M-018). If users want JSON5 features (unquoted keys, single quotes,
hex numbers), the substrate raises a clean parse error pointing at the
offending position.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_jsonc(text: str) -> Any:
    """Parse a JSONC string and return the decoded value."""
    return json.loads(_strip(text))


def load_jsonc_file(path: Path) -> Any:
    """Read + parse a JSONC file."""
    return load_jsonc(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Stripper
# ---------------------------------------------------------------------------


def _strip(text: str) -> str:
    """Return ``text`` with comments and trailing commas removed.

    State machine over characters: outside strings, ``//`` starts a line
    comment and ``/*`` starts a block comment. Inside strings (including
    escapes), everything is preserved verbatim.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]

        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                # Line comment — skip to end of line (preserve the newline so
                # error positions stay aligned with the source).
                j = text.find("\n", i + 2)
                i = n if j == -1 else j
                continue
            if nxt == "*":
                # Block comment — skip to matching ``*/``.
                end = text.find("*/", i + 2)
                if end == -1:
                    # Unterminated; let json.loads surface the resulting
                    # error at this column.
                    i = n
                else:
                    i = end + 2
                continue

        out.append(ch)
        i += 1

    return _strip_trailing_commas("".join(out))


def _strip_trailing_commas(text: str) -> str:
    """Remove commas that immediately precede ``}`` or ``]`` (ignoring whitespace).

    Same string-aware state machine, in reverse-look style: emit characters
    one at a time, but defer emitting a comma until we see what's next.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            # Look ahead past whitespace; if next non-space is `}` or `]`,
            # drop the comma.
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)
