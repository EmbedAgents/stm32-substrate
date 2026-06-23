"""C1a skeleton tests — cubeide package imports, result dataclasses are
frozen, both public methods raise ``NotImplementedError`` until their
sub-phase bodies land."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

import pytest

from embedagents.stm32.context import SubstrateContext
from embedagents.stm32.cubeide import (
    AmbiguousCallback,
    BuildResult,
    ConflictCallback,
    CubeIDE,
    ExistingCallback,
    FoundProject,
    HeadlessInvocation,
    SettingChange,
    SettingsModification,
)
from embedagents.stm32.errors import (
    ConfigurationError,
    CProjectEditError,
    ProjectAmbiguityError,
    ProtocolError,
    WorkspaceLockedError,
)


_RESULT_TYPES = [
    BuildResult,
    FoundProject,
    HeadlessInvocation,
    SettingChange,
    SettingsModification,
]


class TestResultDataclasses:
    @pytest.mark.parametrize("cls", _RESULT_TYPES)
    def test_is_dataclass(self, cls: type) -> None:
        assert is_dataclass(cls)

    def test_build_result_frozen(self, tmp_path: Path) -> None:
        result = BuildResult(
            success=True,
            exit_code=0,
            duration_s=1.5,
            log_path=tmp_path / "build.log",
            console_output="ok",
            artifact_path=None,
            map_path=None,
            project_name="demo",
            configuration="Debug",
            workspace_path=tmp_path,
        )
        with pytest.raises(FrozenInstanceError):
            result.success = False  # type: ignore[misc]

    def test_found_project_defaults(self, tmp_path: Path) -> None:
        fp = FoundProject(
            path=tmp_path,
            name="demo",
            cproject_path=tmp_path / ".cproject",
        )
        assert fp.candidates_considered == ()

    def test_settings_modification_optional_fields(self) -> None:
        sm = SettingsModification(
            file=Path("/tmp/.cproject"),
            backup_path=None,
            changes=[],
        )
        assert sm.rolled_back is False

    def test_headless_invocation_defaults(self, tmp_path: Path) -> None:
        inv = HeadlessInvocation(
            project_name="demo",
            configuration="Debug",
            workspace=tmp_path,
        )
        assert inv.project_path is None
        assert inv.clean is False
        assert inv.extra_args == ()


class TestErrorHierarchy:
    def test_project_ambiguity_carries_candidates(self) -> None:
        err = ProjectAmbiguityError(
            message="multiple .cproject found",
            candidates=(Path("/a/.cproject"), Path("/b/.cproject")),
        )
        assert err.candidates == (Path("/a/.cproject"), Path("/b/.cproject"))
        # Live raisers (cubeide/client.py) differentiate this by class
        # identity and set no cubeide_marker -> defaults to None (RES-056).
        assert err.cubeide_marker is None

    def test_project_ambiguity_default_empty(self) -> None:
        err = ProjectAmbiguityError(message="boom")
        assert err.candidates == ()

    def test_cproject_edit_extends_protocol(self) -> None:
        err = CProjectEditError(
            message="parse failed",
            failed_step="parse",
            file=Path("/tmp/.cproject"),
            backup_path=Path("/tmp/.cproject.bak"),
            superclass_attempted="some.option.id",
        )
        assert isinstance(err, ProtocolError)
        assert err.failed_step == "parse"
        assert err.file == Path("/tmp/.cproject")

    def test_workspace_locked_inherits_context_fields(self, tmp_path: Path) -> None:
        err = WorkspaceLockedError(
            message="held",
            workspace_path=tmp_path,
            project_name="demo",
            configuration="Debug",
        )
        assert err.workspace_path == tmp_path
        assert err.project_name == "demo"


class TestCallableAliases:
    def test_aliases_exist(self) -> None:
        # These are runtime-checked via callable assignment.
        cb: ConflictCallback = lambda _f, _a, _b: "replace"
        ex: ExistingCallback = lambda _p: "skip"
        am: AmbiguousCallback = lambda paths: paths[0]
        assert cb("x", "y", "z") == "replace"
        assert ex(Path("/x")) == "skip"
        assert am([Path("/a"), Path("/b")]) == Path("/a")


@pytest.fixture()
def ctx(tmp_path: Path) -> SubstrateContext:
    return SubstrateContext.from_environment(project_path=tmp_path)


class TestClientSkeleton:
    def test_construct(self, ctx: SubstrateContext) -> None:
        client = CubeIDE(ctx)
        assert client.ctx is ctx
        assert client._log.name == "embedagents.stm32.cubeide"

    def test_unresolved_cubeide_raises_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STM32CUBEIDE", raising=False)
        monkeypatch.setenv("PATH", "")
        isolated = SubstrateContext.from_environment(project_path=tmp_path)
        client = CubeIDE(isolated)
        with pytest.raises(ConfigurationError, match="STM32CubeIDE"):
            client._require_cubeide()

    # build() implemented in C1d-f → tests in test_cubeide_build.py.
    # find_project() implemented in C1d-f → tests in test_cubeide_find_project.py.


class TestPublicMethodCount:
    def test_only_two_public_methods(self) -> None:
        methods = [
            name
            for name, member in inspect.getmembers(CubeIDE, inspect.isfunction)
            if not name.startswith("_")
        ]
        # Per spec: "Two public methods only".
        assert sorted(methods) == ["build", "find_project"]
