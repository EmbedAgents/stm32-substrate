"""``stm32 prog`` CLI subcommand group — cubeprogrammer-side operations.

Maps 1:1 to ``v1/cubeprogrammer-api.md`` § "CLI subcommand surface". Every
handler runs against a fresh ``CubeProgrammer`` constructed from
``SubstrateContext.from_environment()`` and emits JSON on stdout.

Exit-code conventions:

- Success → 0 (each handler returns ``(result, 0)``).
- ``ping-swd``: 0 when the target responds, 1 when not (per spec).
- Raised ``SubstrateError`` → handled by the dispatcher: JSON to stderr,
  exit 1.
- Unknown failures → exit 2 (per argparse convention).

Streaming subcommands (``swo``) emit one JSON object per line (NDJSON).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from embedagents.stm32.cli._serialize import (
    dumps,
    serialise_error,
    serialise_unexpected,
)
from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.errors import SubstrateError


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``prog`` group on the top-level parser."""
    parser = subparsers.add_parser(
        "prog",
        help="STM32_Programmer_CLI operations (D-* / F-* / DIAG-* / SWO).",
    )
    sub = parser.add_subparsers(
        dest="prog_subcommand",
        required=True,
        metavar="<subcommand>",
    )
    _register_subcommands(sub)


def dispatch(args: argparse.Namespace) -> int:
    """Run the parsed ``prog`` subcommand. Returns the process exit code."""
    handler: Callable[[argparse.Namespace, CubeProgrammer], tuple[Any, int]] = (
        args.prog_fn
    )
    try:
        ctx = SubstrateContext.from_environment()
        client = CubeProgrammer(ctx)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except Exception as err:  # CLI boundary: never leak a raw traceback (HARD RULE 1)
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    try:
        result, exit_code = handler(args, client)
    except SubstrateError as err:
        sys.stderr.write(serialise_error(err) + "\n")
        return 1
    except KeyboardInterrupt:
        # Streaming subcommands (swo) intentionally swallow Ctrl-C as the
        # natural stop signal. Other commands propagate.
        if getattr(args, "_prog_streams", False):
            return 0
        raise
    except Exception as err:  # CLI boundary: library ValueError/NotImplementedError
        # and any other unexpected error become a structured envelope, never
        # a raw Python traceback (HARD RULE 1: fail loud with a hint).
        sys.stderr.write(serialise_unexpected(err) + "\n")
        return 2

    if result is not None:
        sys.stdout.write(dumps(result, pretty=getattr(args, "pretty", False)) + "\n")
    return exit_code


# ---------------------------------------------------------------------------
# Registration — one block per subcommand
# ---------------------------------------------------------------------------


def _register_subcommands(sub: argparse._SubParsersAction) -> None:
    # ------- discovery (D-*) -------
    p = sub.add_parser("connect", help="D-001 - connect via SWD and return banner")
    p.add_argument("--ur", action="store_true", help="connect under reset (D-011)")
    p.add_argument("--freq", type=int, default=None, help="SWD frequency in kHz")
    p.set_defaults(prog_fn=_cmd_connect)

    p = sub.add_parser("diagnose-micro", help="D-002 - run the SWD recovery ladder")
    p.set_defaults(prog_fn=_cmd_diagnose_micro)

    p = sub.add_parser(
        "list-probes", help="D-005 - enumerate attached ST-LINK probes"
    )
    p.set_defaults(prog_fn=_cmd_list_probes)

    p = sub.add_parser(
        "ping-swd",
        help="D-006 - quick SWD-responsiveness probe (exit 0 / 1)",
    )
    p.set_defaults(prog_fn=_cmd_ping_swd)

    p = sub.add_parser("cores", help="D-007 - primary / secondary cores")
    p.set_defaults(prog_fn=_cmd_cores)

    p = sub.add_parser(
        "svd",
        help="D-008 - SVD file for the attached device (banner + svd_db lookup)",
    )
    p.set_defaults(prog_fn=_cmd_svd)

    p = sub.add_parser(
        "read-ob", help="D-009 - read option bytes via ``-ob displ``"
    )
    p.set_defaults(prog_fn=_cmd_read_ob)

    # ------- option-byte writes (F-021 + DIAG-018) -------
    p = sub.add_parser(
        "write-ob",
        help=(
            "F-021 - write option bytes. Pairs are NAME=VALUE arguments; "
            "values accept hex (0x..) / decimal / true / false / strings."
        ),
    )
    p.add_argument("pairs", nargs="+", help="NAME=VALUE")
    p.add_argument(
        "--confirm-destructive",
        action="store_true",
        help="grant the F-021 destructive gate (required for every OB write)",
    )
    p.add_argument(
        "--confirm-irreversible",
        action="store_true",
        help="grant the RDP-level-2 (0xCC) irreversibility gate",
    )
    p.set_defaults(prog_fn=_cmd_write_ob)

    p = sub.add_parser(
        "verify-ob", help="DIAG-018 - diff observed OB against expected"
    )
    p.add_argument("pairs", nargs="+", help="NAME=VALUE")
    p.set_defaults(prog_fn=_cmd_verify_ob)

    # ------- atomic target control (F-001/002/016/017/018) -------
    p = sub.add_parser("erase", help="F-001 / F-002 - mass erase chip")
    p.add_argument(
        "--with-reset",
        action="store_true",
        help="combine with reset (selects F-002 ``-e all -rst``)",
    )
    p.add_argument(
        "--confirm-destructive",
        action="store_true",
        help=(
            "grant the destructive gate - mass erase wipes the entire flash "
            "and is irreversible (HIL rule; required)"
        ),
    )
    p.set_defaults(prog_fn=_cmd_erase)

    p = sub.add_parser("reset", help="F-016 - software / hardware reset")
    p.add_argument(
        "--hard",
        action="store_true",
        help="use ``-hardRst`` instead of ``-rst``",
    )
    p.set_defaults(prog_fn=_cmd_reset)

    p = sub.add_parser("halt", help="F-017 - halt the target CPU")
    p.set_defaults(prog_fn=_cmd_halt)

    p = sub.add_parser("resume", help="F-018 - resume the target CPU")
    p.set_defaults(prog_fn=_cmd_resume)

    # ------- flash family -------
    p = sub.add_parser(
        "flash",
        help=(
            "CP-001 - extension-based router: .elf/.hex -> flash_file, "
            ".bin -> flash_bin (with --address) / flash_bin_no_address (without)"
        ),
    )
    p.add_argument("file", type=Path)
    p.add_argument("--address", default=None)
    p.add_argument(
        "--confirm-inferred-address",
        action="store_true",
        help=(
            "approve flashing a .bin to the inferred 0x08000000 when "
            "--address is omitted (a destructive write to a guessed address)"
        ),
    )
    p.set_defaults(prog_fn=_cmd_flash)

    p = sub.add_parser("flash-data", help="F-007 - flash a non-firmware payload")
    p.add_argument("file", type=Path)
    p.add_argument("--address", required=True)
    p.set_defaults(prog_fn=_cmd_flash_data)

    p = sub.add_parser("flash-signed", help="F-006 - flash a signed binary")
    p.add_argument("file", type=Path)
    p.add_argument("--address", default=None)
    p.set_defaults(prog_fn=_cmd_flash_signed)

    p = sub.add_parser(
        "flash-pair", help="F-008 / F-009 - sequential bootloader + app flash"
    )
    p.add_argument("bootloader", type=Path)
    p.add_argument("application", type=Path)
    p.add_argument("--boot-address", default=None)
    p.add_argument("--app-address", default=None)
    p.add_argument(
        "--signed",
        action="store_true",
        help="use flash_signed_pair instead of flash_pair",
    )
    p.add_argument(
        "--sign-unsigned",
        action="store_true",
        help=(
            "with --signed, sign inputs that lack the ST image header via "
            "SigningTool first (needs --header-version; entry points for "
            "fsbl/ssbl)"
        ),
    )
    p.add_argument(
        "--header-version",
        default=None,
        help="signing header version for --sign-unsigned (1|2|2.1|2.2|2.3)",
    )
    p.add_argument(
        "--boot-entry-point",
        default=None,
        help="bootloader entry point for --sign-unsigned (fsbl needs one)",
    )
    p.add_argument(
        "--app-entry-point",
        default=None,
        help="application entry point for --sign-unsigned (ssbl needs one)",
    )
    p.add_argument(
        "--no-key",
        action="store_true",
        help=(
            "with --sign-unsigned, disable authentication (-nk) for the "
            "signing legs; dev-only (keyed hv>=2 signing needs provisioned "
            "key material)"
        ),
    )
    p.set_defaults(prog_fn=_cmd_flash_pair)

    p = sub.add_parser(
        "flash-external", help="F-010 - external loader (``-el``) flash"
    )
    p.add_argument("file", type=Path)
    p.add_argument("--address", required=True)
    p.add_argument(
        "--loader",
        type=Path,
        default=None,
        help="explicit .stldr path (skips auto-discovery)",
    )
    p.set_defaults(prog_fn=_cmd_flash_external)

    p = sub.add_parser("flash-bank", help="F-011 - flash a specific bank (1 or 2)")
    p.add_argument("bank", type=int, choices=(1, 2))
    p.add_argument("file", type=Path)
    p.add_argument("--address", required=True)
    p.set_defaults(prog_fn=_cmd_flash_bank)

    # ------- read family -------
    p = sub.add_parser("read-flash", help="F-019 - dump flash to file")
    p.add_argument("--address", default=None)
    p.add_argument("--size", type=int, default=None)
    p.add_argument("--output", type=Path, default=None)
    p.set_defaults(prog_fn=_cmd_read_flash)

    p = sub.add_parser("read-mem", help="F-020 - peek at memory")
    p.add_argument("--address", required=True)
    p.add_argument("--size", type=int, default=None)
    p.set_defaults(prog_fn=_cmd_read_mem)

    # ------- diagnostic (DIAG-001 binary path) -------
    p = sub.add_parser(
        "hardfault",
        help="DIAG-001 binary-only path - ``-hf`` Hard Fault Analyzer",
    )
    p.set_defaults(prog_fn=_cmd_hardfault)

    # ------- SWO stream (VCP-007) -------
    p = sub.add_parser(
        "swo",
        help=(
            "VCP-007 - tail SWO/ITM stream as newline-delimited JSON. "
            "Press Ctrl-C to stop."
        ),
    )
    p.add_argument("--freq", type=float, required=True, help="SWO clock in MHz")
    p.add_argument(
        "--port", type=int, default=0, help="ITM port number (default 0)"
    )
    p.add_argument(
        "--log", type=Path, default=None, help="CLI-side raw capture file"
    )
    p.set_defaults(prog_fn=_cmd_swo, _prog_streams=True)

    # ------- signing (F-013, routed through /stm32prog per ADR-002 §M1) -------
    p = sub.add_parser(
        "sign",
        help=(
            "F-013 - sign a .bin via STM32_SigningTool_CLI (N6 / MP1 / MP2). "
            "Substrate doesn't pre-check device family; vendor reports."
        ),
    )
    p.add_argument("file", type=Path, help="input binary to sign")
    p.add_argument("--la", dest="load_address", required=True, help="load address (hex)")
    p.add_argument(
        "--type",
        dest="image_type",
        required=True,
        choices=("ssbl", "fsbl", "teeh", "teed", "teex", "copro"),
    )
    p.add_argument(
        "--hv",
        dest="header_version",
        required=True,
        choices=("1", "2", "2.1", "2.2", "2.3"),
    )
    p.add_argument(
        "--ep",
        dest="entry_point",
        default=None,
        help="entry point (required for fsbl/ssbl; substrate raises on miss)",
    )
    p.add_argument("--of", dest="option_flags", default=None)
    p.add_argument(
        "--no-key",
        action="store_true",
        help="disable authentication (-nk); dev-only; logs WARNING",
    )
    align_group = p.add_mutually_exclusive_group()
    align_group.add_argument(
        "--align",
        dest="align",
        action="store_const",
        const=True,
        default=None,
        help="pass --align to SigningTool",
    )
    align_group.add_argument(
        "--no-align",
        dest="align",
        action="store_const",
        const=False,
        help="explicitly disable --align (diagnostic; conflicts with N6+hv=2.3)",
    )
    p.add_argument(
        "-o", "--output", dest="output", type=Path, default=None,
        help="output path (default: <input>-trusted<ext>)",
    )
    p.add_argument(
        "--device-family",
        dest="device_family",
        default=None,
        help="informational hint; enables --align auto-set for STM32N6 hv=2.3",
    )
    p.set_defaults(prog_fn=_cmd_sign)


# ---------------------------------------------------------------------------
# Handlers — each returns (result-to-serialise-or-None, exit_code)
# ---------------------------------------------------------------------------


def _cmd_connect(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    if args.ur:
        return client.connect_under_reset(), 0
    return client.connect(freq_khz=args.freq), 0


def _cmd_diagnose_micro(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.diagnose_micro(), 0


def _cmd_list_probes(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.list_probes(), 0


def _cmd_ping_swd(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    result = client.ping_swd()
    # Spec: exit 0 if responding, exit 1 if not. The JSON output still
    # carries the reason for the False case.
    return result, 0 if result.value else 1


def _cmd_svd(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.svd_for_attached(), 0


def _cmd_cores(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.cores(), 0


def _cmd_read_ob(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.read_option_bytes(), 0


def _cmd_write_ob(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    pairs = dict(_parse_pair(s) for s in args.pairs)
    return (
        client.write_option_bytes(
            pairs,
            confirm_destructive=args.confirm_destructive,
            confirm_irreversible=args.confirm_irreversible,
        ),
        0,
    )


def _cmd_verify_ob(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    pairs = dict(_parse_pair(s) for s in args.pairs)
    return client.verify_option_bytes(pairs), 0


def _cmd_erase(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    if args.with_reset:
        return client.erase_and_reset(
            confirm_destructive=args.confirm_destructive
        ), 0
    return client.erase_chip(confirm_destructive=args.confirm_destructive), 0


def _cmd_reset(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.reset(hard=args.hard), 0


def _cmd_halt(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.halt(), 0


def _cmd_resume(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.resume(), 0


def _cmd_flash(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    # A .bin with no --address infers 0x08000000 and writes there — a
    # destructive write to a guessed address. Gate it on the CLI: the user
    # must pass --confirm-inferred-address. (For .elf/.hex and .bin+address
    # the router never consults on_confirm.)
    confirmed = bool(getattr(args, "confirm_inferred_address", False))
    return (
        client.download_image(
            args.file,
            address=args.address,
            on_confirm=lambda _inferred: confirmed,
        ),
        0,
    )


def _cmd_flash_data(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.flash_data(args.file, args.address), 0


def _cmd_flash_signed(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.flash_signed(args.file, address=args.address), 0


def _cmd_flash_pair(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    if args.signed:
        return (
            client.flash_signed_pair(
                args.bootloader,
                args.application,
                bootloader_address=args.boot_address,
                application_address=args.app_address,
                sign_unsigned=args.sign_unsigned,
                signing_header_version=args.header_version,
                bootloader_entry_point=args.boot_entry_point,
                application_entry_point=args.app_entry_point,
                signing_no_key=args.no_key,
            ),
            0,
        )
    return (
        client.flash_pair(
            args.bootloader,
            args.application,
            bootloader_address=args.boot_address,
            application_address=args.app_address,
        ),
        0,
    )


def _cmd_flash_external(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return (
        client.flash_external(
            args.file, args.address, loader_path=args.loader
        ),
        0,
    )


def _cmd_flash_bank(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.flash_to_bank(args.file, args.bank, args.address), 0


def _cmd_read_flash(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return (
        client.read_flash_to_file(
            address=args.address, size=args.size, output_path=args.output
        ),
        0,
    )


def _cmd_read_mem(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.read_memory(args.address, size=args.size), 0


def _cmd_hardfault(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    return client.analyze_hardfault(), 0


def _cmd_swo(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    """Stream ITM records as NDJSON (one JSON object per line).

    Returns ``(None, 0)`` to signal the dispatcher to skip the bulk-dump
    behaviour — every record has already been emitted as it arrived.
    """
    for record in client.tail_swo(
        freq_mhz=args.freq, port_number=args.port, log_path=args.log
    ):
        sys.stdout.write(dumps(record, pretty=False) + "\n")
        sys.stdout.flush()
    return None, 0


def _cmd_sign(
    args: argparse.Namespace, client: CubeProgrammer
) -> tuple[Any, int]:
    """F-013 — route to ``signing.SigningTool`` (lives outside the
    cubeprogrammer module per ADR-002 §M1).

    ``client`` is the cubeprogrammer instance (the dispatcher builds it
    unconditionally), but this handler ignores it and constructs a fresh
    ``SigningTool(client.ctx)`` against the same SubstrateContext.
    """
    from embedagents.stm32.signing import SigningTool

    tool = SigningTool(client.ctx)
    result = tool.sign_binary(
        args.file,
        load_address=args.load_address,
        image_type=args.image_type,
        header_version=args.header_version,
        entry_point=args.entry_point,
        option_flags=args.option_flags,
        no_key=args.no_key,
        align=args.align,
        output_path=args.output,
        device_family=args.device_family,
    )
    return result, 0


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _parse_pair(s: str) -> tuple[str, int | str | bool]:
    """Parse a CLI ``NAME=VALUE`` argument.

    Coercion rules (mirror the F-021 / DIAG-018 spec):

    - ``true`` / ``false`` (case-insensitive) → ``bool``.
    - ``0x...`` → ``int`` parsed as hex.
    - all-digits → ``int`` parsed as decimal.
    - anything else → ``str`` (passthrough; substrate-side renderer
      preserves user formatting).
    """
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            f"expected NAME=VALUE, got {s!r}"
        )
    name, _, raw = s.partition("=")
    if not name or not raw:
        raise argparse.ArgumentTypeError(
            f"NAME=VALUE both required, got {s!r}"
        )
    lower = raw.lower()
    if lower in ("true", "false"):
        return name, lower == "true"
    if lower.startswith("0x"):
        try:
            return name, int(raw, 16)
        except ValueError:
            return name, raw
    if raw.isdigit():
        return name, int(raw)
    return name, raw
