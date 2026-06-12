"""C1c tests — CProjectEditor atomic-edit protocol + presets/FPU."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from embedagents.stm32.cubeide.cproject import CProjectEditor
from embedagents.stm32.cubeide.presets import (
    FAMILY_FPU_TABLE,
    PRESET_BALANCED,
    PRESET_FAST,
    PRESET_SIZE,
    PRESETS,
    fpu_flags_for_family,
)
from embedagents.stm32.cubeide.results import SettingChange, SettingsModification
from embedagents.stm32.errors import CProjectEditError


# ---------------------------------------------------------------------------
# Minimal synthetic .cproject XML for tests
# ---------------------------------------------------------------------------


def _make_cproject(
    project_dir: Path,
    *,
    configurations: list[str] = ["Debug", "Release"],
    options: list[tuple[str, str, str]] | None = None,
    list_options: list[tuple[str, str, list[str]]] | None = None,
) -> Path:
    """Create a minimal-but-realistic .cproject file under project_dir.

    Args:
        configurations: names for the cconfiguration blocks.
        options: ``[(config, superClass, value), ...]`` — flat options
            attached to each named config.
        list_options: ``[(config, superClass, [value, ...]), ...]`` —
            list-shaped options with pre-existing listOptionValue entries.
    """
    cproject_root = ET.Element("cproject")
    sm = ET.SubElement(cproject_root, "storageModule", moduleId="cdtBuildSystem")

    for config_name in configurations:
        cconfig = ET.SubElement(
            sm, "cconfiguration", id=f"config.{config_name.lower()}.id"
        )
        config_el = ET.SubElement(cconfig, "configuration", name=config_name)
        toolchain = ET.SubElement(config_el, "toolChain")
        tool = ET.SubElement(toolchain, "tool")
        for c, sc, val in options or []:
            if c == config_name:
                ET.SubElement(tool, "option", superClass=sc, value=val)
        for c, sc, values in list_options or []:
            if c == config_name:
                opt = ET.SubElement(tool, "option", superClass=sc)
                for v in values:
                    ET.SubElement(opt, "listOptionValue", value=v, builtIn="false")

    path = project_dir / ".cproject"
    ET.ElementTree(cproject_root).write(
        path, encoding="UTF-8", xml_declaration=True
    )
    return path


@pytest.fixture()
def project_with_options(tmp_path: Path) -> Path:
    """Standard test project: Debug + Release with debug-level + opt-level
    flag options plus an include-paths list option."""
    proj = tmp_path / "demo"
    proj.mkdir()
    _make_cproject(
        proj,
        configurations=["Debug", "Release"],
        options=[
            ("Debug", "gnu.c.compiler.option.debugging.level", "level.default"),
            ("Debug", "gnu.c.compiler.option.optimization.level", "level.none"),
            ("Release", "gnu.c.compiler.option.debugging.level", "level.none"),
            ("Release", "gnu.c.compiler.option.optimization.level", "level.most"),
        ],
        list_options=[
            ("Debug", "gnu.c.compiler.option.includepath", ["./include"]),
            ("Release", "gnu.c.compiler.option.includepath", ["./include"]),
        ],
    )
    return proj


# ---------------------------------------------------------------------------
# snapshot + commit + rollback
# ---------------------------------------------------------------------------


class TestSnapshotCommitRollback:
    def test_snapshot_creates_backup(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        backup = editor.snapshot()
        assert backup.is_file()
        # Backup is byte-equal to source.
        assert backup.read_bytes() == (project_with_options / ".cproject").read_bytes()

    def test_missing_cproject_raises_loudly(self, tmp_path: Path) -> None:
        proj = tmp_path / "empty"
        proj.mkdir()
        with pytest.raises(CProjectEditError) as excinfo:
            CProjectEditor(proj)
        assert excinfo.value.failed_step == "snapshot"

    def test_rollback_restores_backup(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        original = (project_with_options / ".cproject").read_bytes()

        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.maximum",
        )
        editor.write_and_validate()

        modified = (project_with_options / ".cproject").read_bytes()
        assert modified != original

        editor.rollback()
        restored = (project_with_options / ".cproject").read_bytes()
        assert restored == original

    def test_rollback_no_snapshot_logs_warning(
        self, project_with_options: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        editor = CProjectEditor(project_with_options)
        with caplog.at_level(logging.WARNING):
            editor.rollback()
        assert any("without a usable backup" in r.message for r in caplog.records)

    def test_commit_marks_state(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.commit()
        assert editor._committed is True


# ---------------------------------------------------------------------------
# set_option
# ---------------------------------------------------------------------------


class TestSetOption:
    def test_active_only_modifies_first_cconfig(
        self, project_with_options: Path
    ) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        change = editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.minimal",
        )
        editor.write_and_validate()

        tree = ET.parse(project_with_options / ".cproject")
        debug_opts = [
            opt
            for opt in tree.iter("option")
            if opt.get("superClass")
            == "gnu.c.compiler.option.debugging.level"
        ]
        # 2 options exist (Debug + Release); only Debug modified.
        debug_values = sorted(opt.get("value") for opt in debug_opts)
        assert "level.minimal" in debug_values
        assert "level.none" in debug_values  # Release untouched
        assert change.kind == "set_value"
        assert change.old_value == "level.default"
        assert change.new_value == "level.minimal"

    def test_all_configurations_modifies_every(
        self, project_with_options: Path
    ) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.maximum",
            all_configurations=True,
        )
        editor.write_and_validate()

        tree = ET.parse(project_with_options / ".cproject")
        values = sorted(
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass")
            == "gnu.c.compiler.option.debugging.level"
        )
        assert values == ["level.maximum", "level.maximum"]

    def test_configuration_kwarg_targets_named(
        self, project_with_options: Path
    ) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.optimization\.level",
            value="level.size",
            configuration="Release",
        )
        editor.write_and_validate()

        tree = ET.parse(project_with_options / ".cproject")
        # Debug still has level.none; Release now has level.size.
        opts = list(tree.iter("option"))
        opts_by_config = {}
        for cconfig in tree.iter("cconfiguration"):
            name = cconfig.find("configuration").get("name")
            for opt in cconfig.iter("option"):
                if opt.get("superClass") == "gnu.c.compiler.option.optimization.level":
                    opts_by_config[name] = opt.get("value")
        assert opts_by_config["Release"] == "level.size"
        assert opts_by_config["Debug"] == "level.none"

    def test_zero_match_raises_modify(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        with pytest.raises(CProjectEditError) as excinfo:
            editor.set_option(
                superclass=r"never\.matches\.anything",
                value="x",
            )
        err = excinfo.value
        assert err.failed_step == "modify"
        assert err.superclass_attempted == r"never\.matches\.anything"

    def test_unknown_configuration_raises_modify(
        self, project_with_options: Path
    ) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        with pytest.raises(CProjectEditError):
            editor.set_option(
                superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
                value="level.maximum",
                configuration="DoesNotExist",
            )


# ---------------------------------------------------------------------------
# append_list_value + remove_list_value
# ---------------------------------------------------------------------------


class TestListValueOps:
    def test_append_adds_child(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.append_list_value(
            superclass=r"gnu\.c\.compiler\.option\.includepath",
            value="./vendor",
        )
        editor.write_and_validate()

        tree = ET.parse(project_with_options / ".cproject")
        # The active-only default modifies only the first cconfig (Debug).
        # The Debug option should now have two listOptionValue children.
        debug_opt = next(
            opt
            for cconfig in tree.iter("cconfiguration")
            if cconfig.find("configuration").get("name") == "Debug"
            for opt in cconfig.iter("option")
            if opt.get("superClass") == "gnu.c.compiler.option.includepath"
        )
        values = sorted(c.get("value") for c in debug_opt.findall("listOptionValue"))
        assert values == ["./include", "./vendor"]

    def test_append_is_dedupe(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.append_list_value(
            superclass=r"gnu\.c\.compiler\.option\.includepath",
            value="./include",  # already present
        )
        editor.write_and_validate()

        tree = ET.parse(project_with_options / ".cproject")
        debug_opt = next(
            opt
            for cconfig in tree.iter("cconfiguration")
            if cconfig.find("configuration").get("name") == "Debug"
            for opt in cconfig.iter("option")
            if opt.get("superClass") == "gnu.c.compiler.option.includepath"
        )
        # Still exactly one ./include child.
        values = [c.get("value") for c in debug_opt.findall("listOptionValue")]
        assert values == ["./include"]

    def test_remove_drops_matching_children(
        self, project_with_options: Path
    ) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.remove_list_value(
            superclass=r"gnu\.c\.compiler\.option\.includepath",
            value="./include",
        )
        editor.write_and_validate()

        tree = ET.parse(project_with_options / ".cproject")
        debug_opt = next(
            opt
            for cconfig in tree.iter("cconfiguration")
            if cconfig.find("configuration").get("name") == "Debug"
            for opt in cconfig.iter("option")
            if opt.get("superClass") == "gnu.c.compiler.option.includepath"
        )
        assert debug_opt.findall("listOptionValue") == []


# ---------------------------------------------------------------------------
# write_and_validate
# ---------------------------------------------------------------------------


class TestWriteAndValidate:
    def test_no_snapshot_raises(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        with pytest.raises(CProjectEditError) as excinfo:
            editor.write_and_validate()
        assert excinfo.value.failed_step == "snapshot"

    def test_round_trip_well_formed(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.minimal",
        )
        editor.write_and_validate()
        # Re-parse should succeed (no exception).
        ET.parse(project_with_options / ".cproject")

    def test_prolog_processing_instructions_are_preserved(
        self, tmp_path: Path
    ) -> None:
        """Real STM32CubeIDE .cproject files carry a
        ``<?fileVersion 4.0.0?>`` PI between the XML declaration and the
        ``<cproject>`` root element. Eclipse CDT relies on it to recognise
        the managed-build configuration; without it, projects load but
        Build silently no-ops. Stdlib ET drops PIs on the parse → write
        round-trip; the substrate must splice them back."""
        proj = tmp_path / "demo"
        proj.mkdir()
        cproject = proj / ".cproject"
        # Hand-written .cproject with the prolog PI ET would otherwise drop.
        cproject.write_bytes(
            b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            b"<?fileVersion 4.0.0?>\n"
            b'<cproject storage_type_id="org.eclipse.cdt.core.XmlProjectDescriptionStorage">\n'
            b'  <storageModule moduleId="cdtBuildSystem">\n'
            b'    <cconfiguration id="cfg.debug.id">\n'
            b'      <configuration name="Debug">\n'
            b"        <toolChain>\n"
            b"          <tool>\n"
            b'            <option superClass="gnu.c.compiler.option.debugging.level" value="level.default"/>\n'
            b"          </tool>\n"
            b"        </toolChain>\n"
            b"      </configuration>\n"
            b"    </cconfiguration>\n"
            b"  </storageModule>\n"
            b"</cproject>\n"
        )

        editor = CProjectEditor(proj)
        editor.snapshot()
        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.minimal",
        )
        editor.write_and_validate()

        written = cproject.read_bytes()
        assert b"<?fileVersion 4.0.0?>" in written, (
            f"prolog PI was dropped on round-trip; got:\n{written!r}"
        )
        # The edit itself landed.
        assert b'value="level.minimal"' in written
        # ADR-007: LF-only on the written file.
        assert b"\r\n" not in written

    def test_prolog_pi_absent_does_not_inject_anything(
        self, project_with_options: Path
    ) -> None:
        """``project_with_options`` is synthesised via ``ET.write()`` so it
        has no prolog PI to begin with. Round-trip must not invent one."""
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.minimal",
        )
        editor.write_and_validate()
        written = (project_with_options / ".cproject").read_bytes()
        assert b"<?fileVersion" not in written
        assert b"\r\n" not in written


# ---------------------------------------------------------------------------
# "..." superClass expansion + list-edit no-match WARNING
# ---------------------------------------------------------------------------


class TestSuperClassExpansion:
    """Value strings beginning with literal "..." are spliced with the
    matched <option>'s own superClass at edit time. Real CubeIDE
    encodes enum values as "<superClass>.value.<token>"; the placeholder
    lets one regex match both synthetic + full-ST-prefix .cprojects.
    """

    def test_set_value_expands_placeholder(self, tmp_path: Path) -> None:
        proj = tmp_path / "demo"
        proj.mkdir()
        _make_cproject(
            proj,
            configurations=["Debug"],
            options=[
                ("Debug",
                 "com.example.tool.c.compiler.option.optimization.level",
                 "old.value"),
            ],
        )
        editor = CProjectEditor(proj)
        editor.snapshot()
        change = editor.set_option(
            superclass=r".*\.compiler\.option\.optimization\.level",
            value="....value.most",
        )
        editor.write_and_validate()
        tree = ET.parse(proj / ".cproject")
        values = [
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass")
            == "com.example.tool.c.compiler.option.optimization.level"
        ]
        assert values == [
            "com.example.tool.c.compiler.option.optimization.level.value.most"
        ]
        assert change.new_value == values[0]

    def test_no_placeholder_passes_through(self, tmp_path: Path) -> None:
        proj = tmp_path / "demo"
        proj.mkdir()
        _make_cproject(
            proj,
            configurations=["Debug"],
            options=[("Debug", "gnu.c.linker.option.usenewlibnano", "false")],
        )
        editor = CProjectEditor(proj)
        editor.snapshot()
        editor.set_option(
            superclass=r".*\.linker\.option\.usenewlibnano",
            value="true",  # plain value — no expansion
        )
        editor.write_and_validate()
        tree = ET.parse(proj / ".cproject")
        values = [
            opt.get("value")
            for opt in tree.iter("option")
            if opt.get("superClass") == "gnu.c.linker.option.usenewlibnano"
        ]
        assert values == ["true"]


class TestListEditNoMatchSoftWarn:
    """append_list / remove_list on a non-existent list-shaped option is
    a soft no-op with WARNING. CubeIDE only plants list options after a
    GUI edit, so a vanilla .cproject often lacks them; preset edits
    targeting otherflags etc. shouldn't fail loudly in that case.
    set_value retains its loud-raise behavior.
    """

    def test_append_no_match_warns_and_records_empty_change(
        self, project_with_options: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        with caplog.at_level(logging.WARNING):
            change = editor.append_list_value(
                superclass=r".*\.compiler\.option\.otherflags",
                value="-flto",
            )
        assert change.kind == "append_list"
        assert change.old_value == ()
        assert change.new_value == ()
        assert any("no-op" in r.message for r in caplog.records)

    def test_remove_no_match_warns_and_records_empty_change(
        self, project_with_options: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        with caplog.at_level(logging.WARNING):
            change = editor.remove_list_value(
                superclass=r".*\.compiler\.option\.otherflags",
                value="-flto",
            )
        assert change.kind == "remove_list"
        assert any("no-op" in r.message for r in caplog.records)

    def test_set_value_no_match_still_raises(
        self, project_with_options: Path
    ) -> None:
        # set_value retains the loud-raise contract — covered by
        # TestSetOption::test_zero_match_raises_modify above; this is a
        # contrast sentinel to make the divergence explicit.
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        with pytest.raises(CProjectEditError):
            editor.set_option(
                superclass=r"never\.matches",
                value="x",
            )


# ---------------------------------------------------------------------------
# snapshot_record audit trail
# ---------------------------------------------------------------------------


class TestSnapshotRecord:
    def test_collects_all_changes(self, project_with_options: Path) -> None:
        editor = CProjectEditor(project_with_options)
        editor.snapshot()
        editor.set_option(
            superclass=r"gnu\.c\.compiler\.option\.debugging\.level",
            value="level.minimal",
        )
        editor.append_list_value(
            superclass=r"gnu\.c\.compiler\.option\.includepath",
            value="./extra",
        )
        record = editor.snapshot_record()
        assert isinstance(record, SettingsModification)
        assert record.file == project_with_options / ".cproject"
        assert record.backup_path is not None
        assert len(record.changes) == 2
        kinds = sorted(c.kind for c in record.changes)
        assert kinds == ["append_list", "set_value"]
        assert record.rolled_back is False


# ---------------------------------------------------------------------------
# Presets + FPU table
# ---------------------------------------------------------------------------


class TestPresetTables:
    def test_presets_indexed_by_name(self) -> None:
        assert set(PRESETS.keys()) == {"fast", "size", "balanced"}
        assert PRESETS["fast"] is PRESET_FAST
        assert PRESETS["size"] is PRESET_SIZE
        assert PRESETS["balanced"] is PRESET_BALANCED

    def test_preset_fast_includes_flto(self) -> None:
        ops = [op for op in PRESET_FAST if op[2] == "-flto"]
        # Append for both compiler and linker otherflags.
        assert len(ops) == 2
        for kind, regex, value in ops:
            assert kind == "append_list"
            assert value == "-flto"

    def test_preset_size_uses_newlib_nano(self) -> None:
        # set_value_soft: best-effort — sets usenewlibnano where the option
        # exists, soft-no-ops on untouched ST projects that lack it (rather
        # than raising a protocol failure).
        assert any(
            kind == "set_value_soft" and "usenewlibnano" in regex and value == "true"
            for kind, regex, value in PRESET_SIZE
        )


class TestFpuTable:
    @pytest.mark.parametrize(
        "family,expected",
        [
            ("STM32L4", ("fpv4-sp-d16", "hard")),
            ("STM32H7", ("fpv5-d16", "hard")),
            ("STM32F4", ("fpv4-sp-d16", "hard")),
            ("STM32F7", ("fpv5-d16", "hard")),
            ("STM32F3", ("fpv4-sp-d16", "hard")),
        ],
    )
    def test_canonical_families(
        self, family: str, expected: tuple[str, str]
    ) -> None:
        assert fpu_flags_for_family(family) == expected

    def test_longer_prefix_still_matches(self) -> None:
        # STM32H7Sxx → matches STM32H7
        assert fpu_flags_for_family("STM32H7Sxx") == ("fpv5-d16", "hard")

    def test_no_entry_returns_none(self) -> None:
        # STM32F0 → no entry → None (soft-FP fallback)
        assert fpu_flags_for_family("STM32F0") is None

    def test_none_returns_none(self) -> None:
        assert fpu_flags_for_family(None) is None

    def test_empty_returns_none(self) -> None:
        assert fpu_flags_for_family("") is None

    def test_table_contains_l4_h7(self) -> None:
        assert "STM32L4" in FAMILY_FPU_TABLE
        assert "STM32H7" in FAMILY_FPU_TABLE
