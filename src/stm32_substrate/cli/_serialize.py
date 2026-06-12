"""JSON serialization helpers for the ``stm32`` CLI surface.

Every CLI subcommand produces JSON on stdout (per
``v1/cubeprogrammer-api.md`` § "CLI subcommand surface" — "Output format:
stdout = JSON; pretty-printed via ``--pretty``"). Errors raised as
``SubstrateError`` are serialised here too for stderr output.

Shapes are stable: each dataclass becomes a flat / nested ``dict``;
``Path`` → ``str``; ``Enum`` → its name; ``set`` → sorted list.
"""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path
from typing import Any

from stm32_substrate.errors import SubstrateError


def to_dict(value: Any) -> Any:
    """Recursively reduce ``value`` to JSON-friendly primitives.

    Dataclasses → ``dict`` (via ``asdict``); ``list[T]`` / ``tuple[T, ...]``
    → element-wise reduced list; everything else passed through (callers
    rely on ``_json_default`` to handle outliers like ``Path``).
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    if isinstance(value, dict):
        return {k: to_dict(v) for k, v in value.items()}
    return value


def dumps(value: Any, *, pretty: bool = False) -> str:
    """JSON-serialise ``value`` with substrate-specific defaults."""
    indent = 2 if pretty else None
    return json.dumps(
        to_dict(value),
        default=_json_default,
        indent=indent,
        sort_keys=False,
    )


def serialise_error(err: SubstrateError) -> str:
    """JSON-serialise a ``SubstrateError`` for stderr output.

    Includes the substrate-error common fields + any per-tool subclass
    fields (markers, error_code, etc.) via ``asdict``.
    """
    payload: dict[str, Any] = {
        "error_type": type(err).__name__,
    }
    if dataclasses.is_dataclass(err):
        payload.update(dataclasses.asdict(err))
    else:
        payload["message"] = str(err)
    return json.dumps(payload, default=_json_default, indent=2)


def serialise_unexpected(err: BaseException) -> str:
    """JSON-serialise a non-``SubstrateError`` exception for stderr.

    The CLI boundary must never leak a raw Python traceback (HIL HARD
    RULE 1: fail loud *with a hint*, never crash raw). The library
    deliberately raises plain ``ValueError`` for bad arguments and
    ``NotImplementedError`` for not-yet-wired paths — a Pythonic contract
    for library callers, but on the CLI surface those would otherwise
    surface as a stack trace that a newcomer can't distinguish from a
    substrate bug. This wraps any such exception into the same structured
    envelope shape as ``serialise_error`` (``error_type`` + ``message`` +
    ``hint``) so the CLI always emits JSON, never a traceback.
    """
    return json.dumps(
        {
            "error_type": type(err).__name__,
            "message": str(err) or repr(err),
            "hint": (
                "the substrate could not complete this request — the message "
                "above says what to fix (usually a bad argument). If it looks "
                "like a substrate bug rather than your input, please report it."
            ),
            "recoverable": False,
        },
        default=_json_default,
        indent=2,
    )


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    raise TypeError(
        f"cannot JSON-serialise {type(obj).__name__!r} (value={obj!r})"
    )
