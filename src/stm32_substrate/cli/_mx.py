"""``stm32 mx`` CLI subcommand group — cubemx-side operations.

Per ``v1/cubemx-api.md`` § "CLI subcommand surface". Only one
subcommand: ``mx generate``. Output is the ``CubeMXResult`` JSON;
``success=False`` exits 0 (failure is a result for caller scripts,
not a substrate error), and ``CubeMXError`` / ``ConfigurationError``
exit 1 with the error JSON on stderr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from stm32_substrate.cli._serialize import (
    dumps,
    serialise_error,
    serialise_unexpected,
)
from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubemx import CubeMX
from stm32_substrate.errors import SubstrateError


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``mx`` group on the top-level parser."""
    parser = subparsers.add_parser(
        "mx",
        help="STM32CubeMX project generation (MX-001 / CP-008).",
    )
    sub = parser.add_subparsers(
        dest="mx_subcommand",
        required=True,
        metavar="<subcommand>",
    )

    p = sub.add_parser(
        "generate",
        help="MX-001 — open IOC, generate project code into output_path.",
    )
    p.add_argument(
        "ioc",
        nargs="?",
        type=Path,
        default=None,
        help="path to .ioc (autodiscovered from descriptor cubemx.ioc_path if omitted)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output directory (default: ioc parent or descriptor field)",
    )
    p.add_argument(
        "--name",
        default=None,
        help="project name (default: ioc stem or descriptor field)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="initial budget in seconds (default: cubemx.long_call_s)",
    )
    p.set_defaults(mx_fn=_cmd_generate)


def dispatch(args: argparse.Namespace) -> int:
    handler = args.mx_fn
    try:
        ctx = SubstrateContext.from_environment()
        client = CubeMX(ctx)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    try:
        result = handler(args, client)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    sys.stdout.write(dumps(result, pretty=getattr(args, "pretty", False)) + "\n")
    # success=False is a result, not an error — exit 0 regardless.
    return 0


def _cmd_generate(args: argparse.Namespace, client: CubeMX) -> Any:
    return client.generate(
        args.ioc,
        output_path=args.output,
        project_name=args.name,
        timeout_s=args.timeout,
    )
