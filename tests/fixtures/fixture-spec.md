# Test fixtures — top-level index

Aggregated catalog of `tests/fixtures/` across all 6 per-tool modules. This file is the entry point; per-tool detail lives in `tests/fixtures/<tool>/fixture-spec.md`.

**Status:** Pass 1 aggregation (per-tool only). Per RES-022, cross-tool compounds (CP-*) deferred to Pass 2 — `tests/fixtures/compound/fixture-spec.md` and its rows in tables below are placeholders, populated when Pass 2 lands.

---

## Per-tool catalogs

| Module | Spec | Layout | Domains owned |
|---|---|---|---|
| `cubeprogrammer` | [`cubeprogrammer/fixture-spec.md`](cubeprogrammer/fixture-spec.md) | `banners/` / `option-bytes/` / `hex-dumps/` / `hardfaults/` / `probe-lists/` / `swv/` / `errors/` / `external-loaders/` / `descriptors/` | (uses `F-PROJ`, no module-only domain — banner + dump fixtures live under named subdirs) |
| `cubeide` | [`cubeide/fixture-spec.md`](cubeide/fixture-spec.md) | reference projects + `cproject-baselines/` + `cproject-post-edit/` + `workspaces/` + `headless-outputs/` | `F-CP`, `F-CPE`, `F-WS`, `F-OUT` (cubeide-scoped) |
| `cubemx` | [`cubemx/fixture-spec.md`](cubemx/fixture-spec.md) | `iocs/` + `scripts/` + `outputs/` | `F-IOC`, `F-SCRIPT`, `F-OUT`, `F-DB`, `F-EVAL` (last two co-owned — see footnote) |
| `debug` | [`debug/fixture-spec.md`](debug/fixture-spec.md) | `mi-records/` + `svd-decodes/` + `svd-samples/` + `session-handles/` + `hardfaults/` + `recipes/` + `transcripts/` + `eval/` | `F-MI`, `F-SVD`, `F-SVDX`, `F-SH`, `F-DIAG`, `F-HF`, `F-RC`, `F-TRX`, `F-EVAL` (NB: `F-SREG` retired per RES-026 — CLI session registry never shipped) |
| `vcp` | [`vcp/fixture-spec.md`](vcp/fixture-spec.md) | `list-ports/` + `captures/` + `terminators/` + `transcripts/` | `F-LP`, `F-CAP`, `F-TERMINATOR`, `F-TRX` (co-owned with debug) |
| `signing` | [`signing/fixture-spec.md`](signing/fixture-spec.md) | `inputs/` + `signed-samples/` + `outputs/` | `F-IN`, `F-SS`, `F-OUT` |
| **compound** | *(Pass 2)* `compound/fixture-spec.md` | *(TBD when CP-* lands)* | *(TBD)* |

**Domain-scope footnote.** `F-OUT` is a common domain used by **three modules** (cubeide / cubemx / signing) — each scopes its own `F-OUT-*` fixtures within its own subdirectory. There is no global `F-OUT` namespace; the prefix identifies *category* (tool subprocess output capture), not ownership. `F-EVAL` is co-owned by debug and cubemx (Claude-side eval inputs). `F-TRX` is co-owned by debug (test-recipe transcripts) and vcp (send/read state-machine transcripts).

**Aggregate counts (Pass 1):** 6 per-tool spec files, ~2,876 lines, 10 shared reference projects (`F-PROJ-*`), **21 module-scoped fixture domains** (excluding `F-PROJ`; `F-SREG` was the 22nd before being retired per RES-026 alongside the CLI session registry).

---

## Shared reference projects (`F-PROJ-<board>-<role>`)

Single canonical naming per RES-019 — pattern `F-PROJ-<board>-<role>`. All shared reference projects live under `tests/fixtures/projects/<id>/` and are consumed by multiple modules.

| ID | Board | Role | Primary consumer(s) | Hardware-marker board |
|---|---|---|---|---|
| `F-PROJ-NUCLEO-L476RG-BLINKY` | NUCLEO-L476RG | Clean-building blinky | cubeide / cubeprogrammer / debug / vcp | L476RG |
| `F-PROJ-NUCLEO-L476RG-VCP-ECHO` | NUCLEO-L476RG | UART echo firmware | vcp / cubeide | L476RG |
| `F-PROJ-NUCLEO-L476RG-FAULTING` | NUCLEO-L476RG | Deliberate HardFault | debug / cubeprogrammer | L476RG |
| `F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE` | NUCLEO-L476RG | Missing-semicolon source | cubeide | (none — build-fail; unit/smoke layer) |
| `F-PROJ-NUCLEO-L476RG-BROKEN-LINK` | NUCLEO-L476RG | Undefined-symbol link error | cubeide | (none — link-fail; unit/smoke layer) |
| `F-PROJ-STM32H7S78-DK-MULTI-FOLDER` | STM32H7S78-DK | Nested CubeIDE workspace (Appli + Boot sub-projects) | cubeide | STM32H7S78-DK (build optional; discovery is unit-layer) |
| `F-PROJ-NUCLEO-F401RE-PERIPHERALS` | NUCLEO-F401RE + IKS02A1 | Peripheral-exerciser firmware (X-CUBE-MEMS1 DataLogFusion) | debug (DIAG recipes) | NUCLEO-F401RE |
| `F-PROJ-DISCO-H747XI-FPU` | STM32H747I-DISCO | Hardware-FPU exerciser (FPU_Fractal; CM7) | cubeide (preset-fast FPU) | STM32H747I-DISCO |
| `F-PROJ-DISCO-H747XI-DUAL-CORE` | STM32H747I-DISCO | Multi-core nested project (M7 + M4; USB_Host/MSC_Standalone) | cubeide / debug | STM32H747I-DISCO |
| `F-PROJ-NUCLEO-N657X0-Q-SIGNED` | NUCLEO-N657X0-Q | Signed binary for trusted-flash | cubeprogrammer / signing | N657X0-Q |

**Path on disk:** `tests/fixtures/projects/F-PROJ-<board>-<role>/` (uppercase, per RES-019 directory rename).

**Test-harness convention:** test code parametrizes over board-per-role via `pytest.mark.parametrize` — a single test may run multiple boards for the same role if hardware is present. Hardware-marked tests skip cleanly when the required board is absent.

---

## Fixture-domain registry

The 2-4 character domain code registry — what each `F-<DOMAIN>-*` prefix means and which module owns it — lives in [`../../api-conventions.md` § "Test fixture naming convention"](../../api-conventions.md#test-fixture-naming-convention).

Adding a new fixture: pick an existing domain if applicable; otherwise propose a new 2-4 char code in the owning module's `fixture-spec.md` and add a row to that registry.

---

## Fixture authoring conventions (cross-cutting)

Conventions consistently applied across all six per-tool fixture-specs. Read each rule once here; per-tool specs reference back instead of restating.

| Convention | Rule | Source |
|---|---|---|
| **`[in]` / `[out]` status checklist** | Every fixture entry carries a status: `[in]` means the artifact is supplied and dependent tests run; `[out]` means it's pending and dependent tests skip cleanly with a populate-path hint. Tests parametrize over supplied artifacts; empty fixture dirs cleanly skip. | RES-011 (cubeide) — adopted module-wide |
| **Sidecar error fixtures** | Error captures are stored as *pairs*: `<name>.stderr` (verbatim vendor stderr, no header comment, no preamble) + `<name>.exit_code.txt` (integer exit code only). No header lines that could be mis-parsed; binary-byte fidelity preserved. | RES-020 cubeprogrammer #c — applied to cubeide / cubemx / signing |
| **`expected.json` field-comparison rules** | Outcome fixtures pair the capture with an `expected.json` describing the expected result-type fields, plus per-field comparison rules (exact equality / predicate / normalized after sentinel-path substitution). Avoids brittle byte-equality on host- or tool-version-dependent values like log_path / duration_s. Each module that uses outcome fixtures (cubeide / cubemx / signing) documents its own rule table. | RES-020 cubemx #c + signing #c + cubeide expected.json rules section |
| **LF-only line endings** | All text fixtures (`.cproject`, scripts, captured stderr, expected.json) author on Linux with LF endings only. Lets byte-compare tests survive when v2 Windows lands. | M-020 (v1 Linux-only / v2 Windows) |
| **F-PROJ on-disk directory naming** | Reference-project directories use the long form `tests/fixtures/projects/F-PROJ-<board>-<role>/` (uppercase). RES-019 renamed 10 short-form IDs; the on-disk rename happens during code phase. | RES-019 |
| **Hardware markers** | `@pytest.mark.hardware` for read-only ops; `@pytest.mark.hardware_destructive` (or per-module equivalent) for erase / OB-write / similar ops requiring explicit opt-in. | T-006 + cubeprogrammer fixture-spec |

---

## Test-layer scheme

Per T-005 / T-006 in `decisions.md`. Four layers, four pytest markers:

| Layer | pytest marker | When it runs | Needs | Examples |
|---|---|---|---|---|
| **T-001 Unit** | `@pytest.mark.unit` (default) | Always | No external tools | Banner parsing, `.cproject` XML edits, MI-record parsers, SVD decode logic, _quote() rejection |
| **T-002 Smoke** | `@pytest.mark.smoke` | When tools resolved | `STM32_Programmer_CLI` / `STM32CubeIDE` / `STM32CubeMX` / `arm-none-eabi-gdb` on `PATH` or in `.claude/stm32-tools.local.json` | `--version` parse, headless-build.sh discovery, CubeMX launcher resolution, `_canonical_svd_filename()` lookup |
| **T-002b Smoke-with-probe** | `@pytest.mark.smoke` *(implicit + probe gate)* | When an ST-LINK is enumerated | One ST-LINK probe; **no specific target board** required | `ST-LINK_gdbserver` port-handshake (per debug RES-013(e)) |
| **T-003 Hardware** | `@pytest.mark.hardware` | When matching board present | Hardware board attached over USB; SWD probe accessible | Flash + verify + register reads (read-only) |
| **T-003 Hardware-destructive** | `@pytest.mark.hardware_destructive` | Explicit opt-in only | Same as `hardware` + user consent | Erase chip, option-byte writes, RDP-protected ops |
| **T-007 Eval** | `@pytest.mark.eval` | When eval harness configured | Claude Code SDK + cached responses or live API | Slash-command prompt-to-action verification |

**Default `pytest` invocation:** runs `unit` only. `smoke` / `hardware` / `eval` markers are opt-in via `pytest -m "<marker>"`.

**`tools/check-hw-env.sh`** (per P0-T15 in `phase0-tickets.md`) reports which of the 3 hardware boards are detected — informational, not gating.

---

## Hardware-board coverage matrix

Per RES-019, full T-003 coverage spans three NUCLEO boards. Each test names the board it targets via fixture parametrization; missing boards skip cleanly.

| Board | Role coverage | Primary tests | What's skipped on this board |
|---|---|---|---|
| **NUCLEO-L476RG** | blinky / vcp-echo / faulting / peripherals / fpu / multi-folder (build path) / broken-* (build path) | Most of cubeprogrammer T-003; vcp T-003; debug T-003 (single-core); cubeide T-003 (single-core build + run) | Dual-core attach; N6 signed-flash; QSPI external-flash (no MX25 on this board) |
| **NUCLEO-H745ZI-Q** | dual-core | Debug dual-core attach (M7 + M4); cubeide dual-core nested project build | N6-specific tests; QSPI |
| **NUCLEO-N657X0-Q** | signed-flash | Signing tool end-to-end; cubeprogrammer N6 trusted-flash (`flash_signed()`); F-015 boot-from-flash (Pass-2 compound) | Single-core L4 baseline tests; H7 dual-core |

**Tests skip on absent board** with an explanatory message naming which board would unblock the test.

---

## Cross-tool fixture sharing

| Fixture / asset | Owner module | Other consumers | Channel |
|---|---|---|---|
| `F-PROJ-NUCLEO-L476RG-BLINKY` | shared (T-004) | cubeide (build), cubeprogrammer (flash + verify), debug (attach + register reads), vcp (paired echo response) | On-disk path under `tests/fixtures/projects/` |
| `F-PROJ-NUCLEO-N657X0-Q-SIGNED` | shared (T-004) | signing (input bin), cubeprogrammer (`flash_signed()`) | On-disk path |
| `F-PROJ-NUCLEO-L476RG-FAULTING` | shared (T-004) | debug (hardfault decode), cubeprogrammer (`analyze_hardfault()` binary path) | On-disk path |
| `F-SVDX-*` (SVD samples) | debug | (none in Pass 1; future cubeprogrammer SVD lookups read same files via `ctx.svd_db`) | `ctx.svd_db: SvdDb` field |
| `F-LP-*` (list-ports snapshots) | vcp | (none direct; could be referenced by debug for ST-LINK enumeration in future) | Fixture file |
| Banner files (`banners/`) | cubeprogrammer | debug (probe enumeration parse), signing (vendor-error capture per RES-018) | Fixture file |

---

## Top-level `conftest.py` composition

Per T-006 (code phase). Top-level conftest provides shared fixtures; per-tool conftests extend with module-scoped ones.

```python
# tests/conftest.py  (Pass-1 contract; pseudocode)
import pytest

@pytest.fixture(scope="session")
def substrate_ctx():
    """SubstrateContext built from .claude/stm32-tools.local.json + env."""
    ...

@pytest.fixture(scope="session")
def hardware_board():
    """Detected board via tools/check-hw-env.sh. Returns board ID or None."""
    ...

def pytest_collection_modifyitems(config, items):
    """Apply skip on @pytest.mark.hardware when hardware_board is None
       (or doesn't match the test's parametrize board)."""
    ...
```

Per-tool `tests/fixtures/<tool>/conftest.py` files extend with module-specific fixtures (cataloged in each module's spec).

---

## Build sequence (when each fixture lands)

Per T-005, fixtures build incrementally during code phase — not all at once.

| Sequence | What lands | When | Gate |
|---|---|---|---|
| 1 | T-001 unit fixtures per module (parse-only) | As wrappers land | Each fixture authored when its consuming test is written |
| 2 | T-002 smoke fixtures | As tool wrappers reach smoke layer | User has the relevant CLI tool installed |
| 3 | T-003 reference projects (`F-PROJ-*`) | After T-002 smoke green | User provides projects per cubeide RES-011 user-provides model + cubemx RES-017 multi-IOC catalog |
| 4 | T-003 hardware tests | After projects build green | Matching NUCLEO board attached |
| 5 | T-007 eval fixtures | After eval scaffolding lands (Pass 1 step 11) | Eval framework selected (T-007) |
| 6 | **(Pass 2)** `compound/` fixtures | After CP-* API surface signed off | Per-CP-* fixture-spec drafted alongside its API |

---

## Pass 2 deltas (forward-looking)

When Pass 2 (CP-* compounds) lands:

- New row in the **per-tool catalogs** table for `compound` — `tests/fixtures/compound/fixture-spec.md`.
- New domains in the registry — likely `F-CP-CHAIN-*` (CP-001..CP-013 outcome captures), `F-COMPOUND-PROJ-*` if compound-specific reference projects are needed.
- New entries in **cross-tool sharing** for compound fixtures that read multiple per-tool inputs.
- **No rename of Pass-1 fixtures** is expected — Pass 1 IDs stay stable through Pass 2.
- Aggregated count target post-Pass-2: ~3,200–3,500 lines across 7 spec files (estimate).

---

## State

**Pass 1 release-grade:** per-tool aggregation complete; F-PROJ taxonomy + F-* domain registry stable; test-layer scheme + hardware-board coverage matrix codified; cross-tool sharing channels documented; build sequence enumerated.

**Pending:** code-phase materialization (T-001/002/003 fixture build-out happens incrementally during code phase per T-005). This spec is the contract; the on-disk fixtures land alongside their consumer tests.

**Pass 2 trigger:** CP-* API surface sign-off (M-015 step 3) — at which point a new `compound/` row is added across the tables above and `compound/fixture-spec.md` is drafted.
