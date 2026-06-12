"""``stm32 vcp`` CLI subcommand group.

Per ``v1/vcp-api.md`` § "CLI subcommand surface". Four subcommands:

- ``vcp tail [--port] [--baud] [--last-n] [--follow] [--timeout]`` — VCP-001.
- ``vcp send LINE [--port] [--baud] [--terminator] [--timeout] [--inter-line-idle-ms] [--echo-filter]`` — VCP-002.
- ``vcp reconnect [--port] [--max-wait]`` — VCP-003.
- ``vcp close`` — explicit port release for handoff to external tools.

VCP intents in the slash-command surface route through ``/stm32agent``
(no ``/stm32vcp``). These ``stm32 vcp ...`` subcommands are for direct
terminal use.
"""

from __future__ import annotations

import argparse
import codecs
import sys

from stm32_substrate.cli._serialize import (
    dumps,
    serialise_error,
    serialise_unexpected,
)
from stm32_substrate.context import SubstrateContext
from stm32_substrate.errors import SubstrateError
from stm32_substrate.vcp import VCP


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "vcp",
        help="USB virtual COM port reader (VCP-001 / VCP-002 / VCP-003).",
    )
    sub = parser.add_subparsers(
        dest="vcp_subcommand",
        required=True,
        metavar="<subcommand>",
    )

    # ---- tail ---------------------------------------------------------
    p = sub.add_parser("tail", help="VCP-001 — yield serial lines as text.")
    p.add_argument("--port", default=None, help="explicit /dev/ttyACMx override")
    p.add_argument("--baud", type=int, default=None, help="baud rate (default 115200)")
    p.add_argument(
        "--last-n",
        type=int,
        default=None,
        help="recent buffered lines to flush before live tail (default 100)",
    )
    p.add_argument(
        "--follow",
        action="store_true",
        help="stream live lines (Ctrl-C to stop); else snapshot N lines and exit",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "snapshot: max seconds to wait for --last-n lines; with "
            "--follow: wall-clock bound on the stream (omit to stream "
            "until Ctrl-C)"
        ),
    )
    p.set_defaults(vcp_fn=_cmd_tail)

    # ---- send ---------------------------------------------------------
    p = sub.add_parser("send", help="VCP-002 — write a line, collect reply.")
    p.add_argument("line", help="line to send (terminator appended automatically)")
    p.add_argument("--port", default=None)
    p.add_argument("--baud", type=int, default=None)
    p.add_argument(
        "--terminator",
        default=None,
        help='line terminator (default "\\n"; pass "\\r\\n" for CRLF)',
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="wall-clock budget in seconds (default vcp.send_default_timeout_s)",
    )
    p.add_argument(
        "--inter-line-idle-ms",
        type=int,
        default=None,
        help="multi-line reply collection idle (default vcp.inter_line_idle_ms)",
    )
    p.add_argument(
        "--echo-filter",
        action="store_true",
        help="strip echoed sent-line from reply lines",
    )
    p.set_defaults(vcp_fn=_cmd_send)

    # ---- reconnect ----------------------------------------------------
    p = sub.add_parser("reconnect", help="VCP-003 — force-reconnect after reset.")
    p.add_argument("--port", default=None, help="explicit port override")
    p.add_argument(
        "--max-wait",
        type=float,
        default=None,
        help="seconds to wait for the device to re-enumerate (default 10.0)",
    )
    p.set_defaults(vcp_fn=_cmd_reconnect)

    # ---- close --------------------------------------------------------
    p = sub.add_parser(
        "close",
        help="release the active reader so an external tool can take the port.",
    )
    p.set_defaults(vcp_fn=_cmd_close)


def dispatch(args: argparse.Namespace) -> int:
    handler = args.vcp_fn
    try:
        ctx = SubstrateContext.from_environment()
        client = VCP(ctx)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    try:
        return handler(args, client)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2


def _cmd_tail(args: argparse.Namespace, client: VCP) -> int:
    """Stream lines to stdout. Snapshot exits when ``last_n`` is reached
    or ``timeout_s`` elapses; ``--follow`` runs until Ctrl-C."""
    try:
        for line in client.tail(
            port=args.port,
            baud=args.baud,
            last_n=args.last_n,
            follow=args.follow,
            timeout_s=args.timeout,
        ):
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        # Ctrl-C is the natural stop for follow mode; exit clean.
        pass
    return 0


def _decode_terminator(value: str | None) -> str | None:
    r"""Decode shell-typed backslash escapes (``"\r\n"`` -> CRLF).

    The help text tells users to pass ``"\r\n"``, but the shell delivers
    four literal characters - undecoded they went onto the wire and
    broke reply splitting (A-018). Real control characters (e.g. from
    ``$'\r\n'``) pass through unchanged.
    """
    if value is None:
        return None
    return codecs.decode(value, "unicode_escape")


def _cmd_send(args: argparse.Namespace, client: VCP) -> int:
    result = client.send_and_read(
        args.line,
        port=args.port,
        baud=args.baud,
        terminator=_decode_terminator(args.terminator),
        timeout_s=args.timeout,
        inter_line_idle_ms=args.inter_line_idle_ms,
        echo_filter=args.echo_filter,
    )
    sys.stdout.write(dumps(result, pretty=getattr(args, "pretty", False)) + "\n")
    return 0


def _cmd_reconnect(args: argparse.Namespace, client: VCP) -> int:
    result = client.reconnect(port=args.port, max_wait_s=args.max_wait)
    sys.stdout.write(dumps(result, pretty=getattr(args, "pretty", False)) + "\n")
    return 0


def _cmd_close(args: argparse.Namespace, client: VCP) -> int:
    client.close()
    return 0
