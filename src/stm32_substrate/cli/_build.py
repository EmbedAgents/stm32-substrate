"""``stm32 build`` CLI subcommand group — cubeide-side operations.

Maps to the CubeIDE API spec § "CLI subcommand surface". The base
``stm32 build`` accepts all simple flags (project / config / clean /
debug-level / opt / preset / all-configs); action sub-subcommands
(``add-symbol`` / ``add-lib`` / ``add-source`` / ``add-include``) handle
the list-shaped edits; discovery sub-subcommands (``in-folder`` /
``named``) chain ``find_project`` + ``build``.

Output:

- Successful build (success=True) → exit 0 with ``BuildResult`` JSON on
  stdout. ``console_output`` mirrored to stderr so users see the build
  log in their terminal.
- **Build-level failure** (compile / link errors → ``success=False``) →
  **exit 0**: build failure is a result the user scripts check via
  ``BuildResult.success``. console_output still mirrored.
- Substrate-side failure (``CubeIDEError`` / ``WorkspaceLockedError`` /
  ``CProjectEditError``) → exit 1 with the error JSON on stderr.
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
from stm32_substrate.cubeide import CubeIDE
from stm32_substrate.errors import SubstrateError


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``build`` group on the top-level parser."""
    parser = subparsers.add_parser(
        "build",
        help="STM32CubeIDE headless build (B-* prompts).",
    )
    _add_common_flags(parser, include_edit_flags=True)
    parser.set_defaults(build_fn=_cmd_base_build)

    sub = parser.add_subparsers(
        dest="build_action",
        required=False,
        metavar="<action>",
    )

    # ---- add-symbol ----
    p = sub.add_parser(
        "add-symbol",
        help="B-011 — append preprocessor symbols (one or more NAME[=VALUE]).",
    )
    p.add_argument("symbols", nargs="+", help="NAME or NAME=VALUE")
    _add_common_flags(p, include_edit_flags=False)
    p.set_defaults(build_fn=_cmd_add_symbol)

    # ---- add-lib ----
    p = sub.add_parser(
        "add-lib",
        help="B-012 — append linker libraries (paths).",
    )
    p.add_argument("libs", nargs="+", type=Path)
    _add_common_flags(p, include_edit_flags=False)
    p.set_defaults(build_fn=_cmd_add_lib)

    # ---- add-source ----
    p = sub.add_parser(
        "add-source",
        help="B-013 — append source files. v1 records only (tracks aux).",
    )
    p.add_argument("sources", nargs="+", type=Path)
    p.add_argument(
        "--target",
        type=Path,
        default=None,
        help="optional target directory to copy each source into (paired tuple)",
    )
    _add_common_flags(p, include_edit_flags=False)
    p.set_defaults(build_fn=_cmd_add_source)

    # ---- add-include ----
    p = sub.add_parser(
        "add-include",
        help="B-014 — append compiler include paths.",
    )
    p.add_argument("includes", nargs="+")
    _add_common_flags(p, include_edit_flags=False)
    p.set_defaults(build_fn=_cmd_add_include)

    # ---- in-folder ----
    p = sub.add_parser(
        "in-folder",
        help="B-018 — discover a project under FOLDER (one match) then build.",
    )
    p.add_argument(
        "folder",
        nargs="?",
        default=None,
        type=Path,
        help="defaults to ctx.cwd",
    )
    p.add_argument("--config", default=None)
    p.add_argument("--clean", action="store_true")
    p.set_defaults(build_fn=_cmd_in_folder)

    # ---- named ----
    p = sub.add_parser(
        "named",
        help="B-019 — discover by name (exact > substring) then build.",
    )
    p.add_argument("name")
    p.add_argument("--folder", type=Path, default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--clean", action="store_true")
    p.set_defaults(build_fn=_cmd_named)


def dispatch(args: argparse.Namespace) -> int:
    """Run the parsed build subcommand. Returns the process exit code.

    Build-level failures (``success=False``) exit 0 — the user inspects
    the JSON. Substrate-side failures exit 1 with error JSON on stderr.
    """
    handler = args.build_fn
    try:
        ctx = SubstrateContext.from_environment()
        client = CubeIDE(ctx)
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

    # BuildResult.console_output also goes to stderr so the user sees the
    # build log without parsing the JSON envelope.
    if getattr(result, "console_output", None):
        sys.stderr.write(result.console_output)
        if not result.console_output.endswith("\n"):
            sys.stderr.write("\n")

    sys.stdout.write(dumps(result, pretty=getattr(args, "pretty", False)) + "\n")
    return 0


# ---------------------------------------------------------------------------
# Shared flag helper
# ---------------------------------------------------------------------------


def _add_common_flags(parser: argparse.ArgumentParser, *, include_edit_flags: bool) -> None:
    parser.add_argument("--project", type=Path, default=None)
    parser.add_argument("--config", default=None, help="CDT configuration name (e.g. Debug)")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--all-configs",
        action="store_true",
        help="apply settings edits to every configuration (not just active)",
    )
    if include_edit_flags:
        parser.add_argument("--debug-level", default=None, dest="debug_level")
        parser.add_argument("--opt", default=None, dest="optimization")
        parser.add_argument("--preset", default=None, help="fast / size / balanced")


def _common_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "project": getattr(args, "project", None),
        "configuration": getattr(args, "config", None),
        "clean": getattr(args, "clean", False),
    }
    if getattr(args, "all_configs", False):
        kwargs["modify_all_configurations"] = True
    return kwargs


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _cmd_base_build(args: argparse.Namespace, client: CubeIDE) -> Any:
    """``stm32 build`` (no sub-subcommand) — base build with simple flags."""
    kwargs = _common_kwargs(args)
    kwargs["debug_level"] = getattr(args, "debug_level", None)
    kwargs["optimization"] = getattr(args, "optimization", None)
    kwargs["preset"] = getattr(args, "preset", None)
    return client.build(**kwargs)


def _cmd_add_symbol(args: argparse.Namespace, client: CubeIDE) -> Any:
    parsed = [_parse_symbol(s) for s in args.symbols]
    kwargs = _common_kwargs(args)
    kwargs["add_symbols"] = parsed
    return client.build(**kwargs)


def _cmd_add_lib(args: argparse.Namespace, client: CubeIDE) -> Any:
    kwargs = _common_kwargs(args)
    kwargs["add_libraries"] = list(args.libs)
    return client.build(**kwargs)


def _cmd_add_source(args: argparse.Namespace, client: CubeIDE) -> Any:
    sources: list = list(args.sources)
    if args.target is not None:
        sources = [(p, args.target) for p in sources]
    kwargs = _common_kwargs(args)
    kwargs["add_sources"] = sources
    return client.build(**kwargs)


def _cmd_add_include(args: argparse.Namespace, client: CubeIDE) -> Any:
    kwargs = _common_kwargs(args)
    kwargs["add_include_paths"] = list(args.includes)
    return client.build(**kwargs)


def _cmd_in_folder(args: argparse.Namespace, client: CubeIDE) -> Any:
    found = client.find_project(folder=args.folder)
    return client.build(
        project=found.path,
        configuration=args.config,
        clean=args.clean,
    )


def _cmd_named(args: argparse.Namespace, client: CubeIDE) -> Any:
    found = client.find_project(folder=args.folder, name=args.name)
    return client.build(
        project=found.path,
        configuration=args.config,
        clean=args.clean,
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_symbol(s: str) -> str | tuple[str, str]:
    """Parse ``NAME`` or ``NAME=VALUE`` for ``add-symbol``."""
    if "=" not in s:
        return s
    name, _, value = s.partition("=")
    if not name:
        raise argparse.ArgumentTypeError(
            f"add-symbol expects NAME or NAME=VALUE, got {s!r}"
        )
    return (name, value)
