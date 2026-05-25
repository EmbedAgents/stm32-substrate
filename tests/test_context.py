"""Unit tests for ``stm32_substrate.context``."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

from stm32_substrate.context import (
    ProjectDescriptor,
    RuntimeDefaults,
    SessionState,
    SubstrateContext,
    ToolPaths,
)
from stm32_substrate.errors import ConfigurationError


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
        from stm32_substrate import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "darwin")
        with pytest.raises(ConfigurationError) as excinfo:
            _ctx._enforce_supported_platform()
        assert "macOS not currently supported" in excinfo.value.message
        assert excinfo.value.hint is not None
        assert "GitHub" in excinfo.value.hint or "issue" in excinfo.value.hint.lower()

    def test_unknown_platform_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stm32_substrate import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "freebsd")
        with pytest.raises(ConfigurationError) as excinfo:
            _ctx._enforce_supported_platform()
        assert "unsupported platform" in excinfo.value.message
        assert "freebsd" in excinfo.value.message

    def test_linux_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stm32_substrate import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "linux")
        _ctx._enforce_supported_platform()  # must not raise

    def test_linux_variant_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``sys.platform.startswith("linux")`` covers 'linux2' etc."""
        from stm32_substrate import context as _ctx

        monkeypatch.setattr(_ctx.sys, "platform", "linux2")
        _ctx._enforce_supported_platform()  # must not raise

    def test_win32_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stm32_substrate import context as _ctx

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

        with caplog.at_level(logging.WARNING, logger="stm32_substrate.context"):
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
        assert ctx.logger.name == "stm32_substrate"

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
