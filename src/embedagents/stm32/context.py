"""SubstrateContext + configuration loading.

Implements ``SubstrateContext.from_environment()`` per M-016 and the
resolution conventions R-002 / R-003 / R-004. Bad configs raise
``ConfigurationError`` with the loud-error fields documented in
``v1/api-conventions.md`` § "Configuration validation".

Public surface:

- ``SubstrateContext`` — aggregate context object, dependency-injected into
  every tool wrapper.
- ``ToolPaths`` — typed resolved tool binary paths; attribute names match
  the conventions across v1 specs (``cube_programmer_cli``, ``cubeide_path``,
  ``cubemx_executable``, ``stlink_gdbserver``, ``arm_gdb``,
  ``stm32_signing_tool_cli``, ``stm32cubeclt_path``).
- ``RuntimeDefaults`` / ``ProjectDescriptor`` — dict-backed wrappers with
  dotted-attribute access (e.g. ``ctx.defaults.programmer.connect_timeout_s``).
  The schema-validated dict is also exposed as ``self._raw``.
- ``SessionState`` — mutable session-scope state (active debug session,
  active VCP reader, T3 fields).

The supported-platform check is the first thing ``from_environment()``
does, per ADR-007 (supersedes ADR-005): Linux and Windows are first-class
v1 hosts; macOS is deferred pending demand.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from embedagents.stm32._jsonc import load_jsonc, load_jsonc_file
from embedagents.stm32.errors import ConfigurationError

_log = logging.getLogger("embedagents.stm32.context")


# ---------------------------------------------------------------------------
# Dict-backed wrappers (dotted access + raw dict)
# ---------------------------------------------------------------------------


class _DictBacked:
    """Internal helper: read-only dotted-attribute access over a dict.

    Nested dicts become ``_DictBacked`` instances recursively. Lists are
    preserved as lists (element-wise coerced). The full dict is exposed via
    ``self._raw`` for callers that prefer dict access.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        object.__setattr__(self, "_raw", raw)
        for k, v in raw.items():
            object.__setattr__(self, k, _coerce(v))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"{type(self).__name__} is read-only; mutate via the source file + reload."
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({sorted(self._raw)})"


def _coerce(value: Any) -> Any:
    if isinstance(value, dict):
        return _DictBacked(value)
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    return value


class RuntimeDefaults(_DictBacked):
    """Parsed ``stm32-runtime-defaults.jsonc``; schema-validated.

    Access knobs as nested attributes:
        ``ctx.defaults.programmer.connect_timeout_s``
        ``ctx.defaults.cubemx.long_call_s``
    """


class ProjectDescriptor(_DictBacked):
    """Parsed ``stm32-project.jsonc``; schema-validated.

    Carries the absolute path of the descriptor file in ``source_path`` so
    wrappers can resolve relative paths inside it.
    """

    def __init__(self, raw: dict[str, Any], source_path: Path) -> None:
        super().__init__(raw)
        object.__setattr__(self, "source_path", source_path)


# ---------------------------------------------------------------------------
# ToolPaths (typed, resolved at load time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPaths:
    """Resolved tool binary paths.

    Each field is the resolved ``Path`` or ``None`` if no candidate
    matched. Tool wrappers raise ``ConfigurationError`` lazily on first use
    when their required path is ``None`` — most workflows do not need every
    tool present.
    """

    cube_programmer_cli: Path | None = None
    cubeide_path: Path | None = None
    cubeide_headless_build: Path | None = None
    cubemx_executable: Path | None = None
    stlink_gdbserver: Path | None = None
    arm_gdb: Path | None = None
    stm32_signing_tool_cli: Path | None = None
    stm32cubeclt_path: Path | None = None


# Schema-key (under ``tools.``) → ToolPaths attribute. Some schema entries
# resolve to install roots vs binaries; the attribute name reflects the
# consumer-side meaning per the v1 per-tool specs.
_SCHEMA_KEY_TO_ATTR: dict[str, str] = {
    "cube_programmer": "cube_programmer_cli",
    "cubeide": "cubeide_path",
    "cubemx": "cubemx_executable",
    "stlink_gdb_server": "stlink_gdbserver",
    "arm_gdb": "arm_gdb",
    "stm32_signing_tool_cli": "stm32_signing_tool_cli",
    "stm32cubeclt": "stm32cubeclt_path",
}


# Built-in tool definitions per the v1 per-tool API spec loud-error formats.
# Used as the resolution chain when ``stm32-tools.local.jsonc`` is absent
# or does not override a given tool. Env vars are documented in the
# corresponding per-tool spec's "Substrate-shared errors raised by this
# module" section.
_BUILTIN_TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    "cube_programmer": {
        "env_var": "STM32_PROGRAMMER_CLI",
        "executable_name": "STM32_Programmer_CLI",
    },
    "cubeide": {
        "env_var": "STM32CUBEIDE",
        "executable_name": "stm32cubeide",
    },
    "cubemx": {
        "env_var": "STM32CUBEMX_PATH",
        "executable_name": "STM32CubeMX",
    },
    "stlink_gdb_server": {
        "env_var": "STLINK_GDB_SERVER",
        "executable_name": "ST-LINK_gdbserver",
    },
    "arm_gdb": {
        "env_var": "ARM_NONE_EABI_GDB",
        "executable_name": "arm-none-eabi-gdb",
    },
    "stm32_signing_tool_cli": {
        "env_var": "STM32_SIGNING_TOOL_CLI",
        "executable_name": "STM32_SigningTool_CLI",
    },
    "stm32cubeclt": {
        "env_var": "STM32CUBECLT",
        "executable_name": "stm32cubeclt",
    },
}


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """Mutable per-context session state.

    Lives for the lifetime of the Python process. Per RES-026
    (2026-05-21), CLI cross-process continuity does not exist —
    each `stm32 debug ...` invocation is one-shot (recipe-flow
    model). Stateful workflows use the Python ``DebugSession``
    context manager.
    """

    active_debug_session: Any = None
    active_vcp_reader: Any = None
    last_build: Any = None
    last_fault: Any = None
    attempt_history: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SubstrateContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubstrateContext:
    """Aggregate configuration / state injected into every tool wrapper."""

    tools: ToolPaths
    defaults: RuntimeDefaults
    project: ProjectDescriptor | None
    logger: logging.Logger
    cwd: Path
    default_probe_sn: str | None = None
    svd_db: Any = None
    session_state: SessionState = field(default_factory=SessionState)

    @classmethod
    def from_environment(
        cls,
        project_path: Path | None = None,
        *,
        tools_config_path: Path | None = None,
        defaults_config_path: Path | None = None,
    ) -> "SubstrateContext":
        """Build a context by discovering and validating config files.

        Resolution per R-002 / R-003 / R-004:

        - ``project`` ← explicit ``project_path`` → ``stm32-project.jsonc``
          in named/current folder → ``None`` if absent.
        - ``defaults`` ← ``stm32-runtime-defaults.jsonc`` at repo root
          (search walks up from ``cwd``) → built-in empty defaults.
        - ``tools`` ← ``stm32-tools.local.jsonc`` (under ``.claude/`` at the
          repo root, then repo root itself) → ``ToolPaths`` with PATH-only
          fallbacks.

        Set ``STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION=1`` to bypass
        jsonschema validation (debug only; WARNING logged).

        Raises ``ConfigurationError`` on schema-invalid configs or on an
        unsupported host (v1 supports Linux and Windows per ADR-007;
        macOS is deferred pending marketplace-demand signal).
        """
        _enforce_supported_platform()

        cwd = (project_path or Path.cwd()).resolve()
        logger = logging.getLogger("embedagents.stm32")

        # Project descriptor (R-002): explicit → cwd → None.
        project = _load_project(project_path, cwd)

        # Runtime defaults (R-004): named override → search → empty.
        defaults = _load_runtime_defaults(defaults_config_path, cwd)

        # Tool paths (R-003): named override → search → PATH-only.
        tools_raw = _load_tools_local(tools_config_path, cwd)
        tools = _resolve_tool_paths(tools_raw)

        default_probe_sn = _resolve_default_probe_sn(tools_raw)

        # Build ctx.svd_db so cross-module consumers (cubeprogrammer
        # D-008, debug DBG-007, every DIAG recipe) can call into a
        # shared lookup. Import lazily to keep contexts that never need
        # the debug surface free of its side imports.
        svd_db = _build_svd_db(tools)

        return cls(
            tools=tools,
            defaults=defaults,
            project=project,
            logger=logger,
            cwd=cwd,
            default_probe_sn=default_probe_sn,
            svd_db=svd_db,
        )


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


def _enforce_supported_platform() -> None:
    """Reject hosts not on the v1 supported list per ADR-007.

    v1 supports Linux and Windows. macOS is deferred pending marketplace
    demand; raises with a hint pointing at the issues URL so users can
    register interest. Any other ``sys.platform`` value (e.g. ``cygwin``,
    ``freebsd``) is rejected.
    """
    if sys.platform.startswith("linux") or sys.platform == "win32":
        return
    if sys.platform == "darwin":
        raise ConfigurationError(
            message="macOS not currently supported; planned based on marketplace demand.",
            hint=(
                "register interest by filing an issue at the project's GitHub "
                "tracker; macOS support lands when demand justifies the "
                "hardware investment per ADR-007."
            ),
        )
    raise ConfigurationError(
        message=f"unsupported platform: {sys.platform}; v1 supports linux and win32",
        hint="run substrate on Linux or Windows per ADR-007.",
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


_SKIP_ENV = "STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION"


def _load_schema(schema_name: str) -> dict[str, Any]:
    """Load a package-bundled schema by basename (e.g. ``stm32-project.schema.json``)."""
    pkg = resources.files("embedagents.stm32.schemas")
    handle = pkg.joinpath(schema_name)
    return load_jsonc(handle.read_text(encoding="utf-8"))


def _validate(instance: Any, schema_name: str, source_path: Path) -> None:
    if os.environ.get(_SKIP_ENV) == "1":
        _log.warning(
            "%s=1 set; skipping jsonschema validation for %s (debug only).",
            _SKIP_ENV,
            source_path,
        )
        return
    schema = _load_schema(schema_name)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    err = errors[0]
    json_path = ".".join(str(p) for p in err.absolute_path) or "<root>"
    expected = _format_expected(err)
    actual = _format_actual(err.instance)
    hint = _hint_for(err)
    raise ConfigurationError(
        message=f"{schema_name} validation failed",
        schema_name=schema_name,
        json_path=json_path,
        expected=expected,
        actual=actual,
        hint=hint,
        tool_output=err.message,
    )


def _format_expected(err: jsonschema.ValidationError) -> str:
    validator = err.validator
    val = err.validator_value
    if validator == "type":
        return f"type {val!r}"
    if validator == "required":
        missing = set(val) - set(err.instance.keys() if isinstance(err.instance, dict) else [])
        return f"required key(s) {sorted(missing)}"
    if validator == "pattern":
        return f"string matching {val!r}"
    if validator == "enum":
        return f"one of {val!r}"
    if validator == "const":
        return f"const {val!r}"
    if validator == "additionalProperties":
        return "no additional properties"
    if validator == "minLength":
        return f"string of length >= {val}"
    if validator == "minimum":
        return f">= {val}"
    if validator == "maximum":
        return f"<= {val}"
    return f"{validator}={val!r}"


def _format_actual(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    return repr(value)


def _hint_for(err: jsonschema.ValidationError) -> str:
    if err.validator == "pattern":
        return f"value must match {err.validator_value!r}"
    if err.validator == "type":
        return f"change the value to a {err.validator_value}"
    if err.validator == "required":
        return f"add the missing key(s) to the object"
    if err.validator == "additionalProperties":
        return "remove the unrecognised key (or check spelling)"
    return "see the schema for the allowed shape"


# ---------------------------------------------------------------------------
# Project descriptor loader (R-002)
# ---------------------------------------------------------------------------


def _load_project(project_path: Path | None, cwd: Path) -> ProjectDescriptor | None:
    candidate = _find_project(project_path, cwd)
    if candidate is None:
        return None
    try:
        raw = load_jsonc_file(candidate)
    except Exception as ex:
        raise ConfigurationError(
            message=f"failed to parse {candidate}",
            schema_name="stm32-project.schema.json",
            tool_output=str(ex),
            hint="fix the JSONC syntax (comments and trailing commas are allowed)",
        ) from ex
    _validate(raw, "stm32-project.schema.json", candidate)
    return ProjectDescriptor(raw, candidate)


def _find_project(project_path: Path | None, cwd: Path) -> Path | None:
    if project_path is not None:
        p = project_path.resolve()
        # IMP-21: an explicit path that doesn't exist is a caller typo,
        # not a "no descriptor" situation — raise rather than silently
        # proceeding descriptor-less against a phantom cwd. A directory
        # WITHOUT a descriptor stays valid (it anchors ctx.cwd; the
        # descriptor itself is optional).
        if not p.exists():
            raise ConfigurationError(
                message=f"project_path does not exist: {project_path}",
                schema_name="stm32-project.schema.json",
                hint=(
                    "fix the path passed to "
                    "SubstrateContext.from_environment(project_path=...)"
                ),
            )
        if p.is_file():
            return p
        cand = p / "stm32-project.jsonc"
        return cand if cand.is_file() else None
    cand = cwd / "stm32-project.jsonc"
    return cand if cand.is_file() else None


# ---------------------------------------------------------------------------
# Runtime defaults loader (R-004)
# ---------------------------------------------------------------------------


def _load_runtime_defaults(
    defaults_config_path: Path | None, cwd: Path
) -> RuntimeDefaults:
    candidate = _find_runtime_defaults(defaults_config_path, cwd)
    if candidate is None:
        # Built-in fallback per R-004: empty dict — every knob falls back
        # to per-tool defaults declared in spec/code.
        return RuntimeDefaults({})
    try:
        raw = load_jsonc_file(candidate)
    except Exception as ex:
        raise ConfigurationError(
            message=f"failed to parse {candidate}",
            schema_name="stm32-runtime-defaults.schema.json",
            tool_output=str(ex),
            hint="fix the JSONC syntax (comments and trailing commas are allowed)",
        ) from ex
    _validate(raw, "stm32-runtime-defaults.schema.json", candidate)
    return RuntimeDefaults(raw)


def _find_runtime_defaults(
    defaults_config_path: Path | None, cwd: Path
) -> Path | None:
    if defaults_config_path is not None:
        p = defaults_config_path.resolve()
        # IMP-21: a typo'd explicit override silently produced built-in
        # defaults with no signal — raise instead.
        if not p.is_file():
            raise ConfigurationError(
                message=f"defaults_config_path does not exist: {defaults_config_path}",
                schema_name="stm32-runtime-defaults.schema.json",
                hint=(
                    "fix the explicit path, or drop the argument to use "
                    "the stm32-runtime-defaults.jsonc search walk"
                ),
            )
        return p
    for parent in (cwd, *cwd.parents):
        cand = parent / "stm32-runtime-defaults.jsonc"
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------------------
# Tools-local loader (R-003) + tool-path resolution
# ---------------------------------------------------------------------------


def _load_tools_local(
    tools_config_path: Path | None, cwd: Path
) -> dict[str, Any]:
    candidate = _find_tools_local(tools_config_path, cwd)
    if candidate is None:
        # Built-in fallback per R-003: no config → resolution falls back to
        # ``shutil.which`` only.
        return {"version": 1, "tools": {}}
    try:
        raw = load_jsonc_file(candidate)
    except Exception as ex:
        raise ConfigurationError(
            message=f"failed to parse {candidate}",
            schema_name="stm32-tools.local.schema.json",
            tool_output=str(ex),
            hint="fix the JSONC syntax (comments and trailing commas are allowed)",
        ) from ex
    _validate(raw, "stm32-tools.local.schema.json", candidate)
    return raw


def _find_tools_local(
    tools_config_path: Path | None, cwd: Path
) -> Path | None:
    if tools_config_path is not None:
        p = tools_config_path.resolve()
        # IMP-21: same loud-error contract as the defaults override.
        if not p.is_file():
            raise ConfigurationError(
                message=f"tools_config_path does not exist: {tools_config_path}",
                schema_name="stm32-tools.local.schema.json",
                hint=(
                    "fix the explicit path, or drop the argument to use "
                    "the .claude/stm32-tools.local.jsonc search walk"
                ),
            )
        return p
    for parent in (cwd, *cwd.parents):
        for rel in (".claude/stm32-tools.local.jsonc", "stm32-tools.local.jsonc"):
            cand = parent / rel
            if cand.is_file():
                return cand
    return None


def _resolve_tool_paths(raw: dict[str, Any]) -> ToolPaths:
    """Walk the ``tools`` block and resolve each entry per R-003.

    For each known tool, the configured entry (if any) is merged on top of
    the built-in defaults (``env_var`` + ``executable_name``). This lets
    callers set ``STM32_PROGRAMMER_CLI`` or rely on PATH lookup without
    needing a ``stm32-tools.local.jsonc`` file at all — matching the
    "env var override" half of the per-tool loud-error format.
    """
    resolved: dict[str, Path | None] = {}
    tools_block = raw.get("tools", {})
    for schema_key, attr_name in _SCHEMA_KEY_TO_ATTR.items():
        configured = tools_block.get(schema_key, {})
        tool_def = {**_BUILTIN_TOOL_DEFAULTS.get(schema_key, {}), **configured}
        resolved[attr_name] = _resolve_one_tool(tool_def)
    return ToolPaths(**resolved)


def _resolve_one_tool(tool_def: dict[str, Any]) -> Path | None:
    """Apply the R-003 chain: env var → configured candidates → PATH lookup → None.

    Returns ``None`` when no candidate exists. The caller (tool wrapper)
    raises ``ConfigurationError`` lazily on first use, with a hint pointing
    at the JSON key to set.
    """
    env_var = tool_def.get("env_var")
    if env_var:
        env_value = os.environ.get(env_var)
        if env_value:
            p = Path(env_value)
            if p.exists():
                return p
            # IMP-20: a SET env var is an explicit pin. Falling through
            # to candidates/PATH would silently run a different binary
            # than the one the user pinned — raise loud instead.
            raise ConfigurationError(
                message=(
                    f"env var {env_var} is set but points at a "
                    f"nonexistent path: {env_value!r}"
                ),
                hint=(
                    f"fix or unset {env_var}; substrate refuses to fall "
                    "back to PATH when an explicit pin is broken"
                ),
            )

    # Configured candidates — per-OS lookup keyed by sys.platform per ADR-007.
    # Schema keys are "linux" / "windows" / "darwin"; macOS is gated by
    # _enforce_supported_platform() so darwin candidates are never reached
    # in v1, but the lookup honours them for forward-compat.
    candidates_block = tool_def.get("candidates", {})
    if sys.platform == "win32":
        candidates = candidates_block.get("windows", [])
    elif sys.platform == "darwin":
        candidates = candidates_block.get("darwin", [])
    else:
        candidates = candidates_block.get("linux", [])
    for c in candidates:
        p = Path(c).expanduser()
        if p.exists():
            return p

    # PATH lookup via shutil.which
    exe_name = tool_def.get("executable_name")
    if exe_name:
        found = shutil.which(exe_name)
        if found:
            return Path(found)

    return None


def _resolve_default_probe_sn(raw: dict[str, Any]) -> str | None:
    """Read ``programmer.default_probe_sn`` or the env override."""
    env_value = os.environ.get("STM32_PROGRAMMER_DEFAULT_SN")
    if env_value:
        return env_value
    programmer = raw.get("programmer", {})
    return programmer.get("default_probe_sn")


def _build_svd_db(tools: ToolPaths) -> Any:
    """Build the substrate-wide ``ctx.svd_db`` from resolved tool paths.

    Lazy import keeps the debug subpackage off the top-level import
    graph; contexts that never look at SVDs don't pay the cost.

    Always returns an ``SvdDb`` instance — degrades gracefully when none
    of the three source roots resolve (consumers see a ``find_for() →
    None`` and raise ``SVDLookupError`` themselves).
    """
    # Defer import: keeps test-only contexts (e.g. errors / platform
    # tests) free of debug-module side effects.
    from embedagents.stm32.debug.svd import SvdDb, resolve_svd_roots

    # Construct an ad-hoc namespace exposing only ``tools`` so
    # resolve_svd_roots stays decoupled from the full SubstrateContext.
    class _MinimalCtx:
        pass

    minimal = _MinimalCtx()
    minimal.tools = tools  # type: ignore[attr-defined]
    roots = resolve_svd_roots(minimal)  # type: ignore[arg-type]
    return SvdDb(roots=roots)
