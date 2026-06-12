"""File-resolution helpers per ``v1/api-conventions.md`` § "Resolution
helpers" (R-002).

Two jobs, library-wide:

- **str tolerance** (IMP-22): every Path-typed public entry point accepts
  ``str | Path`` — a caller passing ``"firmware.elf"`` must never see a
  raw ``AttributeError`` off a string. (Claude's heredocs and plain
  Python callers naturally pass strings.)
- **Anchoring** (IMP-23): an explicit relative argument resolves against
  the *process* CWD (terminal semantics — the caller typed it where they
  stand); a relative *descriptor* value anchors to ``ctx.cwd`` (the
  descriptor's home), never the process CWD — running from another
  directory must not break descriptor paths (the RES-037 cubeide rule,
  generalized).

Never scans the filesystem for "the only .elf" or similar.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from embedagents.stm32.errors import ConfigurationError

if TYPE_CHECKING:
    from embedagents.stm32.context import SubstrateContext


def coerce_path(value: str | Path, *, anchor: Path | None = None) -> Path:
    """``str | Path`` → resolved ``Path``, anchored when relative.

    ``anchor=None`` resolves a relative path against the process CWD
    (explicit-argument semantics); pass ``anchor=ctx.cwd`` for
    descriptor-sourced values.
    """
    p = Path(value)
    if anchor is not None and not p.is_absolute():
        p = anchor / p
    return p.resolve()


def resolve_file(
    arg: str | Path | None,
    *,
    ctx: "SubstrateContext",
    descriptor_field: str,
    arg_name: str,
    required: bool = True,
) -> Path | None:
    """R-002 resolution order: explicit ``arg`` → descriptor field →
    loud ``ConfigurationError`` naming the field to set (or ``None``
    when ``required=False``).

    ``descriptor_field`` is the dotted descriptor path, e.g.
    ``"debug.elf_path"``.
    """
    if arg is not None:
        return coerce_path(arg)
    configured = _descriptor_lookup(ctx.project, descriptor_field)
    if configured:
        return coerce_path(configured, anchor=ctx.cwd)
    if not required:
        return None
    raise ConfigurationError(
        message=f"{arg_name}= not given and {descriptor_field} is unset",
        hint=(
            f"pass {arg_name}=... explicitly, or set {descriptor_field} "
            "in stm32-project.jsonc"
        ),
    )


def _descriptor_lookup(descriptor: object, dotted: str) -> object:
    obj: object = descriptor
    for part in dotted.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    return obj
