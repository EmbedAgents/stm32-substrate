"""Build-preset tables + family→FPU lookup for ``CubeIDE.build()``.

Per ``v1/cubeide-api.md`` § "Preset definitions" and the MR-1 closure
("Family → FPU table" — RES-020). The tables encode the multi-option
edits each preset applies to ``.cproject``; ``FAMILY_FPU_TABLE`` is the
substrate-side mapping from ``ctx.project.firmware.device_family`` to
``(-mfpu, -mfloat-abi)`` flag values.

TODO: data-drive from JSON once more variants accumulate.
"""

from __future__ import annotations

from typing import Literal


# One preset entry is one ``.cproject`` ``<option>`` edit:
#   (operation, superclass_regex, value)
# where operation is one of "set_value" / "append_list" / "remove_list".
#
# Value-string convention: a leading literal ``...`` is replaced at edit
# time by the matched option's actual ``superClass`` attribute (see
# ``CProjectEditor._edit_option``). Real CubeIDE encodes enumerated
# option values as ``<superClass>.value.<token>``; the placeholder lets
# one regex match both the synthetic test form (``gnu.c.compiler.option.
# optimization.level``) and the real ST form (``com.st.stm32cube.ide.
# mcu.gnu.managedbuild.tool.c.compiler.option.optimization.level``) with
# correct per-option prefix expansion.
PresetOp = tuple[
    Literal["set_value", "set_value_soft", "append_list", "remove_list"], str, str
]
# ``set_value_soft`` is a best-effort scalar set: identical to
# ``set_value`` but a soft no-op (WARNING) instead of a protocol failure
# when no matching ``<option>`` exists. Used for options real CubeIDE
# only plants after a GUI toggle (e.g. ``usenewlibnano``), which an
# untouched ST example project doesn't carry.


PRESET_FAST: tuple[PresetOp, ...] = (
    ("set_value", r".*\.compiler\.option\.optimization\.level", "....value.most"),       # -O3
    ("set_value", r".*\.compiler\.option\.debuglevel", "....value.g1"),                  # -g1
    ("append_list", r".*\.compiler\.option\.otherflags", "-flto"),
    ("append_list", r".*\.linker\.option\.otherflags", "-flto"),
    # FPU flags appended at runtime from FAMILY_FPU_TABLE when family
    # has an entry; soft-FP fallback otherwise (WARNING logged).
)

PRESET_SIZE: tuple[PresetOp, ...] = (
    ("set_value", r".*\.compiler\.option\.optimization\.level", "....value.size"),       # -Os
    ("set_value", r".*\.compiler\.option\.debuglevel", "....value.g1"),                  # -g1
    ("append_list", r".*\.linker\.option\.otherflags", "-Wl,--gc-sections"),
    # newlib-nano: best-effort. Real CubeIDE only carries the
    # ``usenewlibnano`` <option> once it's been toggled in the GUI; an
    # untouched ST example project lacks it, so a hard set_value would
    # raise. set_value_soft sets it where present, soft-no-ops otherwise.
    ("set_value_soft", r".*\.linker\.option\.usenewlibnano", "true"),
)

PRESET_BALANCED: tuple[PresetOp, ...] = (
    ("set_value", r".*\.compiler\.option\.optimization\.level", "....value.more"),       # -O2
    ("set_value", r".*\.compiler\.option\.debuglevel", "....value.g3"),                  # -g (CubeIDE Debug default)
)


PRESETS: dict[str, tuple[PresetOp, ...]] = {
    "fast": PRESET_FAST,
    "size": PRESET_SIZE,
    "balanced": PRESET_BALANCED,
}


# MR-1 closure (RES-020): family-prefix → (-mfpu, -mfloat-abi).
# Sourced from ``ctx.project.firmware.device_family`` at build time;
# substrate does NOT probe hardware to derive this.
FAMILY_FPU_TABLE: dict[str, tuple[str, str]] = {
    "STM32F3": ("fpv4-sp-d16", "hard"),
    "STM32F4": ("fpv4-sp-d16", "hard"),
    "STM32F7": ("fpv5-d16", "hard"),
    "STM32H7": ("fpv5-d16", "hard"),
    "STM32L4": ("fpv4-sp-d16", "hard"),
    # Families absent here fall through to soft-FP.
}


# User-facing compiler-flag aliases → CDT enum suffix (with "..." for
# per-option superClass expansion in CProjectEditor). Values not present
# in the map are written verbatim; this lets callers pass a fully-
# formed enum value when needed.
DEBUG_LEVEL_ALIASES: dict[str, str] = {
    "none": "....value.gnone",
    "-g1": "....value.g1",
    "-g": "....value.g2",
    "-g3": "....value.g3",
}

OPTIMIZATION_ALIASES: dict[str, str] = {
    "-O0": "....value.none",
    "-O1": "....value.optimize",
    "-O2": "....value.more",
    "-O3": "....value.most",
    "-Og": "....value.debug",
    "-Os": "....value.size",
    "-Ofast": "....value.ofast",
    "-Oz": "....value.oz",
}


def fpu_flags_for_family(device_family: str | None) -> tuple[str, str] | None:
    """Return ``(fpu_value, float_abi_value)`` for a family prefix, or ``None``.

    Used by ``build(preset="fast")`` to decide whether to append
    ``-mfpu=<x> -mfloat-abi=<y>`` to the compiler flags. ``None`` triggers
    the soft-FP fallback + WARNING log in the caller.
    """
    if not device_family:
        return None
    # Longest-prefix match: ``"STM32H7Sxx"`` should map via ``"STM32H7"``.
    matches = [
        family for family in FAMILY_FPU_TABLE if device_family.startswith(family)
    ]
    if not matches:
        return None
    longest = max(matches, key=len)
    return FAMILY_FPU_TABLE[longest]
