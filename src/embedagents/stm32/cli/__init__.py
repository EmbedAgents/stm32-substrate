"""Top-level ``stm32`` CLI entry point.

Aggregates five per-tool subparser groups — ``prog`` (cubeprogrammer +
signing per ADR-002 §M1), ``build`` (cubeide), ``mx`` (cubemx), ``debug``
(gdbserver + arm-gdb), ``vcp`` (USB virtual COM) — and routes parsed args
to each group's ``dispatch``.

Per ``v1/api-conventions.md`` § "Logging and progress streaming", the
library does NOT configure logging handlers — the CLI does. ``main()``
installs a stderr handler with a structured-field formatter, scoped to
the ``embedagents.stm32`` root logger.

TODO(v1+): ``--project`` / ``--tools-config`` / ``--defaults-config``
overrides plumbed into ``SubstrateContext.from_environment``; current Pass-1
surface uses repo-walked discovery only.
"""

from __future__ import annotations

import argparse
import logging
import sys

from embedagents.stm32 import __version__
from embedagents.stm32.cli import _build, _debug, _mx, _prog, _vcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stm32",
        description=(
            "STM32 substrate CLI — wraps STM32_Programmer_CLI / CubeIDE / "
            "CubeMX / ST-LINK_gdbserver / arm-none-eabi-gdb / "
            "STM32_SigningTool_CLI behind a unified surface."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"embedagents-stm32 {__version__}",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON output (default: compact)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity (-v INFO, -vv DEBUG)",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="<group>",
    )
    _prog.add_subparser(subparsers)
    _build.add_subparser(subparsers)
    _mx.add_subparser(subparsers)
    _debug.add_subparser(subparsers)
    _vcp.add_subparser(subparsers)

    if argv is None:
        argv = sys.argv[1:]
    # `stm32 build PATH` ergonomics: route a leading non-action positional
    # into --project before argparse sees the build subtree. The global
    # flags (--pretty, -v) never consume a value, so the first non-flag
    # token is always the command.
    for i, token in enumerate(argv):
        if not token.startswith("-"):
            if token == "build":
                argv = [*argv[: i + 1], *_build.pre_parse_argv(argv[i + 1 :])]
            break

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "prog":
        return _prog.dispatch(args)
    if args.command == "build":
        return _build.dispatch(args)
    if args.command == "mx":
        return _mx.dispatch(args)
    if args.command == "debug":
        return _debug.dispatch(args)
    if args.command == "vcp":
        return _vcp.dispatch(args)

    # argparse should reject unknown commands before reaching this
    # branch — defensive only.
    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; argparse.error raises SystemExit


def _configure_logging(verbosity: int) -> None:
    """Attach a stderr handler to the substrate root logger.

    ``-v`` → INFO; ``-vv`` → DEBUG; default → WARNING.
    """
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    root = logging.getLogger("embedagents.stm32")
    # Don't double-install when ``main()`` is called multiple times in
    # the same Python process (e.g. tests).
    if not any(getattr(h, "_substrate_installed", False) for h in root.handlers):
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s: %(message)s")
        )
        handler._substrate_installed = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)


if __name__ == "__main__":
    sys.exit(main())
