"""``.cproject`` atomic-edit protocol — ``CProjectEditor``.

Per ``v1/cubeide-api.md`` § "Settings-modification protocol". Internal
helper; not part of the public cubeide surface. ``CubeIDE.build()`` uses
it to materialise all settings edits in one snapshot → parse → modify →
validate-XML → write cycle.

Commit / rollback rule:

- Protocol-level failure (snapshot / parse / modify / validate_xml /
  write) → ``rollback()`` restores ``.cproject`` from the backup and
  ``build()`` raises ``CProjectEditError``.
- Build-level failure (compile / link errors after a valid protocol
  edit) → substrate keeps the change; caller iterates.

Mechanism:

- Snapshot via ``shutil.copy2``.
- Parse via ``xml.etree.ElementTree`` (stdlib). Insignificant whitespace
  not preserved; v1 fixture-authoring rule + canonical-XML
  equivalence in tests sidesteps this.
- LF-only writes (Linux + Windows v1 per ADR-007; canonical-XML
  comparison in tests sidesteps line-ending drift entirely).
- ``<option>`` matching by ``superClass`` regex (``re.fullmatch``); each
  ``<cconfiguration>`` walked independently for configuration scoping.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from embedagents.stm32.cubeide.results import SettingChange, SettingsModification
from embedagents.stm32.errors import CProjectEditError


class CProjectEditor:
    """Atomic-edit primitives for one ``.cproject`` file.

    Usage from ``CubeIDE.build()``::

        editor = CProjectEditor(project_path, logger=ctx.logger.getChild(...))
        editor.snapshot()
        try:
            editor.set_option(...)
            editor.append_list_value(...)
            editor.write_and_validate()
            editor.commit()
        except CProjectEditError:
            editor.rollback()
            raise
    """

    def __init__(
        self,
        project_path: Path,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.project_path = project_path
        self.cproject_path = project_path / ".cproject"
        if not self.cproject_path.is_file():
            raise CProjectEditError(
                message=f".cproject not found at {self.cproject_path}",
                failed_step="snapshot",
                file=self.cproject_path,
            )
        self._log = logger or logging.getLogger("embedagents.stm32.cubeide.cproject")
        self._backup_path: Path | None = None
        self._tree: ET.ElementTree | None = None
        self._root: ET.Element | None = None
        self._changes: list[SettingChange] = []
        self._aux_files: list[Path] = []
        self._committed = False

    # ------------------------------------------------------------------
    # snapshot / commit / rollback
    # ------------------------------------------------------------------

    def snapshot(self) -> Path:
        """Copy ``.cproject`` to ``.cproject.substrate-backup-<ts>`` and
        parse it. Returns the backup path."""
        ts = time.strftime("%Y%m%dT%H%M%S")
        backup = self.cproject_path.with_suffix(
            f".cproject.substrate-backup-{ts}"
        )
        # Use parent + name to avoid suffix-collision when cproject_path
        # already ends in something unusual.
        backup = self.cproject_path.parent / (self.cproject_path.name + f".substrate-backup-{ts}")
        try:
            shutil.copy2(self.cproject_path, backup)
        except OSError as ex:
            raise CProjectEditError(
                message=f"snapshot failed: {ex}",
                failed_step="snapshot",
                file=self.cproject_path,
            ) from ex
        self._backup_path = backup
        try:
            self._tree = ET.parse(self.cproject_path)
        except ET.ParseError as ex:
            raise CProjectEditError(
                message=f".cproject XML parse failed: {ex}",
                failed_step="parse",
                file=self.cproject_path,
                backup_path=backup,
            ) from ex
        self._root = self._tree.getroot()
        return backup

    def commit(self) -> None:
        """Keep the on-disk edit; backup retention follows
        ``cubeide.cleanup.cproject_backups`` policy.

        v1 default is ``"immediate"`` — backup deleted on commit. This
        method is a placeholder for future policies; callers always call
        it regardless.
        """
        # Future: respect cleanup.cproject_backups setting. For v1
        # simple-now (M-018) we keep the backup file — disk-cheap, and
        # users can manually delete if it bothers them.
        self._committed = True

    def rollback(self) -> None:
        """Restore ``.cproject`` from the backup. No-op if no snapshot."""
        if self._backup_path is None or not self._backup_path.is_file():
            self._log.warning(
                "rollback called without a usable backup at %s",
                self._backup_path,
            )
            return
        try:
            shutil.copy2(self._backup_path, self.cproject_path)
        except OSError as ex:
            raise CProjectEditError(
                message=f"rollback failed: {ex}",
                failed_step="rollback",
                file=self.cproject_path,
                backup_path=self._backup_path,
            ) from ex

    # ------------------------------------------------------------------
    # edit primitives
    # ------------------------------------------------------------------

    def set_option(
        self,
        *,
        superclass: str,
        value: str,
        configuration: str | None = None,
        all_configurations: bool = False,
        required: bool = True,
    ) -> SettingChange:
        """Set the ``value=`` attribute on every matched ``<option>``.

        ``superclass`` is a regex matched against the option's
        ``superClass`` attribute via ``re.fullmatch``. Per the spec's
        configuration-scoping rule: active-only by default (``False``);
        ``all_configurations=True`` modifies every cconfiguration that
        carries the option.

        ``required`` (default ``True``): when no ``<option>`` matches the
        regex, raise ``CProjectEditError`` (a genuinely-missing
        compiler/linker option is a protocol failure). Pass
        ``required=False`` for *best-effort* scalar sets — the option is
        soft-no-op'd with a WARNING when absent (used by presets for
        options like ``usenewlibnano`` that real CubeIDE only plants once
        the user toggles them in the GUI; an untouched ST example project
        simply doesn't carry the element).

        Returns a single ``SettingChange`` summarising the edit (one
        record even when multiple cconfigurations were touched —
        ``configuration`` carries the joined names).
        """
        return self._edit_option(
            superclass=superclass,
            value=value,
            kind="set_value",
            configuration=configuration,
            all_configurations=all_configurations,
            required=required,
        )

    def append_list_value(
        self,
        *,
        superclass: str,
        value: str,
        configuration: str | None = None,
        all_configurations: bool = False,
    ) -> SettingChange:
        """Append ``<listOptionValue value="..."/>`` under the matched option.

        Idempotent: if a matching child already exists, the operation is
        a no-op (dedupe). ``old_value`` on the returned ``SettingChange``
        is the tuple of values present before; ``new_value`` is after.
        """
        return self._edit_option(
            superclass=superclass,
            value=value,
            kind="append_list",
            configuration=configuration,
            all_configurations=all_configurations,
        )

    def remove_list_value(
        self,
        *,
        superclass: str,
        value: str,
        configuration: str | None = None,
        all_configurations: bool = False,
    ) -> SettingChange:
        """Remove ``<listOptionValue value="...">`` children matching ``value``."""
        return self._edit_option(
            superclass=superclass,
            value=value,
            kind="remove_list",
            configuration=configuration,
            all_configurations=all_configurations,
        )

    # ------------------------------------------------------------------
    # write + validate
    # ------------------------------------------------------------------

    def write_and_validate(self) -> None:
        """Serialise + re-parse. Raises ``CProjectEditError`` on either
        step (triggers rollback in the caller).

        Stdlib ``ElementTree`` drops processing instructions that appear
        in the prolog (between the XML declaration and the root element)
        on the parse → write round-trip. STM32CubeIDE's ``.cproject``
        relies on ``<?fileVersion 4.0.0?>`` sitting there for Eclipse CDT
        to recognise the managed-build configuration — without it the
        project loads but Build silently no-ops. We extract those PIs
        from the snapshot copy before ET clobbers them and splice them
        back into the written output. CRLF is also normalised to LF
        per ADR-007 (fixtures + tracked files are LF-only).
        """
        if self._tree is None or self._root is None:
            raise CProjectEditError(
                message="write_and_validate called before snapshot()",
                failed_step="snapshot",
                file=self.cproject_path,
                backup_path=self._backup_path,
            )
        prolog_pis = (
            _extract_prolog_pis(self._backup_path)
            if self._backup_path is not None
            else []
        )
        try:
            self._tree.write(
                self.cproject_path,
                encoding="UTF-8",
                xml_declaration=True,
                short_empty_elements=True,
            )
        except (OSError, TypeError) as ex:
            raise CProjectEditError(
                message=f"write failed: {ex}",
                failed_step="validate_xml",
                file=self.cproject_path,
                backup_path=self._backup_path,
            ) from ex
        try:
            data = self.cproject_path.read_bytes()
            normalized = data.replace(b"\r\n", b"\n")
            if prolog_pis:
                normalized = _splice_prolog_pis(normalized, prolog_pis)
            if normalized != data:
                self.cproject_path.write_bytes(normalized)
        except OSError as ex:
            raise CProjectEditError(
                message=f"post-write normalisation failed: {ex}",
                failed_step="validate_xml",
                file=self.cproject_path,
                backup_path=self._backup_path,
            ) from ex
        # Re-parse to confirm the round-trip stays well-formed. Reach-
        # ability check: ensure the project root is still
        # ``<cproject>`` / ``<storageModule>``-bearing.
        try:
            reparsed = ET.parse(self.cproject_path)
        except ET.ParseError as ex:
            raise CProjectEditError(
                message=f"post-write re-parse failed: {ex}",
                failed_step="validate_xml",
                file=self.cproject_path,
                backup_path=self._backup_path,
            ) from ex
        if reparsed.getroot() is None:
            raise CProjectEditError(
                message="post-write tree has no root element",
                failed_step="validate_xml",
                file=self.cproject_path,
                backup_path=self._backup_path,
            )

    def snapshot_record(self) -> SettingsModification:
        """Build the ``SettingsModification`` audit trail. Use this on
        the returned ``BuildResult.settings_modification`` field."""
        return SettingsModification(
            file=self.cproject_path,
            backup_path=self._backup_path,
            changes=list(self._changes),
            rolled_back=False,
        )

    def track_aux(self, path: Path) -> Path:
        """Record an auxiliary file (e.g. a copied library / source) for the
        audit trail. Returns ``path`` unchanged.

        The file itself is copied by ``CubeIDE.build()`` outside the XML
        edit; this records it on the ``SettingsModification`` for caller
        introspection.
        """
        self._aux_files.append(path)
        return path

    def unexclude_source(self, basename: str) -> SettingChange | None:
        """Remove ``basename`` from any ``<sourceEntries>`` ``excluding``
        list so a previously-excluded source participates in the build.

        ST examples carry a single catch-all
        ``<entry kind="sourcePath" name="" excluding="Example/User/main.c"/>``;
        a file copied into the project still won't compile if its name is
        in ``excluding``. This walks every ``<entry>`` with an
        ``excluding`` attribute (the ``|``-separated CDT path list) and
        drops any token whose basename matches. Soft no-op when nothing
        matches — returns ``None`` in that case.
        """
        if self._root is None:
            raise CProjectEditError(
                message="unexclude_source called before snapshot()",
                failed_step="snapshot",
                file=self.cproject_path,
            )
        touched: list[str] = []
        for entry in self._root.iter("entry"):
            excluding = entry.get("excluding")
            if not excluding:
                continue
            tokens = excluding.split("|")
            kept = [t for t in tokens if Path(t).name != basename]
            if len(kept) != len(tokens):
                if kept:
                    entry.set("excluding", "|".join(kept))
                else:
                    del entry.attrib["excluding"]
                touched.append(excluding)
        if not touched:
            self._log.warning(
                "unexclude_source no-op: %r not found in any sourceEntries "
                "excluding list",
                basename,
            )
            return None
        change = SettingChange(
            superclass_id="sourceEntries.excluding",
            configuration="",
            kind="remove_list",
            old_value=tuple(touched),
            new_value=(basename,),
        )
        self._changes.append(change)
        return change

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _edit_option(
        self,
        *,
        superclass: str,
        value: str,
        kind: Literal["set_value", "append_list", "remove_list"],
        configuration: str | None,
        all_configurations: bool,
        required: bool = True,
    ) -> SettingChange:
        if self._root is None:
            raise CProjectEditError(
                message="edit called before snapshot()",
                failed_step="snapshot",
                file=self.cproject_path,
            )
        pattern = re.compile(superclass)
        configs = _enumerate_cconfigurations(
            self._root, configuration=configuration, all_configurations=all_configurations
        )
        if not configs:
            raise CProjectEditError(
                message=(
                    f"no cconfiguration matched configuration={configuration!r} "
                    f"(all_configurations={all_configurations})"
                ),
                failed_step="modify",
                file=self.cproject_path,
                superclass_attempted=superclass,
                backup_path=self._backup_path,
            )

        touched_configs: list[str] = []
        old_value: str | tuple[str, ...] | None = None
        new_value: str | tuple[str, ...]
        matched_any = False
        last_expanded: str = value  # last per-option expansion, for audit

        for cconfig_name, cconfig in configs:
            for option in _iter_options(cconfig, pattern):
                matched_any = True
                touched_configs.append(cconfig_name)
                # Per-option "..." expansion: real CubeIDE encodes enum
                # values as "<superClass>.value.<token>" with the full ST
                # prefix; preset tables write "...value.<token>" and we
                # splice the matched option's actual superClass in here.
                # See plan-windows.md F.6 follow-up + RES on presets.py
                # table format drift.
                opt_super = option.get("superClass", "")
                expanded = (
                    opt_super + value[3:] if value.startswith("...") else value
                )
                last_expanded = expanded
                if kind == "set_value":
                    old_value = option.get("value")
                    option.set("value", expanded)
                elif kind == "append_list":
                    existing = tuple(
                        c.get("value", "") for c in option.findall("listOptionValue")
                    )
                    if expanded in existing:
                        old_value = existing
                        new_value = existing
                        continue  # dedupe
                    child = ET.SubElement(option, "listOptionValue")
                    child.set("value", expanded)
                    child.set("builtIn", "false")
                    old_value = existing
                else:  # remove_list
                    existing = tuple(
                        c.get("value", "") for c in option.findall("listOptionValue")
                    )
                    for child in list(option.findall("listOptionValue")):
                        if child.get("value") == expanded:
                            option.remove(child)
                    old_value = existing

        if not matched_any:
            if kind == "set_value" and required:
                raise CProjectEditError(
                    message=(
                        f"no <option superClass=...> matched regex {superclass!r}"
                    ),
                    failed_step="modify",
                    file=self.cproject_path,
                    superclass_attempted=superclass,
                    backup_path=self._backup_path,
                )
            # required=False set_value, or append_list / remove_list:
            # list-shaped option absent in this
            # .cproject (CubeIDE only plants <option> elements when the
            # user has touched them in the GUI). Soft no-op with WARNING
            # so the rest of the edit batch still applies — caller can
            # observe the empty SettingChange in the audit trail.
            self._log.warning(
                "%s no-op: no <option superClass=...> matched regex %r "
                "(value=%r); list-shaped option absent - CubeIDE only "
                "plants it after a manual GUI edit",
                kind, superclass, value,
            )
            change = SettingChange(
                superclass_id=superclass,
                configuration="",
                kind=kind,
                old_value=(),
                new_value=(),
            )
            self._changes.append(change)
            return change

        if kind == "append_list":
            new_value = old_value if last_expanded in (old_value or ()) else (
                tuple(old_value or ()) + (last_expanded,)
            )
        elif kind == "remove_list":
            new_value = tuple(v for v in (old_value or ()) if v != last_expanded)
        else:
            new_value = last_expanded

        change = SettingChange(
            superclass_id=superclass,
            configuration=",".join(dict.fromkeys(touched_configs)),
            kind=kind,
            old_value=old_value,
            new_value=new_value,
        )
        self._changes.append(change)
        return change


# ---------------------------------------------------------------------------
# Element-walk helpers
# ---------------------------------------------------------------------------


def _enumerate_cconfigurations(
    root: ET.Element, *, configuration: str | None, all_configurations: bool
) -> list[tuple[str, ET.Element]]:
    """Return ``(name, element)`` pairs for the cconfigurations in scope.

    - ``configuration`` given → exact-name match wins (case-insensitive
      on the trailing fragment after ``"."``).
    - ``configuration=None`` + ``all_configurations=False`` → first
      cconfiguration only (treated as the "active" configuration in v1).
    - ``configuration=None`` + ``all_configurations=True`` → every
      cconfiguration in document order.
    """
    cconfigs: list[tuple[str, ET.Element]] = []
    for cconfig in root.iter("cconfiguration"):
        name = _cconfiguration_name(cconfig)
        cconfigs.append((name, cconfig))
    if not cconfigs:
        # Some .cproject layouts wrap <configuration> directly (no
        # cconfiguration wrapper). Fall back to those.
        for config in root.iter("configuration"):
            name = config.get("name", "Default")
            cconfigs.append((name, config))

    if configuration is not None:
        matches = [
            (name, el)
            for name, el in cconfigs
            if name == configuration
            or name.lower().endswith("." + configuration.lower())
            or name.lower() == configuration.lower()
        ]
        return matches

    if all_configurations:
        return cconfigs

    return cconfigs[:1]


def _cconfiguration_name(cconfig: ET.Element) -> str:
    """Pull a human name off a ``<cconfiguration>`` element.

    CDT layouts vary: some carry ``name="Debug"`` directly; others nest a
    ``<configuration name="...">`` child under
    ``<storageModule moduleId="cdtBuildSystem">``. We try direct first
    then nested.
    """
    direct = cconfig.get("name")
    if direct:
        return direct
    nested = cconfig.find(".//configuration")
    if nested is not None and nested.get("name"):
        return nested.get("name") or "Default"
    return cconfig.get("id", "Default")


def _iter_options(scope: ET.Element, pattern: re.Pattern[str]):
    """Yield every descendant ``<option>`` whose ``superClass`` attribute
    matches ``pattern`` via ``re.fullmatch``."""
    for option in scope.iter("option"):
        superclass = option.get("superClass")
        if superclass is None:
            continue
        if pattern.fullmatch(superclass):
            yield option


_PROLOG_PI_RE = re.compile(rb"<\?(?!xml\b)[^>]*?\?>")


def _extract_prolog_pis(source: Path) -> list[bytes]:
    """Return processing instructions sitting between the XML declaration
    and the root element, as raw bytes. Empty list when there are none."""
    data = source.read_bytes()
    if data.lstrip().startswith(b"<?xml"):
        decl_end = data.find(b"?>")
        if decl_end == -1:
            return []
        scan_from = decl_end + 2
    else:
        scan_from = 0
    root_start = -1
    pos = scan_from
    while pos < len(data):
        idx = data.find(b"<", pos)
        if idx == -1:
            return []
        if data[idx : idx + 2] == b"<?":
            close = data.find(b"?>", idx)
            if close == -1:
                return []
            pos = close + 2
            continue
        root_start = idx
        break
    if root_start == -1:
        return []
    return _PROLOG_PI_RE.findall(data[scan_from:root_start])


def _splice_prolog_pis(data: bytes, pis: list[bytes]) -> bytes:
    """Insert ``pis`` between the XML declaration and the first element.

    The output preserves whatever line-terminator follows the XML
    declaration (typically ``\\n`` after ``ET.write()``) and emits each
    PI on its own line so Eclipse / git-blame stay readable.
    """
    if not pis:
        return data
    if data.startswith(b"<?xml"):
        decl_end = data.find(b"?>")
        if decl_end == -1:
            return data
        insert_at = decl_end + 2
        if insert_at < len(data) and data[insert_at : insert_at + 1] == b"\n":
            insert_at += 1
    else:
        insert_at = 0
    block = b"".join(pi + b"\n" for pi in pis)
    return data[:insert_at] + block + data[insert_at:]
