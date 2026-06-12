# Test fixtures — `cubemx` module

**Last updated:** 2026-05-12 (catalog further reduced for cubemx scope cut per P-037; Q5 resolved per RES-017 — multi-IOC catalog kept for timing-parameter validation)
**Status:** **Signed off** (RES-017). Paired with `cubemx-api.md` (round-1 + thin-wrapper redesign integrated 2026-05-11 + 2026-05-12 scope cut applied) per T-005. Module #6 of 6 — final per M-015 step 2. **Module fully signed off 2026-05-12** — all fixture-spec questions resolved.

**Scope:** test inputs the cubemx module's tests need across unit / smoke layers. (Hardware-layer is empty — CubeMX is host-side.) **Catalog further collapsed** vs the 2026-05-11 draft: with `new_project()` / `saveas_and_modify()` / `retarget_mcu()` / `find_board()` / `find_mcu()` all dropped 2026-05-12, the script-text goldens reduce to one and there are no DB-search or retarget tests left. **But:** the multi-IOC F-IOC-* catalog stays — multiple IOCs serve as smoke-layer inputs to validate the running-loop's timing parameters (`long_call_s`, extension cadence, `liveness_threshold_s`) against real-CubeMX generation-time variance across different projects.

**Build status:** spec only. Fixture artifacts supplied by the user (real IOCs, real captures) or hand-authored by Claude (descriptors) during the code phase per T-005. Tests gracefully skip on `[out]` items until artifacts arrive.

---

## How this fixture catalog works

(Same model as cubeide / debug / vcp / signing fixture-specs ratified per RES-011 / RES-013 / RES-014 / RES-015.)

Each fixture requirement has: **ID** · **Status** (`[in]`/`[out]`) · **Description** · **Features required** · **Drop path** · **Multi-artifact** (yes/no) · **Drives tests**.

Tests parametrize over the artifacts under the requirement's drop path; empty dir → `pytest.skip` with the path to populate.

**v1 fixture-authoring rules** (per M-020):

- LF line endings on all text files (IOCs, scripts, log captures, descriptors, expected-JSON sidecars).
- Anonymized paths where artifacts come from real user projects.
- IOCs are key=value plain text; substrate doesn't parse them — preserved as opaque blobs.

---

## Catalog at a glance (post-2026-05-12 scope cut)

| Group | Count (planning) | Path prefix | Used by |
|---|---|---|---|
| Input IOC files (F-IOC-*) | open-ended per Q5 (user supplies multiple) | `iocs/F-IOC-*/` | smoke (real-CubeMX generate per supplied IOC) |
| Script-text goldens (F-SCRIPT-*) | 3 | `scripts/F-SCRIPT-*/` | unit (inline string construction). Per RES-020: GENERATE + GENERATE-SPACED-PATH + GENERATE-QUOTED-REJECT (loud-error fixture). |
| CubeMX subprocess captures (F-OUT-*) | 2 | `outcomes/F-OUT-*/` | unit (substrate captures the subprocess stdout+stderr; outcome detection via exit code only) |
| Reference projects (F-PROJ-*) — shared | 1 | `tests/fixtures/projects/F-PROJ-*/` | smoke (real CubeMX generate against this IOC) |
| Descriptors (substrate-authored) | 1 | `descriptors/` | unit (cubemx config-resolution) |

**Dropped 2026-05-12 (scope cut):**
- `F-SCRIPT-NEW-BOARD` — `new_project()` dropped.
- `F-SCRIPT-SAVEAS` — `saveas_and_modify()` dropped.
- `F-SCRIPT-RETARGET` — `retarget_mcu()` dropped.
- Retarget sibling-write byte-compare tests against F-IOC-* baselines — `retarget_mcu()` dropped.
- `find_board()` / `find_mcu()` smoke tests against real `db/` — helpers dropped.
- `descriptors/cubemx-mcu-flow.jsonc` — already dropped 2026-05-11; stays dropped.
- `firmware.board` consumption from descriptor — moot here (was for `new_project()`; vcp still proposes it as round-2 candidate).

**Dropped earlier (2026-05-11 thin-wrapper redesign):**
- **F-IOC-MALFORMED** — no IOC parser to test parse-error paths.
- **F-IOC-VERSION-TOO-HIGH** — no `cubemx_failure_reason()` classifier.
- **F-LOG-*** — no log-content tailing.
- **F-MON-*** — no separate monitor algorithm.
- **F-DB-MINI** — no DeviceDB.
- **F-OUT-PACKAGE-MISSING / F-OUT-VERSION-TOO-HIGH / F-OUT-SCRIPT-REJECTED** — substrate doesn't classify.
- **F-IOC-NUCLEO-L476RG-BLINKY-MODIFIED expected-diff sidecar** — no `diff_iocs()` method.

---

## Input IOC files (F-IOC-*) — Q5 open-ended

Per Q5 ratified, user supplies **multiple** real `.ioc` files (not just one canonical NUCLEO-L476RG-BLINKY). Per Q1(a) ratified, all are real-anonymized. Substrate doesn't parse them; tests treat them as opaque byte blobs.

**With retarget gone, F-IOC-* survives purely as smoke-layer real-CubeMX generate inputs** — no unit-layer byte-compare needed. **Per Q5 resolved (RES-017):** multiple IOCs are the deliberate point — they exercise CubeMX across varied real-world generation times, which validates the running-loop's timing parameters (initial budget, extension delta, max extensions, liveness threshold) against actual variance. One IOC is not enough to know whether the defaults are right.

### F-IOC-* — user-supplied real IOCs (anonymized)

**Status:** `[out]` (user supplies during code phase).
**Path:** `tests/fixtures/cubemx/iocs/F-IOC-<name>/<name>.ioc` (one subdirectory per IOC).
**Description:** Real `.ioc` files from the user's projects. Substrate treats them as opaque text — no parsing.

**Features required:**
- Valid IOC syntax (CubeMX-parseable).
- LF line endings throughout.
- Anonymized paths / IDs where applicable.

**Multi-artifact:** **yes — Q5 open-ended.** User supplies as many as they want; tests parametrize over discovered subdirectories. Naming convention: descriptive subdir name (e.g., `F-IOC-NUCLEO-L476RG-BLINKY`, `F-IOC-H753ZI-PERIPHERALS`, `F-IOC-N6570-DK-FSBL`).

**Drives tests:**
- **Smoke-layer real CubeMX generate** — `CubeMX.generate(<this-IOC>, output_path=<tmp>)` against the user's installed CubeMX. **Tests parametrize across ALL supplied F-IOC-* entries** (per Q5 RES-017) to validate the running-loop's timing parameters against real-world generation-time variance. Per-IOC pass criteria: `.cproject` appears in `<tmp>` within the configured budget + extensions; record `duration_s` + `extensions_used` per IOC for trend tracking.

---

## Script-text goldens (F-SCRIPT-*)

The one fixture is the expected script text — what `generate()`'s **inline string construction** (per Q2(b) ratified; no typed `Script` class) emits for canonical kwargs. Unit tests do byte-compare against this golden.

| Fixture | Method call | Drives |
|---|---|---|
| `F-SCRIPT-GENERATE/expected.txt` | `CubeMX.generate(ioc, output_path=path, project_name=name, toolchain="STM32CubeIDE")` | Script construction for MX-001 / CP-008 |
| `F-SCRIPT-GENERATE-SPACED-PATH/expected.txt` | `CubeMX.generate(ioc=Path("/tmp/has space/in.ioc"), output_path=Path("/tmp/has space/out"), ...)` | `_quote()` happy path — values containing spaces wrap in `"..."` |
| `F-SCRIPT-GENERATE-QUOTED-REJECT/expected.exc.txt` | `CubeMX.generate(ioc=Path('/tmp/has"quote/in.ioc'), ...)` | `_quote()` loud-error path — values with `"` or `\` raise `ValueError` before any subprocess runs. Fixture stores expected exception text rather than a script. |

**Status:** `[out]`. Substrate-authored — Claude writes the expected text (or expected exception text for loud-error fixtures) alongside the inline-construction implementation.

**Multi-artifact:** three canonical cases per RES-020 — one happy-path script (`F-SCRIPT-GENERATE`), one happy-path variant covering spaced paths (`F-SCRIPT-GENERATE-SPACED-PATH`), and one substrate-side loud-error fixture covering forbidden chars (`F-SCRIPT-GENERATE-QUOTED-REJECT`). The first two byte-compare against `expected.txt`; the third asserts a `ValueError` is raised pre-subprocess and substring-matches `expected.exc.txt`. Add more variants if a new verb-shape lands in v1+.

**Drives tests:** inline script construction → byte-compare for the two happy fixtures; pre-subprocess `ValueError` raise + substring-match for the reject fixture. Catches quoting / line-order / hardcoded `exit_mx` regressions plus the substrate-side path-character refusal rule.

**Dropped per 2026-05-12 scope cut:**
- `F-SCRIPT-NEW-BOARD/expected.txt` — `new_project()` dropped.
- `F-SCRIPT-SAVEAS/expected.txt` — `saveas_and_modify()` dropped.
- `F-SCRIPT-RETARGET/expected.txt` — `retarget_mcu()` dropped.
- `F-SCRIPT-NEW-MCU/expected.txt` — already deferred per 2026-05-11 TODO(v1+); now permanently dropped along with `new_project()`.

---

## CubeMX subprocess captures (F-OUT-*) — substrate-captures-doesn't-interpret

Captured `STM32CubeMX -q <script>` stdout/stderr/exit_code triples + `expected.json` for the expected `CubeMXResult`. Per substrate-captures rule, substrate doesn't parse content — it just records the exit code and dumps stdout+stderr to a log file. Two fixtures cover both outcomes.

**`expected.json` field-comparison rules** (avoids brittle equality on host/tool-version-dependent values):

| Field | Comparison |
|---|---|
| `success`, `timed_out`, `extensions_used`, `terminated_after_marker` | exact equality |
| `exit_code` | exact equality when not None; if expected is None, observed must also be None |
| `output_dir` | path equality after normalizing tempdir prefix to a sentinel like `<TMPDIR>/` |
| `log_path`, `cubemx_log_path` | predicate: `.exists()` (substrate writes a real file; exact path varies per run) |
| `script_text` | exact equality on lines, but absolute path tokens normalized to fixture-relative |
| `duration_s` | predicate: `duration_s >= 0.0` (host-dependent) |

Test code provides a `compare_cubemx_result(observed, expected_json)` helper that applies these rules.

### F-OUT-SUCCESS

**Status:** `[out]` (user-captured on the host).
**Path:** `tests/fixtures/cubemx/outcomes/F-OUT-SUCCESS/`
**Description:** Real successful CubeMX `-q` invocation against an F-IOC-* baseline; captures the full transcript ending with `OK` after `project generate` and `exit_mx`.

**Features required:**
- `stdout.txt` (verbatim CubeMX transcript).
- `stderr.txt` (usually empty).
- `exit_code.txt` (typically `0`).
- `expected.json` — `CubeMXResult` with `success=True`, `timed_out=False`, expected `output_dir` (relative to fixture root).

**Multi-artifact:** no — one capture (only one verb left). Multi-artifact returns if a second verb lands.

**Drives tests:** subprocess capture → log_path written; CubeMXResult populated with expected fields.

### F-OUT-CLI-FAILED

**Status:** `[out]` (user-captured by deliberately bad invocation).
**Path:** `tests/fixtures/cubemx/outcomes/F-OUT-CLI-FAILED/`
**Description:** CubeMX `-q` invocation that exits non-zero. Easy to force: pass a script with syntax-malformed lines, or `config load <nonexistent.ioc>`. **Substrate doesn't classify the failure reason** — exit non-zero + no marker file = `CubeMXResult(success=False, log_path=..., cubemx_log_path=...)`.

**Features required:**
- `stdout.txt` / `stderr.txt` — captured.
- `exit_code.txt` — non-zero.
- `expected.json` — `CubeMXResult` with `success=False`, `timed_out=False` (subprocess exited; not timeout), `cubemx_log_path` populated.

**Multi-artifact:** yes — variants per failure cause (bad IOC path, script syntax error). All collapse to the same outcome from substrate's POV (success=False + log path).

**Drives tests:** failure path → substrate captures the log + reports the path; doesn't try to classify.

---

## Reference projects (F-PROJ-*) — shared cross-module

### F-PROJ-NUCLEO-L476RG-BLINKY (shared with cubeide)

**Status:** **shared from cubeide module per RES-011** (user supplies during code phase).
**Path:** `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/`
**Description:** Real CubeMX-generated NUCLEO-L476RG blinky project. Already in cubeide's fixture-spec; cubemx tests reuse it.

**Cubemx-specific drives:**
- The project's `.ioc` file is the canonical input for smoke-level `CubeMX.generate()` against real CubeMX → regenerates the project, validates the running-loop algorithm against real timing.

---

## Descriptors (substrate-authored)

Hand-authored `stm32-project.jsonc` exemplars for cubemx config-resolution tests.

| File | Drives |
|---|---|
| `descriptors/cubemx-baseline.jsonc` | Baseline with `cubemx.ioc_path`, `cubemx.output_path`, `cubemx.project_name`, `cubemx.toolchain="STM32CubeIDE"`. Used for `generate()` resolution tests. |

**Dropped from earlier drafts:**
- `descriptors/cubemx-mcu-flow.jsonc` — already dropped 2026-05-11.
- `descriptors/cubemx-missing.jsonc` — missing-IOC test uses an in-test `dict` fixture.
- `firmware.board` field in baseline — no consumer here.

**Status:** `[out]` — Claude writes during code phase.

---

## Layer breakdown

### Unit-layer (T-001) — primary coverage

- **Inline script construction** vs `F-SCRIPT-GENERATE/expected.txt` (byte-compare). Tests cover quoting (toolchain with space), path resolution (absolute), hardcoded `EXIT_COMMAND = "exit_mx"` (Q14).
- **Running-loop branches** with mocked `subprocess.Popen` + temp filesystem:
  - **Marker appears before subprocess exits** → success; subprocess terminated.
  - **Subprocess exits + marker present** → success.
  - **Subprocess exits + marker absent + grace period** → success if marker appears within grace; failure otherwise.
  - **Deadline expires + log mtime advanced within liveness threshold** → extension fires; loop continues.
  - **Deadline expires + log inactive** → failure with `timed_out=True`.
  - **Max extensions used + log still active** → failure with `timed_out=True` after the cap.
  - **Heartbeat callback** — `on_progress` emits one event per poll-tick with elapsed/deadline/extensions_used.
- **Launcher resolver** with mocked filesystem — explicit `cubemx_executable` wins; PATH lookup is the fallback; both unset → `CubeMXLauncherError`. (No jar-fallback path in v1 per Q3(a).)
- **`ioc-missing` raise** — `ioc_path` doesn't exist (or wrong suffix) → `CubeMXError(cubemx_marker="ioc-missing")`.
- **Output-path pass-through** — `output_path` pre-existence is NOT pre-checked; substrate proceeds and CubeMX silently overwrites (per Q8(c) + #10 ratified). Tests verify no `CubeMXError` raised.
- **Toolchain Literal narrowing** — `toolchain="EWARM"` → ValueError (only `"STM32CubeIDE"` accepted in v1 per follow-up #2).
- **Subprocess captures** — exit_code 0 / non-zero → `CubeMXResult` populated correctly (success flag, log_path written, cubemx_log_path populated only on failure).

Subprocess mocked throughout; tests don't invoke real CubeMX at unit layer.

**Removed per 2026-05-12 scope cut:**
- `find_board()` / `find_mcu()` pattern-match tests (helpers dropped).
- `new_project()` board-missing / board-not-in-db / descriptor-fallback tests (method dropped).
- `retarget_mcu()` mcu-not-in-db / sibling-write / regex-find-old-MCU tests (method dropped).

**Removed earlier per thin-wrapper redesign 2026-05-11:**
- IOC parser tests (no parser).
- DeviceDB tests (no DeviceDB).
- IOC diff tests (no `diff_iocs()`).
- `failure_reason` classification tests (no classifier).
- Async-completion monitor branch tests as separate F-MON-* fixtures (folded into running-loop tests inline).
- Output-exists refusal test (Q8(c) pass-through).
- `McuRetarget` audit test (dropped per #3).

### Smoke-layer (T-002) — real CubeMX, no hardware

- `STM32CubeMX -q <trivial-script-with-only-exit_mx>` returns within 5 sec (launches at all).
- `CubeMX.generate(F-PROJ-NUCLEO-L476RG-BLINKY.ioc, output_path=<tempdir>)` completes within 90 sec; `<tempdir>/.cproject` exists; `CubeMXResult.success=True`.
- **Per Q5 RES-017: iterate `CubeMX.generate()` across all supplied F-IOC-* entries.** Parametrize the smoke test over the catalog; each IOC must succeed within budget + extensions. Record `duration_s` and `extensions_used` per IOC; these data points drive any future re-tuning of `long_call_s` / `long_call_extension_s` / `liveness_threshold_s` defaults.

**Removed per 2026-05-12 scope cut:**
- `find_board()` / `find_mcu()` real-`db/` smoke tests.
- `retarget_mcu()` real-CubeMX sibling-write smoke test.
- `new_project()` real-CubeMX smoke test.

### Hardware-layer (T-003) — n/a

CubeMX is host-side; no hardware required. Compound pipelines (CP-009..CP-012) that chain through CubeMX are exercised by `compound/` fixtures + hardware tests, not by this module.

### Eval-layer (T-007) — placeholder

- **F-EVAL-MX-005-PIN-COMPARE** — when MX-005 (T3) reactivates per M-014, the eval-layer fixture set will include user-supplied schematic-note artifacts and substrate-supplied IOC pin assignments for Claude to compare. Not authored in v1; placeholder only.
- **F-EVAL-MX-004-CLAUDE-DIFF** — MR-5: MX-004 is a Claude-side prompt (no substrate method). Eval-layer tests verify Claude correctly invokes the right slash-command-or-tool when asked to "diff these two IOCs". Not authored in v1; placeholder.

---

## Cross-tool sharing

| Shared fixture | Owners |
|---|---|
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/` | cubeide (build owner), cubemx (generate-and-roundtrip target), cubeprogrammer (flash target via compound), debug (DBG-* / DIAG-* via compound) |
| `tests/fixtures/projects/X-CUBE-MEMS1/Projects/NUCLEO-F401RE/Applications/IKS02A1/DataLogFusion/` (F-PROJ-NUCLEO-F401RE-PERIPHERALS, debug-owned) | debug primary; cubemx may reuse if a need surfaces |

Cubemx-specific catalogs (F-IOC-* / F-SCRIPT-* / F-OUT-*) are not shared — they live under `tests/fixtures/cubemx/`.

---

## Build sequence

Per T-005:

1. **API-surface phase (now):** spec only.
2. **Code phase (incremental as user supplies + Claude writes):**
   - **User** supplies F-IOC-* — multiple real anonymized IOCs (per Q1(a) + Q5 ratified). Open-ended catalog.
   - **User** captures F-OUT-SUCCESS + F-OUT-CLI-FAILED by running CubeMX with success/fail params (per Q2(b) ratified).
   - **Claude** writes F-SCRIPT-GENERATE alongside the inline-construction implementation.
   - **Claude** writes the baseline descriptor.
   - Tests run continuously against whatever's supplied; missing fixtures cleanly skip.
3. **Smoke phase:** depends on F-PROJ-NUCLEO-L476RG-BLINKY (shared from cubeide) + the user's installed CubeMX. Runs in CI when both are present.

---

## Round-1 review questions

Per the inline-explanation discipline.

---

### Q1. F-IOC-* sourcing — real anonymized vs substrate-authored synthetic vs both

**Resolved (2026-05-11):** **(a) real-anonymized.** All F-IOC-* fixtures are user-supplied real IOCs.

**Note (post thin-wrapper redesign 2026-05-11):** F-IOC-MALFORMED dropped (no parser). Surviving F-IOC-* are real-anonymized happy-path IOCs.

**Note (post 2026-05-12 scope cut):** unchanged. F-IOC-* still in catalog as smoke-generate inputs.

---
ans: (a)

### Q2. F-OUT-* / F-LOG-* capture strategy — all-recorded vs mixed vs synthesized

**Resolved (2026-05-11):** **(b) all-recorded.**

**Note (post thin-wrapper redesign 2026-05-11):** F-LOG-* dropped; F-OUT-* reduced to 2 outcomes.

**Note (post 2026-05-12 scope cut):** F-OUT-SUCCESS multi-artifact collapses to single (one verb). F-OUT-CLI-FAILED stays multi-artifact for variant failure causes.

---
ans: (b)

### Q3. F-DB-MINI shape

**Moot since 2026-05-11.** DeviceDB dropped; subsequently `find_board()` / `find_mcu()` helpers also dropped 2026-05-12. No DB fixture needed at any layer.

---
ans: real db from installed STM32CubeMX (now moot — find_* helpers also dropped 2026-05-12)

### Q4. F-MON-* completion-monitor scenarios

**Moot since 2026-05-11.** Monitor algorithm folded into running-loop in `runner.py`; tested inline with mocked subprocess + temp filesystem.

---
ans: (b) (now moot)

### Q5. Reference project — reuse cubeide's `F-PROJ-NUCLEO-L476RG-BLINKY` vs author cubemx-specific IOC(s)

**Resolved (2026-05-11):** "author will provide many ioc files, not just NUCLEO-L476RG-BLINKY".

**Resolved (2026-05-12 per RES-017):** **(A) keep the multi-IOC catalog + (B) iterate smoke generate across all supplied IOCs.** User direction: *"we will use multiple iocs to test the timing parameters are good or not."* The multi-IOC catalog is the deliberate vehicle for validating the running-loop's timing parameters (`long_call_s`, `long_call_extension_s`, `long_call_max_extensions`, `liveness_threshold_s`) against real-CubeMX generation-time variance. One IOC isn't enough; the spread across multiple real-world IOCs is what tells us whether the defaults are right.

**Implications:**
- F-IOC-* catalog stays open-ended; user supplies multiple real anonymized IOCs during code phase.
- Smoke layer parametrizes `CubeMX.generate()` over the catalog; per-IOC pass criterion is success within budget + extensions.
- Per-IOC `duration_s` + `extensions_used` recorded for trend analysis; data drives any future tuning of the timing knob defaults.
- Scope (cross-family vs single-family): user supplies whatever IOCs are representative of their actual workload. Cross-family is preferred (broader CubeMX `db/` coverage + wider timing spread), but not enforced.

---
ans: keep multi-IOC catalog + iterate smoke across all supplied IOCs to validate timing parameters (RES-017 2026-05-12).

---

## Round-2 candidates (deferred)

- **F-EVAL-MX-004-CLAUDE-DIFF** — MX-004 is Claude-side now; eval-layer test infrastructure when SDK harness lands.
- **F-EVAL-MX-005-*** — MX-005 (T3) reactivation; user-supplied schematic notes.
- **Per-IOC timing baseline records.** Once the smoke iteration runs in CI for a while, capture per-IOC baseline `duration_s` so regressions surface (e.g., a CubeMX upgrade that doubles generate time).
- **F-SCRIPT-NEW-BOARD / SAVEAS / RETARGET** — reinstate if their owning prompts return to `[in]`.
- **F-PROJ-* per-toolchain variants** — when toolchain Literal widens beyond `STM32CubeIDE` (currently `Literal["STM32CubeIDE"]` per follow-up #2).
- **Q5 scope clarification** — (A) cross-family vs single-family + (B) coverage policy + (C) drop multi-IOC entirely. Settle before code phase.

---

## State

- **Cubemx fixture-spec — signed off 2026-05-12 (RES-017).** Catalog rewritten 2026-05-11 for thin-wrapper redesign; reduced 2026-05-12 for scope cut (P-037); Q5 resolved 2026-05-12 (RES-017) — multi-IOC catalog kept for timing-parameter validation.
  - Surviving groups: **F-IOC-*** (open-ended, user-supplied; smoke iteration validates timing), F-SCRIPT-GENERATE (1 golden), F-OUT-* (2 outcomes — 1 success / 1 failure), F-PROJ-NUCLEO-L476RG-BLINKY (shared), descriptors (1 baseline).
- **Paired:** `cubemx-api.md` round-1 + thin-wrapper redesign + 2026-05-12 scope cut integrated and signed off (RES-017).
- **Module #6 of 6 fully signed off.** Cross-module decisions MR-1/MR-2/MR-3 still open but live in their owning modules (cubeide / vcp / cubeprogrammer); resolution before code phase.
- **M-015 step 2 complete** (all six modules signed off). Step 3 (cross-tool compounds CP-*) begins next. Step 4 (consistency sweep) follows.
