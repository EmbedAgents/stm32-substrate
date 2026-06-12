# Test fixtures — `cubeide` module

**Last updated:** 2026-05-11 (round-1 review answers integrated; restructured as user-provides catalog per Q1–Q4; Q5 sibling-helper-process; M-020 LF-only fixtures + cross-OS forward-compat)
**Status:** Round-1 review answers integrated. Paired with `cubeide-api.md` (round-1 answers integrated 2026-05-11) per T-005. **Per-tool module sign-off complete; fixture supply happens incrementally during code phase.**
**Scope:** test inputs the cubeide module's tests need across unit / smoke / hardware / eval layers.
**Build status:** spec only. Fixture **artifacts are supplied by the user** during the code phase as the substrate's wrappers land. Each requirement in this catalog accepts multiple supplied artifacts (variants by device, CubeIDE version, project shape, etc.).

---

## How this fixture catalog works

Each fixture requirement below has:

- **ID** — stable identifier (e.g. `F-CP-SINGLE`).
- **Status** — `[in]` (artifacts supplied; tests run) or `[out]` (artifacts pending; dependent tests skip with a clear message).
- **Description** — what the fixture is, in one sentence.
- **Features required** — checklist for the user when authoring/supplying the artifact.
- **Drop path** — `tests/fixtures/cubeide/<ID>/` directory the user populates.
- **Multi-artifact** — yes (every requirement accepts ≥1 artifact; multiple variants improve coverage).
- **Drives tests** — which substrate behaviors this fixture exercises.

**Test-harness convention** (implemented during code phase):

```python
# tests/fixtures/cubeide/conftest.py
def supplied_artifacts(req_id: str) -> list[Path]:
    """All files under tests/fixtures/cubeide/<req_id>/, or [] if dir missing/empty."""

# Each requirement gets a pytest fixture that parametrizes over supplied artifacts:
@pytest.fixture(params=supplied_artifacts("F-CP-SINGLE"))
def single_config_cproject(request) -> Path:
    if not request.param:
        pytest.skip("F-CP-SINGLE has no supplied artifacts; "
                    "drop a .cproject in tests/fixtures/cubeide/F-CP-SINGLE/")
    return request.param
```

Tests parametrize automatically over every supplied artifact; an empty fixture directory cleanly skips dependent tests with the path to populate. Adding more variants is just dropping more files.

**v1 fixture-authoring rules** (per M-020 — Linux-only, but design forward-compat for Windows):

- All `.cproject` files use **LF line endings** (substrate writes LF; canonical-XML comparison consumes both sides regardless, but LF keeps git diffs sane). v2 will add CRLF support.
- Project names + paths anonymized where the artifact is derived from a user's real project.
- Reference projects + workspaces are generated on Linux against the user's installed CubeIDE; v2 will accept Windows-generated variants.

---

## Catalog at a glance

| Group | Count | Path prefix |
|---|---|---|
| Reference projects (build targets) | 8 | `projects/F-PROJ-*` (lives under top-level `tests/fixtures/projects/` per T-004 cross-tool sharing) |
| `.cproject` baselines | 8 | `cproject-baselines/F-CP-*` |
| Post-edit `.cproject` pairs | 9 | `cproject-edits/F-CPE-*` |
| Workspace state snapshots | 6 | `workspaces/F-WS-*` |
| Build outcome logs | 6 | `build-outcomes/F-OUT-*` |
| Descriptors (substrate-authored, not user-provided) | 7 | `descriptors/` |

---

## Reference projects (Q1 + Q4 user-provides)

Real STM32 projects the user supplies. Built artifacts (`.elf`, `.map`, `.bin`) check in alongside the source. Multi-artifact = the user can supply multiple devices/variants per requirement; tests parametrize.

### F-PROJ-NUCLEO-L476RG-BLINKY — Clean-building project (happy path)

**Status:** `[out]`
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/`
**Description:** Any minimal STM32 project that builds clean with `headless-build.sh`.

**Features required:**
- Builds clean against the user's installed CubeIDE + arm-none-eabi-gcc.
- Has both `.project` and `.cproject` files.
- Single default configuration named `Debug`.
- No external dependencies.

**Multi-artifact:** yes — supply multiple devices (NUCLEO-L476RG, NUCLEO-H743, NUCLEO-U575, etc.) for cross-device coverage.

**Drives tests:** `build()` happy path; smoke tests; cross-module compound tests (build → flash via cubeprogrammer).

### F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE — Compile error

**Status:** `[out]`
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE/`
**Description:** STM32 project with a deliberate compile error (e.g. an undefined symbol referenced from `main`).

**Features required:**
- Same structure as F-PROJ-NUCLEO-L476RG-BLINKY.
- `main.c` contains at least one line that gcc will reject (e.g. `undefined_function();` or missing-semicolon).
- Build is expected to fail with `errors_count > 0` (a few error lines, ideally with file/line info gcc emits).

**Multi-artifact:** yes — different error kinds (undefined symbol, type mismatch, syntax error) for parser-side substring coverage.

**Drives tests:** `BuildResult.success=False` outcome; substring assertion `"error:"` appears in `console_output`; smoke layer "real CLI produces the expected failure shape".

### F-PROJ-NUCLEO-L476RG-BROKEN-LINK — Linker error

**Status:** `[out]`
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BROKEN-LINK/`
**Description:** STM32 project that compiles but fails at link (e.g. `extern void missing();` called but never defined).

**Features required:**
- Compile succeeds.
- Link fails with `undefined reference to ...` or `region 'FLASH' overflowed`.

**Multi-artifact:** yes — undefined-reference, multiple-definition, region-overflow variants.

**Drives tests:** `BuildResult.success=False` outcome where the error is at link stage; substring assertions for ld-style errors.

### F-PROJ-NUCLEO-L476RG-VCP-ECHO — UART echo firmware

**Status:** `[out]`
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/`
**Description:** Project that reads bytes from VCP and echoes them back. Used by cubeide tests transitively + cross-module compound tests.

**Features required:**
- Builds clean.
- After flashing + reset, target echoes any VCP byte back within ~100ms.
- Uses standard CDC ACM or USART2-via-ST-LINK-VCP path (whichever matches the device).

**Multi-artifact:** yes — different devices.

**Drives tests:** cross-module compound (cubeide builds → cubeprogrammer flashes → vcp observes echo).

### F-PROJ-NUCLEO-L476RG-FAULTING — Hard-fault firmware

**Status:** `[out]`
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-FAULTING/`
**Description:** Project that deliberately triggers a hard fault (e.g. dereferences a misaligned address or executes from non-executable memory).

**Features required:**
- Builds clean.
- After reset, target enters hard fault within ~1s.
- Standard ARM Cortex-M fault behavior (no custom fault handler).

**Multi-artifact:** yes.

**Drives tests:** cross-module (cubeide builds → cubeprogrammer's DIAG-001 `-hf` path).

### F-PROJ-DISCO-H747XI-DUAL-CORE — Multi-core nested project

**Status:** present on bench (user-provides per RES-019)
**Path:** `tests/fixtures/projects/STM32CubeH7/Projects/STM32H747I-DISCO/Applications/USB_Host/MSC_Standalone/STM32CubeIDE/`
**Description:** Real ST USB-Host MSC standalone example on the STM32H747I-DISCO Discovery board. Dual-core nested layout: parent `STM32CubeIDE/.project` references two child Eclipse projects under `STM32CubeIDE/CM7/.cproject` (Cortex-M7 @ 480 MHz; host-side USB MSC stack) and `STM32CubeIDE/CM4/.cproject` (Cortex-M4 @ 240 MHz; secondary core). Cloned from STM32CubeH7 firmware bundle.

**Features required:**
- Parent `.project` + per-core `<core>/.cproject` files at depth 1.
- Both children build clean.

**Multi-artifact:** yes — this canonical dual-core entry covers H7; future variants can supply U5+M33-secure (TrustZone), STM32MP1 (Cortex-A7 + M4), etc.

**Drives tests:** `find_project()` depth-2 search; project-path resolution for nested layouts; dual-core debug attach (CM7 + CM4 sessions).

### F-PROJ-DISCO-H747XI-FPU — Hardware-FPU exerciser

**Status:** present on bench (user-provides per RES-019)
**Path:** `tests/fixtures/projects/STM32CubeH7/Projects/STM32H747I-DISCO/Applications/FPU/FPU_Fractal/STM32CubeIDE/`
**Description:** Real ST FPU_Fractal example on the STM32H747I-DISCO. Mandelbrot-style floating-point exerciser running on the H747's CM7 core (480 MHz Cortex-M7 + double-precision FPU). Same parent + CM4/CM7 children nested layout as DUAL-CORE.

**Features required:**
- Builds clean.
- Device has hardware FPU (H7 family — fpv5-d16, double-precision).
- `.cproject` carries the FPU/floatabi options under the `managedbuild.option.fpu` / `floatabi` superclasses (per real CubeIDE format).

**Multi-artifact:** yes — H7 entry first; future variants can supply M4F (fpv4-sp-d16), M33-secure, etc.

**Drives tests:** `build(preset="fast")` FPU edit application against the H7 family; substrate's `presets.FAMILY_FPU_TABLE` lookup for `STM32H7` → `("fpv5-d16", "hard")`.

### F-PROJ-STM32H7S78-DK-MULTI-FOLDER — Nested CubeIDE workspace (Appli + Boot)

**Status:** present on bench
**Path:** `tests/fixtures/projects/STM32CubeH7RS/Projects/STM32H7S78-DK/Applications/USB_Device/MSC_Standalone/`
**Description:** Real ST USB-MSC standalone example using the nested-CubeIDE-workspace pattern: a parent `STM32CubeIDE/.project` references two child Eclipse projects under `STM32CubeIDE/Appli/` (`.cproject` + `.project`) and `STM32CubeIDE/Boot/` (`.cproject` + `.project`). Distinct linker scripts per child (`STM32H7S7L8HXH_RAMxspi1_ROMxspi2_app.ld` for Appli; `STM32H7S7L8HXH_FLASH.ld` for Boot). User-provides per RES-019; cloned from STM32CubeH7RS firmware bundle.

**Features required:**
- Parent `STM32CubeIDE/.project` declaring child projects.
- ≥ 2 child `.cproject` files (Appli + Boot here) under depth 0–2.
- Different project names so `find_project(name=...)` exact-match resolves cleanly.

**Multi-artifact:** one fixture artifact = the on-disk MSC_Standalone tree. Other H7S78 multi-folder examples in the same bundle (`Templates/`, `Examples/`) can supplement when authored.

**Drives tests:** `find_project()` ambiguity → `ProjectAmbiguityError`; `find_project(name="Appli")` / `find_project(name="Boot")` exact-match-wins; substring-fallback via `on_ambiguous` callback.

---

## `.cproject` baselines (Q2 user-provides)

Real `.cproject` files copied from user projects (anonymized for project names / paths). Multi-artifact = several `.cproject` files per requirement, each tagged with device/CubeIDE-version. Tests parametrize across all of them so the protocol's correctness is verified against the full matrix.

### F-CP-SINGLE — Single-configuration baseline

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-SINGLE/`
**Description:** `.cproject` with exactly one `<cconfiguration>` child (most common shape).

**Features required:**
- Exactly one `<cconfiguration>` element.
- Modern CDT (≥ 1.7) auto-discovery mode (no explicit `<sourceEntries>` enumerating files).
- Standard ST `<option>` superClass IDs (no third-party plugin variants).
- LF line endings.

**Drives tests:** baseline editor round-trip; happy path for `editor.set_option` + `append_list_value` across all the property-page mapping rows.

### F-CP-MULTI — Multi-configuration baseline

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-MULTI/`
**Description:** `.cproject` with both `Debug` and `Release` cconfigurations.

**Features required:**
- Two `<cconfiguration>` children, names `Debug` and `Release`.
- Different `optimization.level` values across the two (e.g. `none` for Debug, `more` for Release).
- LF line endings.

**Drives tests:** `<option>` configuration scoping (active-only by default per Q6); `modify_all_configurations=True` flips behavior.

### F-CP-NESTED — Dual-core nested baseline

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-NESTED/`
**Description:** `.cproject` from a dual-core project where the file lives in a subfolder.

**Features required:**
- Reachable at depth 1–2 from a project-root folder (the directory structure is preserved in the fixture).
- Either CM4 or CM7 side of a dual-core split, your choice.

**Drives tests:** `find_project` depth-2 search; nested-project resolution.

### F-CP-FPU — FPU-options-present baseline

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-FPU/`
**Description:** `.cproject` from an FPU-capable device with floatabi/fpu options present.

**Features required:**
- `<option superClass=...compiler.option.floatabi>` element present.
- `<option superClass=...compiler.option.fpu>` element present.
- Initial value: `floatabi=soft` (so the `preset="fast"` edit has somewhere to write).

**Drives tests:** `build(preset="fast")` FPU-detection-and-edit path.

### F-CP-OLDER-CDT — Explicit-source-list baseline

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-OLDER-CDT/`
**Description:** Pre-1.7 CDT project with explicit `<sourceEntries>` enumerating individual files (no auto-discovery).

**Features required:**
- `<sourceEntries>` present.
- One or more `<entry>` children inside it.
- LF line endings.

**Drives tests:** B-013 add-source path that requires editing the source-entries list (vs. the modern auto-discovery path).

### F-CP-MODERN-CDT — Auto-discovery baseline

**Status:** `[in]` (covered by F-CP-SINGLE)
**Path:** *(same as F-CP-SINGLE — auto-discovery is the default modern shape)*
**Description:** `.cproject` from modern CubeIDE (≥ 1.7) with auto-discovery (no explicit `<sourceEntries>` per-file).

**Drives tests:** B-013 add-source path that skips XML edits (CDT picks the new file up automatically).

*(This requirement is folded into F-CP-SINGLE in v1; modern auto-discovery IS the F-CP-SINGLE baseline. Splitting it out only if a future test needs to disambiguate.)*

### F-CP-THIRDPARTY-PLUGIN — Non-standard superClass

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-THIRDPARTY-PLUGIN/`
**Description:** Hand-authored `.cproject` with at least one `<option>` whose `superClass` attribute doesn't match the canonical regex (simulating a third-party plugin's option).

**Features required:**
- Valid XML.
- At least one `<option>` with `superClass="com.example.foo.option.weird"`.
- Substrate's editor should NOT match this option for any of the standard recipes.

**Drives tests:** `CProjectEditError(failed_step="modify")` when the user tries to edit a non-standard option; verifies the regex matcher doesn't accidentally consume third-party options.

### F-CP-MALFORMED — Unreachable-configuration baseline

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-baselines/F-CP-MALFORMED/`
**Description:** Hand-authored `.cproject`: XML is well-formed but the `<option>` the user is trying to edit isn't reachable from the active `<cconfiguration>` (e.g. it's orphaned in a sibling tree).

**Features required:**
- Well-formed XML (re-parses without error).
- A target `<option>` element exists, but it's not in the active configuration's `<toolChain>` subtree.

**Drives tests:** `validate_xml` step's reachability check; `CProjectEditError(failed_step="validate_xml")` raised.

---

## Post-edit `.cproject` pairs (Q3 user-authors)

Each entry below is a **transformation recipe**: take baseline B, apply edit E, produce expected file F. The user authors F by hand-editing B. **Comparison is canonical-XML, not byte-compare** — `xml.etree.ElementTree`'s serializer has non-trivial whitespace / attribute-order / quoting behavior that's brittle to hand-match. Substrate canonicalizes both its produced output and the expected file (`xml.etree.ElementTree.canonicalize()` from stdlib) before comparing. The user-authored expected file therefore only needs to be *semantically* equivalent — whitespace, attribute order, and quote style are normalized away.

Recommended layout per recipe (e.g., `F-CPE-DBG-G3/`):

```
F-CPE-DBG-G3/
├── input.cproject          # baseline used for the recipe (a copy of the matching F-CP-SINGLE input)
├── expected.cproject       # user-hand-authored expected output (canonical form will be derived at test time)
└── edit.json               # the recipe (substrate's editor inputs: which superClass, what value)
```

`edit.json` documents the inputs that drive the editor (kwargs to `build()` for this recipe — e.g., `{"debug_level": "-g3"}`). Tests load `input.cproject`, apply the editor, canonicalize both sides, compare.

Multi-artifact: for each baseline you supply under the corresponding `F-CP-*` requirement above, you also supply the matching post-edit file under the `F-CPE-*` requirement, with the same filename. Tests parametrize over the pair list.

### F-CPE-DBG-G3 — Set debug level to -g3

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-DBG-G3/`
**Source baseline:** F-CP-SINGLE (one .cproject for every supplied F-CP-SINGLE variant)

**Transformation recipe:**
- Locate `<option>` whose `superClass` attribute matches the regex `.*\.compiler\.option\.debugging\.level`.
- Change its `value` attribute to the CDT enum string for `-g3` (typically `gnu.c.compiler.debugging.level.max` — the exact string may vary by CDT version; preserve whatever the baseline's enum prefix is and swap the suffix to `.max`).
- Touch nothing else.

**Drives tests:** `build(debug_level="-g3")` editor round-trip; verifies set_option produces byte-equivalent output to user-authored expected.

### F-CPE-OPT-OS — Set optimization to -Os

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-OPT-OS/`
**Source baseline:** F-CP-SINGLE

**Transformation recipe:**
- Locate `<option>` whose `superClass` matches `.*\.compiler\.option\.optimization\.level`.
- Change `value` to the `.size` enum (e.g. `gnu.c.optimization.level.size`).
- Touch nothing else.

**Drives tests:** `build(optimization="-Os")` editor round-trip.

### F-CPE-SYM-DEBUG — Append "DEBUG" preprocessor symbol

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-SYM-DEBUG/`
**Source baseline:** F-CP-SINGLE

**Transformation recipe:**
- Locate `<option>` whose `superClass` matches `.*\.compiler\.option\.preprocessor\.def\.symbols`.
- Append a new `<listOptionValue builtIn="false" value="DEBUG"/>` child as the LAST child of that `<option>`.
- Touch nothing else.

**Drives tests:** `build(add_symbols=["DEBUG"])` editor round-trip; list-append behavior.

### F-CPE-INC-PATH — Append "./include" to include paths

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-INC-PATH/`
**Source baseline:** F-CP-SINGLE

**Transformation recipe:**
- Locate `<option>` whose `superClass` matches `.*\.compiler\.option\.includepath`.
- Append `<listOptionValue builtIn="false" value="./include"/>` as the LAST child of that `<option>`.
- Touch nothing else.

**Drives tests:** `build(add_include_paths=["./include"])` editor round-trip.

### F-CPE-LIB-PATHNAME — Append lib path + lib name in one snapshot

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-LIB-PATHNAME/`
**Source baseline:** F-CP-SINGLE

**Transformation recipe:**
- Locate `<option>` matching `.*\.linker\.option\.paths`; append `<listOptionValue builtIn="false" value="${workspace_loc:/proj/Lib}"/>` as last child.
- Locate `<option>` matching `.*\.linker\.option\.libs`; append `<listOptionValue builtIn="false" value="mylib"/>` as last child.
- Both edits in the same file. Touch nothing else.

**Drives tests:** `build(add_libraries=[Path("mylib.a")])` two-edit-one-snapshot behavior.

### F-CPE-PRESET-FAST — preset="fast" multi-edit

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-PRESET-FAST/`
**Source baseline:** F-CP-SINGLE (variants WITHOUT hardware FPU)

**Transformation recipe:**
- `optimization.level` → enum for `-O3` (`gnu.c.optimization.level.most`).
- `debugging.level` → enum for `-g1` (`gnu.c.compiler.debugging.level.minimal`).
- `.compiler.option.otherflags` → append `<listOptionValue builtIn="false" value="-flto"/>` as last child.
- `.linker.option.otherflags` → append `<listOptionValue builtIn="false" value="-flto"/>` as last child.
- No FPU edits (this baseline doesn't have hardware FPU).
- Touch nothing else.

**Drives tests:** `build(preset="fast")` on non-FPU devices.

### F-CPE-PRESET-FAST-FPU — preset="fast" with FPU

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-PRESET-FAST-FPU/`
**Source baseline:** F-CP-FPU

**Transformation recipe:**
- All edits from F-CPE-PRESET-FAST, PLUS:
- `compiler.option.floatabi` → set `value` to the `.hard` enum.
- `compiler.option.fpu` → set `value` to the matching FPU enum for the device (e.g. `fpv4-sp-d16` for M4F).
- Touch nothing else.

**Drives tests:** `build(preset="fast")` on FPU-capable devices.

### F-CPE-PRESET-SIZE — preset="size" multi-edit

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-PRESET-SIZE/`
**Source baseline:** F-CP-SINGLE

**Transformation recipe:**
- `optimization.level` → enum for `-Os` (`.size`).
- `debugging.level` → enum for `-g1` (`.minimal`).
- `.linker.option.otherflags` → append `<listOptionValue value="-Wl,--gc-sections"/>` as last child.
- `linker.option.usenewlibnano` → set `value="true"` (or whatever the CDT enum is).
- Touch nothing else.

**Drives tests:** `build(preset="size")`.

### F-CPE-PRESET-BALANCED — preset="balanced" multi-edit

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/cproject-edits/F-CPE-PRESET-BALANCED/`
**Source baseline:** F-CP-SINGLE

**Transformation recipe:**
- `optimization.level` → enum for `-O2` (`.more`).
- `debugging.level` → enum for `-g` (`.default`).
- Touch nothing else.

**Drives tests:** `build(preset="balanced")`.

---

## Workspace state snapshots (Q4 user-provides)

Real CubeIDE workspaces captured at specific states. The user generates these by running CubeIDE on the supplied F-PROJ-* projects and snapshotting the workspace directory at the right moment. Each snapshot is a directory tree; tests copy it to `tmp_path` per test, mutate, run substrate.

### F-WS-EMPTY — Fresh workspace

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/workspaces/F-WS-EMPTY/`
**Description:** A workspace that's been created (`-data <ws>`) but no project imported yet.

**Features required:**
- `.metadata/` exists.
- No `.metadata/.plugins/org.eclipse.core.resources/.projects/*/` entries.
- No `.metadata/.lock` file (or empty, no holder).

**Drives tests:** first-build path; `-import` prepended; `BuildResult.project_imported=True`.

### F-WS-IMPORTED-CLEAN — Project imported, consistent state

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/workspaces/F-WS-IMPORTED-CLEAN/`
**Description:** Workspace with a project imported, `.location` pointing at the project's actual on-disk path.

**Features required:**
- `.metadata/.plugins/org.eclipse.core.resources/.projects/<project_name>/.location` exists.
- The decoded URI in `.location` points at a real path that exists.
- Project's tree exists at that path (could be inside or outside the workspace).
- LF-only text files inside `.metadata/` (where applicable).

**Drives tests:** subsequent-build path; no `-import`; `project_imported=False`.

### F-WS-STALE-IMPORTED — Stale .location

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/workspaces/F-WS-STALE-IMPORTED/`
**Description:** Workspace with a `.location` registration pointing at a path that no longer exists (project moved or deleted).

**Features required:**
- `.location` exists with a decodable URI.
- That URI points at a path that does NOT exist on disk in the fixture.

**Drives tests:** broader-cleanup path per Q7; WARNING enumeration logged; `-import` re-runs.

### F-WS-STALE-TREE — Orphan project tree

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/workspaces/F-WS-STALE-TREE/`
**Description:** Workspace with `<ws>/<project_name>/` tree present but NO `.projects/<project_name>/` registration.

**Features required:**
- `<ws>/<project_name>/` directory exists with some files (the orphan tree).
- No `.metadata/.plugins/org.eclipse.core.resources/.projects/<project_name>/` entry.

**Drives tests:** broader-cleanup of the orphan tree; WARNING logged; substrate re-imports.

### F-WS-STALE-LOCK — Lock file without holder

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/workspaces/F-WS-STALE-LOCK/`
**Description:** Workspace with `.metadata/.lock` file present but no process holds an exclusive lock on it (simulating a previously-crashed GUI session).

**Features required:**
- `.metadata/.lock` file exists.
- No subprocess holding the lock (the fixture is just a file on disk).

**Drives tests:** `detect_workspace_lock() → False`; broader cleanup includes deleting this `.lock` as part of the WARNING enumeration.

### F-WS-CORRUPT-METADATA — Malformed plugin state

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/workspaces/F-WS-CORRUPT-METADATA/`
**Description:** Workspace with malformed `.metadata/.plugins/.../projects/<name>/` entries (truncated `.location`, missing required files, etc.).

**Features required:**
- `.projects/<project_name>/` directory exists.
- Contents are corrupted (e.g. `.location` is empty or contains garbage bytes).

**Drives tests:** broader-cleanup purges the corrupt entries; WARNING enumerates; build proceeds; other projects' state untouched.

### F-WS-LOCKED-GUI — Live-GUI simulation (Q5 sibling helper process)

**Status:** synthesized by test fixture, not user-provided
**Path:** N/A — generated at test runtime
**Description:** Simulates a live CubeIDE GUI holding an exclusive lock on `<ws>/.metadata/.lock`. Per Q5 ratified 2026-05-11, implemented as a **sibling helper process** so the mechanism is OS-agnostic (v1 Linux uses `fcntl.flock` in the helper; v2 Windows will use `msvcrt.locking` — substrate's lock probe is symmetric).

**Test fixture mechanism:**

```python
# tests/fixtures/cubeide/helpers/hold_lock.py — sibling helper
#!/usr/bin/env python3
import sys, time
from stm32_substrate.platform import acquire_exclusive_lock
lock_path = sys.argv[1]
with acquire_exclusive_lock(lock_path):
    sys.stdout.write("locked\n"); sys.stdout.flush()
    while sys.stdin.readline(): pass   # block until test closes the pipe
```

```python
# pytest fixture
@pytest.fixture
def locked_workspace(tmp_path):
    lock_path = tmp_path / ".metadata" / ".lock"
    lock_path.parent.mkdir(parents=True); lock_path.touch()
    helper = subprocess.Popen([sys.executable, "helpers/hold_lock.py", str(lock_path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert helper.stdout.readline() == "locked\n"
    yield tmp_path
    helper.stdin.close()
    helper.wait(timeout=5)
```

**Drives tests:** `detect_workspace_lock() → True`; `build()` raises `WorkspaceLockedError("close GUI")`; no cleanup attempted.

**TODO(v2-windows):** `acquire_exclusive_lock` wrapper gets a Windows implementation via `msvcrt.locking`; helper script + test fixture work unchanged.

---

## Build outcome logs (substrate-side)

Captured headless-build.sh stdout+stderr. Population strategy: mostly recorded from real runs against the F-PROJ-* projects, plus a few synthesized for substrate-side failure modes.

**`expected.json` field-comparison rules** (avoids brittle equality on host/tool-version-dependent values; mirrors cubemx + signing convention per RES-020):

| Field | Comparison |
|---|---|
| `success` | exact equality |
| `exit_code` | exact equality |
| `artifact_path`, `map_path` | predicate: `.exists()` when non-None; path equality after normalizing workspace prefix to a sentinel like `<WS>/` |
| `log_path` | predicate: `.exists()` (substrate writes a real file; exact path varies per run) |
| `console_output` | substring contains (e.g., `"Build Finished"` for success, `"error:"` for compile-fail) — not byte-equality |
| `build_time_s` / `duration_s` | predicate: `>= 0.0` (host-dependent) |

Test code provides a `compare_build_result(observed, expected_json)` helper that applies these rules.

### F-OUT-SUCCESS — Successful build

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/build-outcomes/F-OUT-SUCCESS/`
**Description:** Recorded log from a clean build via headless-build.sh.

**Features required:**
- Captured from a real F-PROJ-NUCLEO-L476RG-BLINKY build.
- Exit code = 0 (carried in a sidecar `.exit_code.txt` file; sidecar format matches cubeprogrammer / cubemx / signing per RES-020).
- Contains `"Build Finished"` substring.

**Drives tests:** parser-free outcome detection; `success=True`; substring assertions.

### F-OUT-FAIL-COMPILE — Compile-failure log

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/build-outcomes/F-OUT-FAIL-COMPILE/`
**Description:** Recorded log from a build against F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE.

**Features required:**
- Exit code != 0.
- `console_output` contains `"error:"` (or equivalent gcc error text).

**Drives tests:** `success=False` outcome; substring contains gcc error.

### F-OUT-FAIL-LINK — Link-failure log

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/build-outcomes/F-OUT-FAIL-LINK/`
**Description:** Recorded log from a build against F-PROJ-NUCLEO-L476RG-BROKEN-LINK.

**Features required:**
- Exit code != 0.
- `console_output` contains `"undefined reference"` or `"region 'FLASH' overflowed"` or similar ld message.

**Drives tests:** `success=False` outcome at link stage.

### F-OUT-WORKSPACE-LOCKED — CDT-refused log

**Status:** `[out]` — defensive-only; the primary `WorkspaceLockedError` contract is exercised by `F-WS-LOCK-HELD` (preflight lock detection before the subprocess runs). This fixture captures what CDT itself emits when the preflight is bypassed, kept as a sanity check in case `fcntl.flock` probe semantics drift across CubeIDE versions.
**Path:** `tests/fixtures/cubeide/build-outcomes/F-OUT-WORKSPACE-LOCKED/`
**Description:** Log from running headless-build.sh while CubeIDE GUI is open on the same workspace.

**Features required:**
- Captured from a real "GUI is open" run.
- Exit code != 0 with CDT's "workspace is in use" message.

**Drives tests:** `WorkspaceLockedError` raised (defensive path only).

### F-OUT-EDIT-OK-BUILD-FAIL — `.cproject` edit applied, build still fails

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/build-outcomes/F-OUT-EDIT-OK-BUILD-FAIL/`
**Description:** Combined fixture for the "protocol succeeds, build fails" commit/rollback rule (cubeide-api.md L107–114): substrate snapshots `.cproject`, applies a valid settings edit (e.g., `debug_level="-g3"`), writes successfully, then headless-build returns non-zero (compile or link error). Substrate **keeps** the `.cproject` change despite the build failure.

**Features required:**
- A `before.cproject` baseline + `after.cproject` post-edit pair (canonical form per `Post-edit pairs` section).
- A `headless.log` capture of the failing build (compile or link stage; exit code != 0).
- Sidecar `edit.json` documenting the edit kwargs (e.g., `{"debug_level": "-g3"}`).

**Drives tests:** `BuildResult(success=False)`, `settings_modification.rolled_back=False`, `.cproject` byte-equal to `after.cproject` (NOT to `before.cproject`). Locks down test-surface item 2 from cubeide-api.md L639 — the "keep changes on build failure" rule.

### F-OUT-HEADLESS-MISSING — Substrate-side script-missing

**Status:** `[out]`
**Path:** `tests/fixtures/cubeide/build-outcomes/F-OUT-HEADLESS-MISSING/`
**Description:** Synthesized fixture — represents the substrate finding `headless-build.sh` path invalid.

**Features required:**
- Empty log file (substrate raises before invoking).
- Sidecar marker file `cubeide_marker.txt` containing `"headless-script-missing"`.

**Drives tests:** `CubeIDEError(cubeide_marker="headless-script-missing")` raised before subprocess.

---

## Descriptors (substrate-authored, not user-provided)

These are tiny `stm32-project.jsonc` config files for resolution tests. Trivial to hand-author; no user supply needed.

| File | Drives |
|---|---|
| `descriptors/single-project-default.jsonc` | Baseline resolution. |
| `descriptors/multi-config-debug-default.jsonc` | `build.default_configuration: "Debug"`. |
| `descriptors/multi-config-release-default.jsonc` | `build.default_configuration: "Release"`. |
| `descriptors/modify-all-configs.jsonc` | `build.modify_all_configurations: true` flips Q6 default. |
| `descriptors/add-source-symlink-mode.jsonc` | `build.add_source_mode: "symlink"` for B-013. |
| `descriptors/workspace-explicit.jsonc` | `build.workspace` outside repo. |
| `descriptors/nested-project-cm7.jsonc` | `build.project_path: "./Project/CM7"`; multi-core. |

---

## Layer breakdown

### Unit-layer (T-001) — primary coverage

Drives:
- Editor round-trip for every (F-CP-*, F-CPE-*) pair the user supplies.
- Workspace-state helpers for every F-WS-* the user supplies.
- Outcome detection for every F-OUT-* the user supplies.
- Validation for `build()` kwargs (preset/debug_level exclusion, etc.) — no fixtures, code only.
- `find_project()` for F-PROJ-STM32H7S78-DK-MULTI-FOLDER.

### Smoke-layer (T-002) — real CLI, no hardware

Drives:
- `stm32cubeide -version` parses (no fixture needed; live invocation).
- `headless-build.sh` exists adjacent to `cubeide.path`.
- F-PROJ-NUCLEO-L476RG-BLINKY builds clean live → `BuildResult.success=True`.
- F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE builds with failure live → `BuildResult.success=False`.

### Hardware-layer (T-003) — attached NUCLEO

Drives:
- F-PROJ-NUCLEO-L476RG-BLINKY built on the user's host (re-runs smoke under hardware fixture).
- `find_project(tests/fixtures/projects)` discovery exercise.
- Settings-edit-then-build round-trip via cubeprogrammer cross-module compound.

No `@pytest.mark.hardware_destructive` markers in this module — snapshot mechanism keeps settings edits non-destructive.

### Eval-layer (T-007) — placeholder

Per M-014, T3 deferred. When B-021 lands, eval scenarios consume `BuildResult.console_output` raw — no parser fixtures needed module-side.

---

## Cross-tool sharing

| Shared fixture | Owners |
|---|---|
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/` | cubeide (build owner), cubeprogrammer (flash), debug (gdb), vcp (output) |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/` | cubeide (build), cubeprogrammer (flash), vcp (round-trip), compound |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-FAULTING/` | cubeide (build), cubeprogrammer (-hf), debug (gdb), compound |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE/` | cubeide (build-failure path) |
| `tests/fixtures/projects/STM32CubeH7RS/Projects/STM32H7S78-DK/Applications/USB_Device/MSC_Standalone/` | cubeide (`find_project` ambiguity — F-PROJ-STM32H7S78-DK-MULTI-FOLDER) |
| `tests/fixtures/projects/STM32CubeH7/Projects/STM32H747I-DISCO/Applications/USB_Host/MSC_Standalone/STM32CubeIDE/` (F-PROJ-DISCO-H747XI-DUAL-CORE) | cubeide (nested-project discovery), debug (multi-core attach) |
| `tests/fixtures/devicedb/` | cubemx (owner), cubeide (FPU detection for preset="fast"), cubeprogrammer |
| `tests/fixtures/configs/` | substrate-wide schema validation |

---

## Build sequence

Per T-005:

1. **API-surface phase (now):** spec only. No fixtures built.
2. **Code phase (incremental as user supplies):**
   - **User** generates F-PROJ-* projects via CubeMX/CubeIDE on their host. Builds artifacts (`.elf` / `.map` / `.bin`) checked in. LF line endings on text files.
   - **User** copies `.cproject` files into the F-CP-* directories, anonymizing names/paths.
   - **User** hand-authors F-CPE-* post-edit files per the transformation recipes.
   - **User** captures workspace snapshots into F-WS-* directories.
   - **User** records F-OUT-* build logs (sidecar `.exit_code.txt` file for each).
   - **Claude/substrate** writes descriptor JSONCs (tiny, substrate-authored).
   - **Claude/substrate** writes the sibling-helper-process script + the conftest harness that parametrizes from supplied artifacts.
   - Tests run continuously against whatever's supplied; missing fixtures cleanly skip.
3. **Hardware phase:** F-PROJ-* projects flashed; cross-module compound tests exercised.
4. **Eval phase:** added under `tests/fixtures/eval/` when T3 begins.

---

## Round-1 review answers (2026-05-11)

| # | Topic | Resolution |
|---|---|---|
| Q1 | Reference-project source | **User provides real projects** during code phase. Substrate spec enumerates requirements + features; user supplies artifacts. Multi-artifact per requirement supported. |
| Q2 | `.cproject` sample collection | **User provides real `.cproject` files** (anonymized) per the F-CP-* requirements above. Multi-artifact per requirement supported. |
| Q3 | `cproject-edits/` regeneration | **Option (b): user hand-authors expected post-edit `.cproject` files** per the explicit transformation recipes above. Stronger test than substrate-as-its-own-oracle. |
| Q4 | Workspace-state fixture realism | **Option (b): real workspaces from previous CubeIDE actions**, captured by the user. Consistent with Q1/Q2. |
| Q5 | `locked-by-gui/` fixture mechanism | **Option (b) sibling helper process** (with fallback to in-process flock if subprocess machinery proves too complex). Chosen for OS-portability (per M-020 v1 Linux-only with v2 Windows-portable architecture) since `fcntl.flock` is Unix-only. |

**Cross-cutting principles ratified this round:**
- **M-020 (v1 Linux-only, v2 must support Windows)** — surfaced by Q5; LF-only fixture-authoring rule; subprocess + pathlib + lock-wrapper discipline for portability.

---

## State

- **Round-1 review answers integrated 2026-05-11.** All 5 fixture-spec questions resolved.
- **Cubeide module fully signed off** — `cubeide-api.md` + this fixture-spec both ratified.
- **Fixture artifacts** supplied incrementally by user during code phase; tests gracefully skip on `[out]` items until artifacts arrive.
- **Next module per M-015 step 2:** `cubemx` (MX-* + async-completion per SB-004).
