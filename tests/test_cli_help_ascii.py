"""All user-facing CLI help text must be pure ASCII.

Windows consoles on a non-UTF-8 code page (cp1252 / OEM cp850) mangle
characters like the em-dash (U+2014) or arrow (U+2192) into ``?`` / ``�``.
Linux + Windows are first-class targets (CLAUDE.md hard rule 3), so the
``stm32`` ``--help`` surface is held to ASCII. This walks the whole argparse
tree built by ``build_parser`` and fails loudly, naming the offending parser
and string, the moment a non-ASCII glyph creeps back into help/description text.
"""

from __future__ import annotations

import argparse

from embedagents.stm32.cli import build_parser


def _iter_help_strings(parser: argparse.ArgumentParser, prog: str):
    """Yield (location, text) for every user-facing string in the tree."""
    if parser.description:
        yield f"{prog} (description)", parser.description
    if parser.epilog:
        yield f"{prog} (epilog)", parser.epilog
    for action in parser._actions:
        if action.help:
            opt = "/".join(action.option_strings) or action.dest
            yield f"{prog} [{opt}] (help)", action.help
        if isinstance(action.metavar, str):
            yield f"{prog} [{action.dest}] (metavar)", action.metavar
        # Recurse into subparsers.
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _iter_help_strings(sub, f"{prog} {name}")


def test_cli_help_is_ascii() -> None:
    parser = build_parser()
    offenders = [
        (loc, text)
        for loc, text in _iter_help_strings(parser, parser.prog)
        if not text.isascii()
    ]
    assert not offenders, "non-ASCII in user-facing CLI help text:\n" + "\n".join(
        f"  {loc}: {text!r}" for loc, text in offenders
    )
