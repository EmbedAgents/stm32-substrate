"""Unit tests for the substrate error hierarchy."""

from __future__ import annotations

from pathlib import Path

import pytest

from embedagents.stm32.errors import (
    ConfigurationError,
    CubeIDEError,
    CubeMXError,
    CubeProgrammerError,
    GDBError,
    HardwareError,
    ProtocolError,
    ResolutionError,
    SigningToolError,
    SubstrateError,
    SVDLookupError,
    ToolError,
    UserAbortedError,
    VCPAmbiguousProbe,
    VCPError,
    WorkspaceLockedError,
)


class TestBaseHierarchy:
    """Every substrate-raised type ultimately inherits from SubstrateError."""

    @pytest.mark.parametrize(
        "cls",
        [
            ConfigurationError,
            ResolutionError,
            ToolError,
            CubeProgrammerError,
            CubeIDEError,
            CubeMXError,
            GDBError,
            VCPError,
            SigningToolError,
            ProtocolError,
            HardwareError,
            UserAbortedError,
            WorkspaceLockedError,
            VCPAmbiguousProbe,
            SVDLookupError,
        ],
    )
    def test_subclass_of_substrate_error(self, cls: type[SubstrateError]) -> None:
        assert issubclass(cls, SubstrateError)
        assert issubclass(cls, Exception)

    def test_tool_subclasses_inherit_from_tool_error(self) -> None:
        for cls in (
            CubeProgrammerError,
            CubeIDEError,
            CubeMXError,
            GDBError,
            VCPError,
            SigningToolError,
        ):
            assert issubclass(cls, ToolError)

    def test_workspace_locked_is_cubeide_error(self) -> None:
        assert issubclass(WorkspaceLockedError, CubeIDEError)

    def test_vcp_ambiguous_is_vcp_error(self) -> None:
        assert issubclass(VCPAmbiguousProbe, VCPError)

    def test_svd_lookup_is_gdb_error(self) -> None:
        assert issubclass(SVDLookupError, GDBError)


class TestBaseFields:
    def test_minimal_construction(self) -> None:
        err = SubstrateError(message="boom")
        assert err.message == "boom"
        assert err.code is None
        assert err.tool_output is None
        assert err.hint is None
        assert err.recoverable is False
        assert str(err) == "boom"

    def test_full_construction(self) -> None:
        err = SubstrateError(
            message="connect failed",
            code=1,
            tool_output="stderr blob",
            hint="run diagnose_micro()",
            recoverable=True,
        )
        assert err.code == 1
        assert err.tool_output == "stderr blob"
        assert err.hint == "run diagnose_micro()"
        assert err.recoverable is True

    def test_raises_as_exception(self) -> None:
        with pytest.raises(SubstrateError, match="boom"):
            raise SubstrateError(message="boom")


class TestPerToolMarkers:
    def test_cubeide_marker(self) -> None:
        err = CubeIDEError(message="import refused", cubeide_marker="import-failed")
        assert err.cubeide_marker == "import-failed"

    def test_cubemx_marker(self) -> None:
        err = CubeMXError(message="ioc missing", cubemx_marker="ioc-missing")
        assert err.cubemx_marker == "ioc-missing"

    def test_gdb_marker(self) -> None:
        err = GDBError(message="port in use", gdb_marker="port-busy")
        assert err.gdb_marker == "port-busy"

    def test_vcp_marker(self) -> None:
        err = VCPError(message="no port", vcp_marker="no-vcp-enumerated")
        assert err.vcp_marker == "no-vcp-enumerated"

    def test_signing_marker(self) -> None:
        err = SigningToolError(message="align required", signing_marker="align-required")
        assert err.signing_marker == "align-required"

    def test_cubeprogrammer_error_code_is_int(self) -> None:
        err = CubeProgrammerError(
            message="target connect err",
            error_code=1,
            target_device="STM32L476RG",
            swd_freq_khz=4000,
            mode_attempted="hwRstPulse",
        )
        assert err.error_code == 1
        assert err.target_device == "STM32L476RG"
        assert err.swd_freq_khz == 4000
        assert err.mode_attempted == "hwRstPulse"


class TestConfigurationError:
    def test_loud_error_fields(self) -> None:
        err = ConfigurationError(
            message="stm32-project.jsonc validation failed",
            schema_name="stm32-project.schema.json",
            json_path="firmware.flash_address",
            expected="string matching ^0x[0-9A-Fa-f]+$",
            actual='"0x080,000,000"',
            hint="remove the commas",
        )
        assert err.schema_name == "stm32-project.schema.json"
        assert err.json_path == "firmware.flash_address"
        assert err.expected.startswith("string matching")
        assert err.actual == '"0x080,000,000"'
        assert err.hint == "remove the commas"


class TestWorkspaceLocked:
    def test_carries_workspace_path(self, tmp_path: Path) -> None:
        err = WorkspaceLockedError(
            message="workspace held by GUI",
            cubeide_marker="workspace-locked",
            workspace_path=tmp_path,
            hint="close STM32CubeIDE GUI on this workspace",
        )
        assert err.workspace_path == tmp_path
        assert err.cubeide_marker == "workspace-locked"


class TestVCPAmbiguousProbe:
    def test_candidates_default_empty(self) -> None:
        err = VCPAmbiguousProbe(message="multiple probes", vcp_marker="ambiguous-probe")
        assert err.candidates == ()

    def test_candidates_preserved(self) -> None:
        candidate = ("/dev/ttyACM0", "066BFF...", "NUCLEO-L476RG")
        err = VCPAmbiguousProbe(
            message="multiple probes",
            vcp_marker="ambiguous-probe",
            candidates=(candidate,),
        )
        assert err.candidates == (candidate,)


class TestSVDLookupError:
    def test_attempted_paths(self, tmp_path: Path) -> None:
        p1 = tmp_path / "cubeide"
        p2 = tmp_path / "cubeprogrammer"
        p3 = tmp_path / "clt"
        err = SVDLookupError(
            message="SVD not found",
            gdb_marker="svd-not-found",
            attempted_paths=(p1, p2, p3),
        )
        assert err.attempted_paths == (p1, p2, p3)


class TestUserAborted:
    def test_repr_marks_user_choice(self) -> None:
        err = UserAbortedError(message="user declined OB write")
        assert "user declined" in str(err)
