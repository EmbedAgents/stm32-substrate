"""Unit tests for ``embedagents.stm32.context``."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from embedagents.stm32.context import (
    ProjectDescriptor,
    RuntimeDefaults,
    SessionState,
    SubstrateContext,
    ToolPaths,
)
from embedagents.stm32.cubeprogrammer import CubeProgrammer
from embedagents.stm32.errors import ConfigurationError


# ---------------------------------------------------------------------------
# Minimal valid configs for tests to use
# ---------------------------------------------------------------------------

_TOOLS_LOCAL_MIN = {
    "version": 1,
    "tools": {},
}

_TOOLS_LOCAL_WITH_PROBE = {
    "version": 1,
    "programmer": {"default_probe_sn": "066BFF514852"},
    "tools": {},
}

_PROJECT_MIN: dict = {
    "version": 1,
    "project_name": "demo",
}


def _write_jsonc(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Shipped tools-config example
# ---------------------------------------------------------------------------


class TestToolsLocalExample:
    """``.claude/stm32-tools.local.jsonc.example`` is the template the onboarding
    prompt (README) tells Claude to copy, so it must always be schema-valid and
    cover every tool — otherwise a copied config is born broken (release-test
    follow-up: a session that invented its own schema produced an invalid file)."""

    EXAMPLE = (
        Path(__file__).resolve().parent.parent
        / ".claude"
        / "stm32-tools.local.jsonc.example"
    )

    def test_example_exists(self) -> None:
        assert self.EXAMPLE.is_file(), f"missing {self.EXAMPLE}"

    def test_example_validates_against_bundled_schema(self) -> None:
        import jsonschema
        from importlib.resources import files

        from embedagents.stm32._jsonc import load_jsonc_file

        data = load_jsonc_file(self.EXAMPLE)
        schema = json.loads(
            (
                files("embedagents.stm32.schemas")
                / "stm32-tools.local.schema.json"
            ).read_text(encoding="utf-8")
        )
        # Raises on any unrecognized key (additionalProperties: false) or shape
        # mismatch — the exact failure mode the rogue invented config hit.
        jsonschema.validate(data, schema)

    def test_example_covers_every_tool(self) -> None:
        from embedagents.stm32._jsonc import load_jsonc_file

        data = load_jsonc_file(self.EXAMPLE)
        expected = {
            "cube_programmer",
            "cubeide",
            "cubemx",
            "stlink_gdb_server",
            "arm_gdb",
            "stm32_signing_tool_cli",
            "stm32cubeclt",
        }
        assert expected <= set(data.get("tools", {}))


# ---------------------------------------------------------------------------
# DictBacked wrappers
# ---------------------------------------------------------------------------


class TestDictBacked:
    def test_runtime_defaults_dotted_access(self) -> None:
        rd = RuntimeDefaults({"programmer": {"connect_timeout_s": 30}})
        assert rd.programmer.connect_timeout_s == 30
        assert rd._raw == {"programmer": {"connect_timeout_s": 30}}

    def test_runtime_defaults_is_read_only(self) -> None:
        rd = RuntimeDefaults({"a": 1})
        with pytest.raises(AttributeError, match="read-only"):
            rd.a = 2  # type: ignore[misc]

    def test_runtime_defaults_empty(self) -> None:
        rd = RuntimeDefaults({})
        assert rd._raw == {}

    def test_list_values_preserved(self) -> None:
        rd = RuntimeDefaults({"paths": ["/a", "/b"]})
        assert rd.paths == ["/a", "/b"]

    def test_project_descriptor_tracks_source_path(self, tmp_path: Path) -> None:
        src = tmp_path / "stm32-project.jsonc"
        pd = ProjectDescriptor({"v": 1}, src)
        assert pd.source_path == src
        assert pd.v == 1


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class TestSessionState:
    def test_defaults(self) -> None:
        s = SessionState()
        assert s.active_debug_session is None
        assert s.active_vcp_reader is None
        assert s.last_build is None
        assert s.last_fault is None
        assert s.attempt_history == []

    def test_mutable(self) -> None:
        s = SessionState()
        s.active_debug_session = "sentinel"
        s.attempt_history.append({"attempt": 1})
        assert s.active_debug_session == "sentinel"
        assert len(s.attempt_history) == 1


# ---------------------------------------------------------------------------
# Platform gate (ADR-007: Linux + Windows v1; macOS deferred)
# ---------------------------------------------------------------------------


class TestPlatformGate:
    """``_enforce_supported_platform`` rejects darwin + unknown platforms.

    Linux + Windows acceptance is exercised implicitly by the rest of the
    suite — those tests cannot run on a rejected platform. We can't easily
    monkeypatch ``sys.platform`` for the accept paths (the underlying
    platform-wrappers import at module load), so we test the reject paths
    only.
    """

    def test_darwin_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from embedagents.stm32 import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "darwin")
        with pytest.raises(ConfigurationError) as excinfo:
            _ctx._enforce_supported_platform()
        assert "macOS not currently supported" in excinfo.value.message
        assert excinfo.value.hint is not None
        assert "GitHub" in excinfo.value.hint or "issue" in excinfo.value.hint.lower()

    def test_unknown_platform_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from embedagents.stm32 import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "freebsd")
        with pytest.raises(ConfigurationError) as excinfo:
            _ctx._enforce_supported_platform()
        assert "unsupported platform" in excinfo.value.message
        assert "freebsd" in excinfo.value.message

    def test_linux_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from embedagents.stm32 import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "linux")
        _ctx._enforce_supported_platform()  # must not raise

    def test_linux_variant_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sys.platform.startswith("linux")`` covers 'linux2' etc."""
        from embedagents.stm32 import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "linux2")
        _ctx._enforce_supported_platform()  # must not raise

    def test_win32_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from embedagents.stm32 import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "win32")
        _ctx._enforce_supported_platform()  # must not raise


# ---------------------------------------------------------------------------
# Tool path resolution
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only")
class TestToolResolution:
    def test_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = tmp_path / "STM32_Programmer_CLI"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)

        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(cli))
        tools = {
            "version": 1,
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": "STM32_Programmer_CLI",
                    "candidates": {"linux": ["/nonexistent/path"]},
                }
            },
        }
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, tools)

        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.tools.cube_programmer_cli == cli

    def test_candidates_used_when_env_var_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        cli = tmp_path / "bin" / "STM32_Programmer_CLI"
        cli.parent.mkdir()
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)

        tools = {
            "version": 1,
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": "STM32_Programmer_CLI",
                    "candidates": {"linux": [str(cli)]},
                }
            },
        }
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, tools)

        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.tools.cube_programmer_cli == cli

    def test_unresolved_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        # Empty PATH so shutil.which finds nothing.
        monkeypatch.setenv("PATH", "")
        tools = {
            "version": 1,
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": "STM32_Programmer_CLI_definitely_absent",
                    "candidates": {"linux": ["/nonexistent/path"]},
                }
            },
        }
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, tools)

        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.tools.cube_programmer_cli is None

    def test_unresolved_tool_raises_loud_on_first_use(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution returns None *silently* (test above); the loud contract
        is deferred to first use. A newcomer with a misconfigured path must
        then get a ConfigurationError naming the JSON key AND the env var to
        set -- not a downstream crash they can't diagnose."""
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        monkeypatch.setenv("PATH", "")
        tools = {
            "version": 1,
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": "STM32_Programmer_CLI_definitely_absent",
                    "candidates": {"linux": ["/nonexistent/path"]},
                }
            },
        }
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, tools)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)

        prog = CubeProgrammer(ctx)
        with pytest.raises(ConfigurationError) as excinfo:
            prog.connect()
        hint = excinfo.value.hint or ""
        assert "cube_programmer_path" in hint, hint
        assert "STM32_PROGRAMMER_CLI" in hint, hint

    def test_candidates_beat_path_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution order R-003: a configured candidate that exists wins
        over a same-named binary discovered on PATH."""
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        exe_name = "STM32_Programmer_CLI_t5guard"
        # A binary on PATH (the decoy) ...
        path_dir = tmp_path / "pathbin"
        path_dir.mkdir()
        on_path = path_dir / exe_name
        on_path.write_text("#!/bin/sh\n")
        on_path.chmod(0o755)
        monkeypatch.setenv("PATH", str(path_dir))
        # ... and a different configured candidate that also exists (must win).
        candidate = tmp_path / "configured" / exe_name
        candidate.parent.mkdir()
        candidate.write_text("#!/bin/sh\n")
        candidate.chmod(0o755)

        tools = {
            "version": 1,
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": exe_name,
                    "candidates": {"linux": [str(candidate)]},
                }
            },
        }
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, tools)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.tools.cube_programmer_cli == candidate

    def test_path_lookup_used_when_candidates_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R-003 fallback tier: env unset + candidates absent -> shutil.which."""
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        exe_name = "STM32_Programmer_CLI_t5fallback"
        path_dir = tmp_path / "pathbin"
        path_dir.mkdir()
        on_path = path_dir / exe_name
        on_path.write_text("#!/bin/sh\n")
        on_path.chmod(0o755)
        monkeypatch.setenv("PATH", str(path_dir))

        tools = {
            "version": 1,
            "tools": {
                "cube_programmer": {
                    "env_var": "STM32_PROGRAMMER_CLI",
                    "executable_name": exe_name,
                    "candidates": {"linux": ["/nonexistent/path"]},
                }
            },
        }
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, tools)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.tools.cube_programmer_cli == on_path

    def test_malformed_tools_jsonc_raises_configuration_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A syntactically broken tools config surfaces a clean, typed
        ConfigurationError (not a raw JSONDecodeError traceback to the user)."""
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        cfg.write_text('{ "version": 1, "tools": { broken ] ', encoding="utf-8")
        with pytest.raises(ConfigurationError, match="failed to parse"):
            SubstrateContext.from_environment(project_path=tmp_path)


# ---------------------------------------------------------------------------
# default_probe_sn resolution
# ---------------------------------------------------------------------------


class TestDefaultProbeSn:
    def test_from_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STM32_PROGRAMMER_DEFAULT_SN", raising=False)
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, _TOOLS_LOCAL_WITH_PROBE)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.default_probe_sn == "066BFF514852"

    def test_env_overrides_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STM32_PROGRAMMER_DEFAULT_SN", "ENVOVERRIDE")
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, _TOOLS_LOCAL_WITH_PROBE)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.default_probe_sn == "ENVOVERRIDE"

    def test_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STM32_PROGRAMMER_DEFAULT_SN", raising=False)
        cfg = tmp_path / ".claude" / "stm32-tools.local.jsonc"
        cfg.parent.mkdir()
        _write_jsonc(cfg, _TOOLS_LOCAL_MIN)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.default_probe_sn is None


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_invalid_project_raises_with_loud_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        # version=2 violates the const=1 rule on the project schema.
        bad = {"version": 2, "project_name": "demo"}
        proj = tmp_path / "stm32-project.jsonc"
        _write_jsonc(proj, bad)

        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)

        err = excinfo.value
        assert err.schema_name == "stm32-project.schema.json"
        assert err.json_path == "version"
        assert "const" in (err.expected or "")
        assert err.actual is not None

    def test_invalid_flash_address_pattern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        bad = {
            "version": 1,
            "project_name": "demo",
            "firmware": {"flash_address": "0x080,000,000"},  # commas not allowed
        }
        proj = tmp_path / "stm32-project.jsonc"
        _write_jsonc(proj, bad)

        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)

        err = excinfo.value
        assert err.json_path == "firmware.flash_address"
        assert "pattern" in (err.expected or "").lower() or "matching" in (err.expected or "")

    def test_unknown_top_level_key_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        bad = {"version": 1, "project_name": "demo", "unexpected_key": 1}
        proj = tmp_path / "stm32-project.jsonc"
        _write_jsonc(proj, bad)
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        assert excinfo.value.schema_name == "stm32-project.schema.json"

    def test_skip_env_bypasses_validation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", "1")
        # version=2 would normally fail validation.
        bad = {"version": 2, "project_name": "demo"}
        proj = tmp_path / "stm32-project.jsonc"
        _write_jsonc(proj, bad)

        with caplog.at_level(logging.WARNING, logger="embedagents.stm32.context"):
            ctx = SubstrateContext.from_environment(project_path=tmp_path)

        assert ctx.project is not None
        assert any("skipping jsonschema validation" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------


class TestProjectDiscovery:
    def test_no_project_descriptor(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.project is None
        assert ctx.cwd == tmp_path

    def test_project_in_named_folder(self, tmp_path: Path) -> None:
        proj_dir = tmp_path / "myapp"
        proj_dir.mkdir()
        _write_jsonc(proj_dir / "stm32-project.jsonc", _PROJECT_MIN)
        ctx = SubstrateContext.from_environment(project_path=proj_dir)
        assert ctx.project is not None
        assert ctx.project.project_name == "demo"

    def test_project_explicit_file_path(self, tmp_path: Path) -> None:
        proj = tmp_path / "stm32-project.jsonc"
        _write_jsonc(proj, _PROJECT_MIN)
        ctx = SubstrateContext.from_environment(project_path=proj)
        assert ctx.project is not None


# ---------------------------------------------------------------------------
# Defaults discovery
# ---------------------------------------------------------------------------


class TestRuntimeDefaultsDiscovery:
    def test_no_defaults_returns_empty(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.defaults._raw == {}

    def test_defaults_loaded_from_repo_root(self, tmp_path: Path) -> None:
        # tmp_path is the "repo root" for this test.
        defaults = {"version": 1, "programmer": {"connect_timeout_s": 45}}
        _write_jsonc(tmp_path / "stm32-runtime-defaults.jsonc", defaults)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.defaults.programmer.connect_timeout_s == 45

    def test_t3_max_iterations_knob(self, tmp_path: Path) -> None:
        # RES-031: t3.max_iterations bounds the Claude-in-loop fix workflows.
        # Schema-validated + reachable via dotted access (dict-backed).
        defaults = {"version": 1, "t3": {"max_iterations": 3}}
        _write_jsonc(tmp_path / "stm32-runtime-defaults.jsonc", defaults)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.defaults.t3.max_iterations == 3


# ---------------------------------------------------------------------------
# Type identity
# ---------------------------------------------------------------------------


class TestContextShape:
    def test_logger_attached(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert isinstance(ctx.logger, logging.Logger)
        assert ctx.logger.name == "embedagents.stm32"

    def test_session_state_present(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert isinstance(ctx.session_state, SessionState)

    def test_tools_is_toolpaths(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert isinstance(ctx.tools, ToolPaths)

    def test_context_is_frozen(self, tmp_path: Path) -> None:
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        with pytest.raises(Exception):
            ctx.cwd = Path("/elsewhere")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# IMP-20 — set-but-broken tool-path env vars raise loud
# ---------------------------------------------------------------------------


class TestBrokenEnvVarPin:
    def test_env_var_pointing_at_nonexistent_path_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STM32_PROGRAMMER_CLI", str(tmp_path / "typo"))
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        assert "STM32_PROGRAMMER_CLI" in excinfo.value.message
        assert "typo" in excinfo.value.message

    def test_unset_env_var_still_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_PROGRAMMER_CLI", raising=False)
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        # No raise; resolution proceeded to candidates/PATH (may be None).
        assert isinstance(ctx, SubstrateContext)


# ---------------------------------------------------------------------------
# IMP-21 — explicit config-path params must exist
# ---------------------------------------------------------------------------


class TestExplicitConfigPathTypos:
    def test_missing_project_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(
                project_path=tmp_path / "no-such-dir"
            )
        assert "project_path" in excinfo.value.message

    def test_missing_defaults_config_path_raises(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(
                project_path=tmp_path,
                defaults_config_path=tmp_path / "typo-defaults.jsonc",
            )
        assert "defaults_config_path" in excinfo.value.message

    def test_missing_tools_config_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(
                project_path=tmp_path,
                tools_config_path=tmp_path / "typo-tools.jsonc",
            )
        assert "tools_config_path" in excinfo.value.message

    def test_existing_dir_without_descriptor_stays_valid(
        self, tmp_path: Path
    ) -> None:
        # A real directory with no stm32-project.jsonc anchors ctx.cwd;
        # the descriptor itself is optional.
        ctx = SubstrateContext.from_environment(project_path=tmp_path)
        assert ctx.project is None
        assert ctx.cwd == tmp_path.resolve()


# ---------------------------------------------------------------------------
# A-019 — invalid-sample rejection for the OTHER two schemas (M-016)
# ---------------------------------------------------------------------------


class TestRuntimeDefaultsSchemaValidation:
    def test_wrong_type_knob_raises_with_loud_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        bad = {
            "version": 1,
            "debug": {"read_timeout_s": "ten"},  # integer required
        }
        _write_jsonc(tmp_path / "stm32-runtime-defaults.jsonc", bad)
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        err = excinfo.value
        assert err.schema_name == "stm32-runtime-defaults.schema.json"
        assert err.json_path == "debug.read_timeout_s"

    def test_unknown_knob_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        bad = {"version": 1, "debug": {"no_such_knob": 5}}
        _write_jsonc(tmp_path / "stm32-runtime-defaults.jsonc", bad)
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        assert excinfo.value.schema_name == "stm32-runtime-defaults.schema.json"

    def test_out_of_range_port_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        bad = {"version": 1, "debug": {"gdb_port": 80}}  # minimum 1024
        _write_jsonc(tmp_path / "stm32-runtime-defaults.jsonc", bad)
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        assert excinfo.value.json_path == "debug.gdb_port"


class TestToolsLocalSchemaValidation:
    def test_wrong_version_raises_with_loud_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        bad = {"version": 2, "tools": {}}  # const=1
        _write_jsonc(claude_dir / "stm32-tools.local.jsonc", bad)
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        err = excinfo.value
        assert err.schema_name == "stm32-tools.local.schema.json"
        assert err.json_path == "version"

    def test_wrong_candidates_shape_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION", raising=False)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        bad = {
            "version": 1,
            "tools": {
                "cube_programmer": {"candidates": "/usr/bin/cli"}
            },  # object with per-OS lists required
        }
        _write_jsonc(claude_dir / "stm32-tools.local.jsonc", bad)
        with pytest.raises(ConfigurationError) as excinfo:
            SubstrateContext.from_environment(project_path=tmp_path)
        assert excinfo.value.schema_name == "stm32-tools.local.schema.json"
