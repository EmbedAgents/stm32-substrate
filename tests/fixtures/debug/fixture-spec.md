# Test fixtures — `debug` module

**Last updated:** 2026-05-11 (round-1 answers integrated; debug module #3 of 6 fully signed off pending these resolutions landing)
**Status:** **Round-1 signed off 2026-05-11.** Paired with `debug-api.md` (round-1 answers integrated 2026-05-11) per T-005. Module #3 of 6.

---

## Round-1 review answers (2026-05-11)

All 5 round-1 questions answered:

| # | Topic | Resolution |
|---|---|---|
| Q1 | gdb-MI record fixtures — recorded vs synthesized | **(a) Recorded** for the happy path (real arm-gdb MI3 captures against F-PROJ-* projects); synthesized for malformed/error cases. Mixed. |
| Q2 | Peripheral SVD-decode fixtures — sidecar JSON authoring | **(b) SvdDb regen-script with manual-review gate** (cubeide Q3 precedent). TODO(v1+): spot-check 2–3 fixtures via hand-decode against the SVD as an oracle cross-check. |
| Q3 | DIAG-* variant count | **(c) Start with one happy-path variant per DIAG; grow as tests/eval need.** Each F-DIAG-NNN/ entry stays in the v1 catalog with `[out]` status (user-provides model — supplied during code phase from F-PROJ-NUCLEO-F401RE-PERIPHERALS captures). **F-DIAG-002 specifically deferred** per user direction 2026-05-11: watchdog firing requires either deliberate WDT-timeout firmware or post-reset capture, awkward to set up in v1; revisit when needed. Other F-DIAG-NNN/ entries (DIAG-001 / 003..017) stay scope-in for code-phase capture. |
| Q4 | SVD-samples device-family count | **Alternative picked: L4 + Cortex-M4 only for v1.** H7 / U5 / N6 marked `[out]` with deferred status; added when cross-family tests genuinely need them. Per M-018 simple-now. |
| Q5 | Concurrent-session fixture mechanism | **(a) Stub `DebugSession` directly in the test fixture** (no subprocess; pure-Python). Mirrors cubeide's Q5 stub-in-test answer. |

Inline question text + explanations preserved below for audit; each ends with the user's pick.
**Scope:** test inputs the debug module's tests need across unit / smoke / hardware / eval layers.
**Build status:** spec only. Fixture artifacts supplied by the user during the code phase; tests gracefully skip on `[out]` items until artifacts arrive.

---

## How this fixture catalog works

(Same model as cubeide's fixture-spec ratified per RES-011.)

Each fixture requirement has: **ID** · **Status** (`[in]`/`[out]`) · **Description** · **Features required** · **Drop path** · **Multi-artifact** (yes — supply variants for broader coverage) · **Drives tests**.

Tests parametrize over the artifacts under the requirement's drop path; empty dir → `pytest.skip` with the path to populate.

**v1 fixture-authoring rules** (per M-020):
- LF line endings on all text files (gdb-MI logs, SVDs are XML — also LF).
- Project / device names anonymized where artifacts come from user projects.
- Reference projects + workspaces are Linux-generated for v1; v2 will add Windows-generated variants.

---

## Catalog at a glance

| Group | Count | Path prefix | Used by |
|---|---|---|---|
| Reference projects (shared with cubeide) | 4 reused + 1 new | `tests/fixtures/projects/F-PROJ-*` | unit (mocks) + hardware |
| gdb-MI record fixtures | 9 | `tests/fixtures/debug/mi-records/F-MI-*/` | substrate unit (MI parser) |
| Peripheral SVD-decode fixtures | 8 | `tests/fixtures/debug/svd-decodes/F-SVD-*/` | substrate unit (read_peripheral + SVD decode) |
| Hardfault-input fixtures (raw SCB/CFSR/HFSR/MMFAR/BFAR captures) | 5 | `tests/fixtures/debug/hardfaults/F-HF-*/` | **eval / slash-command layer** (Q1 raw-reads-only: substrate doesn't ship `analyze_hardfault` on the gdb path; cubeprogrammer's binary-only path still uses these for its own decoder) |
| DIAG-* recipe-input fixtures | 16 (15 in v1 scope + 1 deferred) | `tests/fixtures/debug/diag-recipes/F-DIAG-*/` | **Eval-layer / future slash-command tests.** Per Q3(c) ratified 2026-05-11: 15 entries (DIAG-001 / 003..017) planned for v1 code-phase capture from F-PROJ-NUCLEO-F401RE-PERIPHERALS — one happy-path variant each, `[out]` until supplied. **F-DIAG-002 deferred** per user direction (watchdog state awkward to capture in v1). Substrate's own unit tests don't depend on any of these; consumers are Claude recipes + future slash-command CLI tests. |
| Session-handle fixtures | 4 | `tests/fixtures/debug/session-handles/F-SH-*/` | substrate unit (lifecycle) |
| Descriptors (substrate-authored) | 4 | `descriptors/` | substrate unit |
| SVD samples (captured + frozen from any of the 3 priority sources) | 5 | `tests/fixtures/debug/svd-samples/F-SVDX-*/` | substrate unit (SvdDb) |

---

## Reference projects (shared per T-004)

These live under top-level `tests/fixtures/projects/` and are shared with cubeide. Debug consumes them; cubeide owns generating them.

| ID | Reused for debug | Drives |
|---|---|---|
| F-PROJ-NUCLEO-L476RG-BLINKY | yes | `start_session` happy path; `read_registers` against post-boot state. |
| F-PROJ-NUCLEO-L476RG-FAULTING | yes | `analyze_hardfault` after flash + run. |
| F-PROJ-NUCLEO-L476RG-VCP-ECHO | yes | breakpoint workflow (set BP in echo loop, hit it, read variable). |
| F-PROJ-DISCO-H747XI-DUAL-CORE | yes | multi-core session (CM7 + CM4 attach). |

A new debug-specific reference project is needed:

### F-PROJ-NUCLEO-F401RE-PERIPHERALS — Peripheral-exerciser firmware

**Status:** present on bench (user-provides per RES-019)
**Path:** `tests/fixtures/projects/X-CUBE-MEMS1/Projects/NUCLEO-F401RE/Applications/IKS02A1/DataLogFusion/`
**Description:** Real ST X-CUBE-MEMS1 reference application — sensor data logging + fusion on the IKS02A1 sensor expansion board (LSM6DSO + LSM303AGR + LIS2DW12 + STTS22H) attached to a NUCLEO-F401RE. Exercises RCC + GPIOA/B/C + I2C (sensor bus) + USART2 (VCP streaming) + DMA + timers + NVIC in a known-good init state so DIAG-* checks have something to inspect.

**Features required:**
- Builds clean against the bundled X-CUBE-MEMS1 firmware tree.
- After reset, executes `HAL_Init()` + clock config + GPIO/I2C/USART/DMA/timer init, then enters the sensor sample + transmit loop.
- Default config:
  - SYSCLK = 84 MHz (STM32F401RE @ HSE + PLL).
  - USART2 enabled (PCE=0, M=8-bit) — wired to the ST-Link VCP.
  - I2C peripheral configured for the IKS02A1 sensor bus.
  - DMA streams armed for sensor sample fan-out.
  - NVIC: USART2 + I2C + DMA IRQs enabled with handlers bound.
  - GPIOA PA2/PA3 in AF7 (USART2); IKS02A1 connector pins in AF / I2C role.

**Multi-artifact:** yes — different MCU families (L4, H7, U5, F4, etc.) for cross-device DIAG coverage. The F401RE entry covers the F4 family; later additions can supply L4/H7/U5 variants under their own `F-PROJ-<board>-PERIPHERALS` paths.

**Drives tests:** DBG-007 (peripheral dump); DIAG-002..017 happy paths (all conditions are positive — clock enabled, NVIC armed, GPIO AF set, DMA armed, I2C running, etc.).

---

## gdb-MI record fixtures (Q1 user-provides — recorded captures)

Captured gdb-MI output for the parser tests. Each fixture is a text file with one MI record per line (plus the originating MI command in a header comment for context).

Population strategy: **mostly recorded.** Run arm-gdb in MI3 mode against F-PROJ-* projects; capture the records. A few synthetic captures for error cases hard to force.

### F-MI-RESULT-DONE — Successful command result

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-RESULT-DONE/`
**Description:** Captures of `^done` result records for various commands (`-data-list-register-values`, `-break-insert`, `-stack-list-frames`, etc.).

**Features required:**
- Each capture file contains the MI command on line 1 (as a comment), the result record on line 2.
- Token-prefixed records (e.g., `42^done,...`) so token-correlation tests have something to match.

**Multi-artifact:** yes — one per MI command we expect to parse.

**Drives tests:** `parse_mi_record` happy path; per-command parsers (`parse_register_dump`, `parse_breakpoint_insert`, `parse_stack_list_frames`).

### F-MI-RESULT-ERROR — Error result record

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-RESULT-ERROR/`
**Description:** `^error,msg="..."` records.

**Features required:**
- At least one each: invalid breakpoint location, expression-evaluation failure, target-not-halted error.

**Drives tests:** error-record parsing → `GDBError` raise with the message preserved.

### F-MI-ASYNC-STOPPED — `*stopped` async notifications

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-ASYNC-STOPPED/`
**Description:** Various `*stopped,reason=...` records.

**Features required:**
- One each: `breakpoint-hit`, `signal-received` (e.g., SIGSEGV), `exited-normally`, `end-stepping-range`.
- Each carries the full record fields per the GDB manual (frame, thread-id, etc.).

**Drives tests:** `run_until_breakpoint` wait + parse → `RunResult` with correct `halt_reason`.

### F-MI-ASYNC-RUNNING — `*running` notifications

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-ASYNC-RUNNING/`
**Description:** `*running,thread-id="..."` records.

**Drives tests:** `target_halted` flips to False on `-exec-continue`.

### F-MI-STREAM-CONSOLE — `~"..."` console-output records

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-STREAM-CONSOLE/`
**Description:** Console-stream records emitted alongside results (e.g., gdb's stdout chatter).

**Drives tests:** parser correctly ignores stream records when matching against `^done`/`^error`; `send_console()` returns the stream contents.

### F-MI-INTERLEAVED — Async + result interleaved

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-INTERLEAVED/`
**Description:** A `^done` arriving while a `*stopped` async record sits in the queue.

**Features required:**
- Realistic interleaving: e.g., user sets a breakpoint, then `-exec-continue` returns `^running`, then `*stopped,reason=breakpoint-hit` arrives ~50ms later.

**Drives tests:** queue draining + matched-token routing of MI records.

### F-MI-MALFORMED — Protocol violation

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-MALFORMED/`
**Description:** Synthetic captures that violate MI3 spec (missing terminator, malformed value, etc.).

**Drives tests:** `parse_mi_record` raises `GDBError(gdb_marker="protocol-violation")`.

### F-MI-DATA-EVALUATE — `-data-evaluate-expression` results

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-DATA-EVALUATE/`
**Description:** Capture of variable-evaluation results for several variable kinds.

**Features required:**
- Integer variable.
- Struct (gdb's `{field1 = N, field2 = "...", ...}` rendering).
- Array.
- Optimized-out variable (`<optimized out>`).
- Variable-not-in-scope error.

**Drives tests:** `parse_evaluate_expression` → `VariableValue` for each kind; `optimized_out=True` flag.

### F-MI-MEMORY-READ — `-data-read-memory-bytes` results

**Status:** captured 2026-06-11 (bench: NUCLEO-L476RG / BLINKY.elf via `tools/capture-mi-records.py`; synthetic entries marked in-file)
**Path:** `tests/fixtures/debug/mi-records/F-MI-MEMORY-READ/`
**Description:** Memory-read result records (the structured-bytes shape, not the older `-data-read-memory`).

**Features required:**
- 4-byte aligned read (32-bit register width).
- Larger block read.
- Unmapped-region read (all `0xFF` returned — substrate flags suspicious-unmapped).

**Drives tests:** `parse_memory_read` → bytes; `PeripheralDump.suspicious_unmapped` detection.

---

## Peripheral SVD-decode fixtures (substrate-internal parser tests)

Each fixture is a tuple: (peripheral name, raw register bytes, expected `PeripheralDump`). The SVD bitfield decoder is the unit under test.

### F-SVD-RCC — RCC peripheral

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-RCC/`
**Description:** Captured RCC peripheral dump from F-PROJ-NUCLEO-F401RE-PERIPHERALS post-init, with the expected decoded `PeripheralDump` as a JSON sidecar.

**Features required:**
- Real bytes captured via `-data-read-memory-bytes` against the peripheral's base+size.
- Sidecar `expected.json` with field-by-field SVD decode.
- LF line endings on the JSON.

**Multi-artifact:** yes — per device family.

**Drives tests:** `SvdDb.decode_register` round-trip; `PeripheralDump.registers[name].fields[name].raw_value` matches sidecar.

### F-SVD-GPIO — GPIOA registers

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-GPIO/`
**Description:** GPIOA bytes + expected decode (MODER/OTYPER/OSPEEDR/PUPDR/AFRL/AFRH/IDR/ODR/BSRR/LCKR).

**Multi-artifact:** yes.

**Drives tests:** GPIO-specific field decoding for DIAG-005/006/016.

### F-SVD-USART — USART1 registers

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-USART/`
**Description:** USART1 bytes + decode.

**Drives tests:** DIAG-011 parity-bit check; DIAG-015 UE bit.

### F-SVD-SPI — SPI1 registers

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-SPI/`
**Description:** SPI1 CR1/CR2/SR + decode.

**Drives tests:** DIAG-007 BUSY-flag sampling (single-sample case); DIAG-010 mode detection; DIAG-015 SPE bit.

### F-SVD-DMA — DMA1 stream registers

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-DMA/`
**Description:** DMA1 controller + one stream's regs.

**Drives tests:** DIAG-013 armed-status decode.

### F-SVD-NVIC — NVIC ISER/IPR/ISPR

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-NVIC/`
**Description:** NVIC register block decode.

**Drives tests:** DIAG-008 NVIC enable/priority/pending decode.

### F-SVD-DBGMCU — DBGMCU.CR

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-DBGMCU/`
**Description:** DBGMCU peripheral decode.

**Drives tests:** DIAG-017 debug-port-disabled bits.

### F-SVD-FAULTS — CFSR / HFSR / SCB.SHCSR / MMFAR / BFAR

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-decodes/F-SVD-FAULTS/`
**Description:** Cortex-M fault status registers + decode.

**Drives tests:** DIAG-001 gdb-path hardfault decode.

---

## DIAG-* recipe-input fixtures (eval-layer / future slash-command CLI tests)

**Post-Q1-reshape (2026-05-11):** Q1 ratified as raw-reads-only, so substrate does NOT ship per-DIAG methods or typed-result dataclasses. The DIAG-* recipes (per expected-behaviors-v2.md) execute by composing raw `read_peripheral()` / `read_memory()` calls + applying vendor-spec rules in Claude or the slash-command CLI layer.

**Q3 resolution (2026-05-11): option (c) — one happy-path variant per DIAG, supplied during v1 code phase.** Each F-DIAG-NNN/ entry stays in the catalog with `[out]` status under the user-provides model. Capture comes from running F-PROJ-NUCLEO-F401RE-PERIPHERALS on the bench during code phase and snapshotting the relevant peripheral state. M-018 simple-now: one variant per DIAG to start; grow as tests/eval need.

**F-DIAG-002 deferred** per user direction 2026-05-11: watchdog firing requires either deliberate WDT-timeout firmware or a post-reset capture, both awkward to set up in v1. Revisit when a real eval/test consumer needs it.

Each F-DIAG-NNN/ directory will hold (when authored):
- Captured peripheral-bytes JSON (the input Claude would see after invoking `read_peripheral(...)`).
- Expected recipe outcome JSON (what Claude or the slash-command layer would produce after applying the rule). The shape is ad-hoc — no shared dataclass.

Substrate's own unit tests do NOT depend on any F-DIAG-NNN/ — they consume F-MI-* (gdb-MI parsing) and F-SVD-* (peripheral decode) directly. The DIAG fixtures are forward-looking inputs for Claude recipes + future slash-command CLI tests.

### F-DIAG-001 — DIAG-001 hardfault recipe (gdb path)

**Status:** `[out]`
**Path:** `tests/fixtures/debug/diag-recipes/F-DIAG-001/`
**Description:** Pairs of (fault-register bytes JSON, expected recipe-outcome JSON in the `HardFaultDecode` shape). Q1 raw-reads-only: substrate does NOT ship `analyze_hardfault` on the gdb path; Claude's recipe reads SCB + raw CFSR/HFSR/MMFAR/BFAR via `read_peripheral` + `read_memory` and constructs `HardFaultDecode` itself (typed shape defined in cubeprogrammer/results.py, shared as cross-tool result).

**Features required:**
- One per fault type: usagefault-undef-instr, memmanage-mpu-violation, busfault-imprecise, escalated-mem-to-hard.
- `callstack` populated (captured `-stack-list-frames` MI record).

**Drives tests:** **eval-layer** test "Claude reads raw fault regs and produces HardFaultDecode matching expected" + cubeprogrammer's own `analyze_hardfault` (binary-only `-hf` path; still typed in cubeprogrammer).

### F-DIAG-002 — Watchdog recipe inputs

**Status:** `[out]` — **deferred per user direction 2026-05-11.** Not authored in v1 even alongside the other F-DIAG-* fixtures. Reason: capturing a watchdog-fired state requires either firmware that deliberately times out the WDT (then attaching gdbserver fast enough to read RCC.CSR before the next clear) or a separate post-reset capture flow. Awkward setup; not worth the v1 effort. Revisit when a real eval/test consumer needs the fixture.
**Path:** `tests/fixtures/debug/diag-recipes/F-DIAG-002/`
**Description:** (When eventually authored:) Variants: `(IWDGRSTF=1)`, `(WWDGRSTF=1)`, `(both=0)` cases. Each fixture is a (RCC/IWDG/WWDG peripheral-bytes JSON, expected-outcome JSON).

**Drives tests (when authored):** Claude's DIAG-002 recipe reads RCC.CSR + IWDG.SR + WWDG.CR/.CFR via `read_peripheral` and produces the watchdog-status outcome.

### F-DIAG-003 — Clock-tree recipe inputs

**Status:** `[out]`
**Path:** `tests/fixtures/debug/diag-recipes/F-DIAG-003/`
**Description:** Variants: nominal-clock, PLL-not-locked, HSE-bypass-detected, LSE-not-running. Each fixture is a (RCC peripheral-bytes JSON + descriptor `firmware.expected_sysclk_mhz`, expected-outcome JSON).

**Drives tests (eval-layer):** Claude's DIAG-003 recipe + DBG-010 clock derivation.

### F-DIAG-004..F-DIAG-017 — One folder each per DIAG entry

**Status:** `[out]` for all
**Path:** `tests/fixtures/debug/diag-recipes/F-DIAG-NNN/` for each
**Description:** Same shape — captured peripheral-bytes JSON + expected recipe-outcome JSON. Multi-artifact: per-peripheral / per-device variants.

(Folders enumerated for clarity; the pattern is uniform — substrate unit tests don't consume these directly; the eval/slash-command layer does):

- `F-DIAG-004/` — peripheral-clock recipe. Variants: enabled, disabled, peripheral-not-present.
- `F-DIAG-005/` — gpio-af-mode recipe. Variants: all-in-AF, one-pin-not-AF, peripheral-with-no-default-pins.
- `F-DIAG-006/` — gpio-af-number recipe. Variants: all-match, one-wrong-AF, custom-pin-mapping.
- `F-DIAG-007/` — peripheral-busy recipe. Variants: always_busy (stuck), always_idle, toggling, I2C-bus-stuck-suspected.
- `F-DIAG-008/` — nvic-status recipe. Variants: enabled-priority-set, disabled, pending.
- `F-DIAG-009/` — vector-table recipe. Variants: custom-handler-bound, default-handler-fallback, ELF-not-available.
- `F-DIAG-010/` — peripheral-mode recipe. Variants: ISR-only, DMA-only, Polling, Mixed-TX-DMA-RX-ISR.
- `F-DIAG-011/` — uart-parity recipe. Variants: none-matches-host, even-matches, mismatch.
- `F-DIAG-013/` — dma-armed recipe. Variants: armed-normal, armed-circular, disabled, DMAMUX-channel.
- `F-DIAG-015/` — peripheral-enable recipe. Variants: PE-set, PE-clear.
- `F-DIAG-016/` — swd-pin recipe. Variants: pins-in-AF, pins-reconfigured-to-GPIO.
- `F-DIAG-017/` — debug-port-disabled recipe. Variants: nominal, DBG_STOP=0, DBGMCU-clock-disabled.

---

## Hardfault decode fixtures (DIAG-001 specific)

Same shape as F-DIAG-001 above; broken out for visibility because DIAG-001 has both gdb-path and cubeprogrammer-path callers (cross-tool result).

### F-HF-USAGEFAULT — UsageFault decode

**Status:** `[out]`
**Path:** `tests/fixtures/debug/hardfaults/F-HF-USAGEFAULT/`
**Description:** Captured CFSR (UFSR portion) showing UNDEFINSTR, INVSTATE, etc.

### F-HF-MEMMANAGE — MemManage fault

**Status:** `[out]`
**Path:** `tests/fixtures/debug/hardfaults/F-HF-MEMMANAGE/`
**Description:** MMFAR populated; CFSR MMARVALID set.

### F-HF-BUSFAULT-PRECISE — Precise BusFault

**Status:** `[out]`
**Path:** `tests/fixtures/debug/hardfaults/F-HF-BUSFAULT-PRECISE/`
**Description:** BFAR populated; CFSR PRECISERR + BFARVALID set.

### F-HF-BUSFAULT-IMPRECISE — Imprecise BusFault

**Status:** `[out]`
**Path:** `tests/fixtures/debug/hardfaults/F-HF-BUSFAULT-IMPRECISE/`
**Description:** CFSR IMPRECISERR + BFARVALID clear.

### F-HF-ESCALATED — Escalated MemManage → HardFault

**Status:** `[out]`
**Path:** `tests/fixtures/debug/hardfaults/F-HF-ESCALATED/`
**Description:** HFSR.FORCED set; MemManage info also present in CFSR.

---

## Session-handle fixtures

Substrate-side state captures for lifecycle tests.

### F-SH-HALTED — Halted session (DBG-001 happy path)

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-handles/F-SH-HALTED/`
**Description:** Captured gdbserver + gdb stdout/stderr from a successful DBG-001 start, plus an expected `SessionHandle` JSON.

**Drives tests:** `start_session(halt=True)` end-to-end with mocked subprocesses → expected handle fields.

### F-SH-ATTACH-RUNNING — Attach without halt (DBG-003)

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-handles/F-SH-ATTACH-RUNNING/`
**Description:** Same shape, attach-running mode; `target_state="running"`.

### F-SH-N6-DEVMODE — N6 dev-mode attach (DBG-012)

**Status:** `[out]` (deferred — N6 hardware required)
**Path:** `tests/fixtures/debug/session-handles/F-SH-N6-DEVMODE/`
**Description:** N6-specific gdbserver argv; `on_n6_boot_confirm` callable returns True.

### F-SH-SESSION-ALREADY-ACTIVE — Concurrent-session collision

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-handles/F-SH-SESSION-ALREADY-ACTIVE/`
**Description:** Stubbed `DebugSession` pre-populated into `ctx.session_state.active_debug_session`; test then calls `Debug.start_session()` and asserts `GDBError(gdb_marker="session-already-active")` is raised with hint text. Validates the domain-correct error type (not `CubeIDEError` — see RES-020) and the HIL-mode raise-immediately rule.

---

## CLI session-registry fixtures (`F-SREG-*`) — RETIRED PER RES-026 (2026-05-21)

> **Section status: superseded.** The file-based CLI session registry that this fixture group covered (per ADR-002 §"CLI session continuity") was retired before shipping. `stm32 debug` recipes are one-shot per RES-026 — every invocation spawns a fresh gdbserver + arm-gdb, performs a composed operation, tears down. No registry exists; `stm32 debug stop` / `clean-sessions` / `status` subcommands also retired. Section body preserved as historical record (audit log). **Do not author these fixtures.** The seven `F-SREG-*` entries below are inert.

Fixtures covering the file-based CLI session registry at `<workspace>/.stm32-substrate/sessions/active.jsonc` introduced by ADR-002 §"CLI session continuity". Each fixture is a workspace-shaped directory containing a registry file in a specific state, plus an `expected.json` describing the expected post-read result.

All registry-touching test code routes through `embedagents.stm32.platform.process_alive` / `terminate_process` / `acquire_exclusive_lock` (per ADR-005); tests inject fakes/mocks at the platform layer rather than calling `os.kill` directly. This keeps the fixture catalogue OS-agnostic.

### F-SREG-CLEAN — Empty registry

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-CLEAN/`
**Description:** Workspace with no `.stm32-substrate/sessions/active.jsonc` file (substrate's first-run state). `stm32 debug status` returns empty session list; `stm32 debug start ...` creates the directory + writes the first entry.

**Drives tests:** `read_registry_with_cleanup()` initial-create path; lock file creation; atomic write.

### F-SREG-LIVE-SESSION — One live session

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-LIVE-SESSION/`
**Description:** `active.jsonc` carrying one entry with `gdbserver_pid` / `gdb_pid` that the test harness mocks as alive (via fake `process_alive` returning True). `expected.json`: that entry survives stale-detection unchanged.

**Drives tests:** read-with-no-pruning path; `--session ID` resolution; "current session" default-pick.

### F-SREG-STALE-PIDS — Stale entry (process dead)

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-STALE-PIDS/`
**Description:** `active.jsonc` carrying one entry; fake `process_alive` returns False for the recorded PIDs. `expected.json`: registry pruned to empty; WARNING log emitted naming the pruned session id.

**Drives tests:** stale-detection algorithm; self-healing semantics; post-reboot scenario (multiple entries all stale at once).

### F-SREG-MIXED — Some live, some stale

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-MIXED/`
**Description:** `active.jsonc` with three entries — two live, one stale. `expected.json`: registry retains the two live entries, drops the one stale. WARNING log identifies the dropped id.

**Drives tests:** selective pruning under a single lock acquisition.

### F-SREG-CONCURRENT-WRITES — Lock contention

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-CONCURRENT-WRITES/`
**Description:** Two test threads (or two sibling helper processes) both call `read_registry_with_cleanup()` simultaneously. The platform-abstracted lock serialises them; `expected.json`: both writes succeed in serial order; final registry state is consistent (no torn writes).

**Drives tests:** `acquire_exclusive_lock` correctness in registry path; no corruption under contention. Uses a sibling helper process (per cubeide RES-011 Q5 portability rule) to avoid relying on Unix-only `fcntl` semantics in-process.

### F-SREG-MIDWRITE-CRASH — Atomic-write recovery

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-MIDWRITE-CRASH/`
**Description:** Workspace with an `active.jsonc.tmp` file lingering (simulating a CLI crash mid-write) plus an intact `active.jsonc`. `expected.json`: next CLI read ignores the stale `.tmp`, operates on `active.jsonc` cleanly. `stm32 debug clean-sessions` purges the stale `.tmp`.

**Drives tests:** atomic-replace recovery; `clean-sessions` cleanup contract.

### F-SREG-SCHEMA-MISMATCH — Future schema version

**Status:** `[out]`
**Path:** `tests/fixtures/debug/session-registry/F-SREG-SCHEMA-MISMATCH/`
**Description:** `active.jsonc` carrying `schema_version: "2"` (a hypothetical future version this code doesn't understand). `expected.json`: substrate logs WARNING + treats registry as empty (does not error out) — opportunistic forward-compatibility.

**Drives tests:** schema-version handling; doesn't break on a newer-version registry left over from a future install.

---

## SVD samples (frozen subset from any of the 3 priority sources)

A small set of SVD files snapshotted from one of the three SVD sources (priority order CubeIDE → CubeProgrammer → CLT per Q5 ratified 2026-05-11), checked into the test repo so SvdDb tests don't depend on those installs being present. Authoring rule: prefer copying from CubeIDE (the priority-one source); fall back to CubeProgrammer or CLT for SVDs missing from CubeIDE (e.g., ARM core SVDs which CubeIDE doesn't ship).

### F-SVDX-L4 — STM32L4 SVD

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-samples/F-SVDX-L4/`
**Description:** `STM32L476.svd` copied verbatim from CubeIDE's SVD bundle: `<cubeide>/plugins/com.st.stm32cube.ide.mcu.productdb.debug_*/resources/cmsis/STMicroelectronics_CMSIS_SVD/STM32L476.svd` (verified present 2026-05-11; also available in CubeProgrammer and CLT bundles).

**Features required:**
- Valid SVD per CMSIS-SVD schema.
- LF line endings.
- Includes RCC, GPIOA, USART1, SPI1, DMA1, NVIC, DBGMCU peripherals (so DIAG-* tests can decode against it).

**Drives tests:** `SvdDb.parse` + `get_peripheral` + `decode_register` happy path.

### F-SVDX-H7 — STM32H7 SVD

**Status:** `[out]` — **deferred per Q4 ratified 2026-05-11** (v1 scope: L4 + Cortex-M4 only; cross-family SVD coverage added when tests genuinely need it).
**Path:** `tests/fixtures/debug/svd-samples/F-SVDX-H7/`
**Description:** H7-family SVD copied from one of the three priority sources (CubeIDE preferred per Q5 in debug-api.md).

**Drives tests:** (when supplied) dual-core layout (H7 dual-core SVDs have CM7 + CM4 entries); FPU-present.

### F-SVDX-U5 — STM32U5 SVD

**Status:** `[out]` — **deferred per Q4**.
**Path:** `tests/fixtures/debug/svd-samples/F-SVDX-U5/`
**Description:** U5 (M33 TZ-capable) SVD copied from one of the three priority sources.

**Drives tests:** (when supplied) TZ-aware peripheral layouts.

### F-SVDX-N6 — STM32N6 SVD

**Status:** `[out]` — **deferred per Q4**.
**Path:** `tests/fixtures/debug/svd-samples/F-SVDX-N6/`
**Description:** N6 SVD copied from one of the three priority sources (if present in the user's installs).

**Drives tests:** (when supplied) N6-specific dev-mode register paths.

### F-SVDX-CORTEX-M4 — ARM Cortex-M4 core SVD

**Status:** `[out]`
**Path:** `tests/fixtures/debug/svd-samples/F-SVDX-CORTEX-M4/`
**Description:** `Cortex-M4.svd` copied from either CubeProgrammer's `Cores/` (plural) or CLT's `Core/` (singular). **CubeIDE does not ship ARM core SVDs**, so this fixture must come from one of the two fallback sources. Drives DIAG-001 gdb-path fault decode where CFSR / HFSR / SCB / MMFAR / BFAR live (ARM core SVD, not vendor SVD).

**Features required:**
- Valid SVD per CMSIS-SVD schema.
- Includes SCB / NVIC / SYSTICK + the fault-status registers.

**Drives tests:** `SvdDb.find_core_for("Cortex-M4")` lookup; DIAG-001 fault-register decode.

### F-SVDX-MISSING — Synthetic empty SVD-bundle dirs

**Status:** synthesized (test fixture creates 1, 2, or 3 empty `tmp_path/*/svd/` dirs at runtime)
**Description:** Used to test `SVDLookupError` paths across the three-source priority lookup. Variants:
- All three sources unconfigured → `SvdDb.sources_configured == ()` → loud error message names all three config keys.
- One source configured but device not found → `find_for("FAKE_DEVICE")` returns None → caller raises `SVDLookupError` with paths attempted.

**Lookup-parameter taxonomy (v1):** all `SvdDb.find_for(...)` fixtures pass a **device-name** string (banner-shaped, e.g., `"STM32L476RG"`), never a numeric device-id. The fixture catalog and test helpers do not provide a `find_for_device_id(0x435)` path in v1 — it would require an id→family table that no v1 consumer has yet. Add `find_for_device_id` fixtures only when a real consumer surfaces.
- Two sources configured, device missing in first but present in second → priority-fallthrough → second source's path returned.

**Drives tests:** three-path priority lookup; graceful degradation when fewer than 3 sources are available.

---

## Descriptors (substrate-authored)

| File | Drives |
|---|---|
| `descriptors/debug-basic.jsonc` | Baseline `debug.elf_path` + `debug.gdb_port`. |
| `descriptors/debug-expected-sysclk.jsonc` | `firmware.expected_sysclk_mhz` populated for DIAG-003 match. |
| `descriptors/debug-expected-ob.jsonc` | `firmware.expected_option_bytes` (cross-references cubeprogrammer DIAG-018). |
| `descriptors/debug-uart-parity.jsonc` | `firmware.uart_parity` for DIAG-011. |

---

## Layer breakdown

### Unit-layer (T-001) — primary coverage

- gdb-MI parsing: every record kind in `mi-records/` round-trips.
- SVD parsing + register decode: every fixture in `svd-decodes/` matches its sidecar.
- DIAG-* per-prompt methods: every fixture in `diag-outcomes/` produces the expected typed result.
- Hardfault decode: every fixture in `hardfaults/` produces the expected `HardFaultDecode`.
- Session lifecycle: mocked subprocesses + `F-SH-*` handles round-trip.
- `SVDLookupError` paths.
- Concurrent-session collision → `CubeIDEError`.

### Smoke-layer (T-002) — real CLIs, no hardware

- `ST-LINK_gdbserver --version`, `arm-none-eabi-gdb --version` parse.
- CubeMX SVD path exists; at least one device-SVD resolvable.

### Smoke-with-probe layer (T-002b) — real CLIs, ST-LINK probe enumerated but no target

- Spawn gdbserver and check the port handshake. **Requires** an ST-LINK probe present (gdbserver loads CubeProgrammer's DLL on startup via `-cp` and refuses to bind a port without a usable probe in v1 of `ST-LINK_gdbserver`). Skips cleanly when `cubeprogrammer.list_probes()` returns empty. Captures gdbserver stderr verbatim for tool-version-dependent message changes.

The previous "spawn gdbserver against `localhost:0` with no hardware" pattern is removed — empirically `ST-LINK_gdbserver` doesn't reach the bind step without a probe; the test was unreliable across versions. Pure no-hardware smoke is restricted to `--version` parsing.

### Hardware-layer (T-003) — attached NUCLEO

- `start_session()` against F-PROJ-NUCLEO-L476RG-BLINKY → halted, registers match reset state.
- `read_peripheral("RCC")` returns device-typical reset values.
- `analyze_hardfault()` against F-PROJ-NUCLEO-L476RG-FAULTING after flash + run → `hardfault_detected=True`.
- `cpu_frequency()` matches `expected_sysclk_mhz`.
- DIAG-002..017 against F-PROJ-NUCLEO-F401RE-PERIPHERALS, asserting expected typed-results per known firmware config.
- Cross-module compound (cubeide builds → cubeprogrammer flashes → debug analyzes).

### Eval-layer (T-007) — placeholder

T3 (DIAG-019/020, DBG-008/009/011) consume this module's raw reads + snapshot. Eval scenarios live under `tests/fixtures/eval/` when T3 lands.

---

## Cross-tool sharing

| Shared fixture | Owners |
|---|---|
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/` | cubeide (build owner), cubeprogrammer (flash), **debug (sessions)**, vcp |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-FAULTING/` | cubeide (build), cubeprogrammer (-hf), **debug (analyze_hardfault)**, compound |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/` | cubeide (build), cubeprogrammer (flash), **debug (breakpoint workflow)**, vcp (round-trip) |
| `tests/fixtures/projects/STM32CubeH7/Projects/STM32H747I-DISCO/Applications/USB_Host/MSC_Standalone/STM32CubeIDE/` (F-PROJ-DISCO-H747XI-DUAL-CORE) | cubeide (build), **debug (multi-core attach)** |
| `tests/fixtures/projects/X-CUBE-MEMS1/Projects/NUCLEO-F401RE/Applications/IKS02A1/DataLogFusion/` (F-PROJ-NUCLEO-F401RE-PERIPHERALS) | **debug (DIAG-* coverage)**, cubeprogrammer transitively |
| `tests/fixtures/devicedb/` | cubemx (owner), cubeide, cubeprogrammer, **debug (peripheral → IRQ + DMA + pin mapping)** |
| `tests/fixtures/configs/` | substrate-wide schema validation |

---

## Build sequence

Per T-005:

1. **API-surface phase (now):** spec only.
2. **Code phase (incremental as user supplies):**
   - **User** captures gdb-MI records via real arm-gdb sessions on F-PROJ-* projects; tags + commits.
   - **User** captures peripheral dumps + writes expected-decode JSON sidecars.
   - **User** supplies the F-PROJ-NUCLEO-F401RE-PERIPHERALS project (CubeMX-generated peripheral exerciser).
   - **User** copies F-SVDX-* SVDs preferring CubeIDE's bundle (priority-one source); Cortex-M4 core SVD comes from CubeProgrammer's `Cores/` or CLT's `Core/` since CubeIDE doesn't ship core SVDs.
   - **User** captures hardfault scenarios by running F-PROJ-NUCLEO-L476RG-FAULTING and snapshotting fault regs.
   - **Substrate-side** descriptors authored by Claude (tiny JSONC files).
   - Tests run continuously against whatever's supplied; missing fixtures cleanly skip.
3. **Hardware phase:** real-board exercises of DIAG-* against F-PROJ-NUCLEO-F401RE-PERIPHERALS.
4. **Eval phase:** added under `tests/fixtures/eval/` when T3 begins.

---

## Round-1 review questions

Per the inline-explanation discipline.

---

### Q1. gdb-MI record fixtures — recorded captures vs synthesized

**Context.** The MI parsers need fixtures: known-good MI records the parser should produce typed dataclasses from. Two ways to author these:

- **(a) Recorded** (proposal): run a real arm-gdb in MI3 mode against the F-PROJ-* projects, capture stdout, commit verbatim. Each fixture is a real captured record.
- **(b) Synthesized**: hand-author MI records per the spec.

**Trade-off.** Recorded captures are guaranteed parseable by real gdb; if gdb changes its MI emission style (which happens across versions), recorded fixtures catch it. Synthesized fixtures are easier to author for error cases (malformed records that real gdb wouldn't emit) but risk diverging from real gdb behavior.

**Proposal:** (a) for the happy path (one capture per MI command we parse), (b) synthesized for error/malformed cases. Mixed.

**Pick.** (a)

---

### Q2. Peripheral dump fixtures — sidecar JSON for expected decode

**Context.** `svd-decodes/F-SVD-*/` fixtures pair captured peripheral bytes with an expected typed-`PeripheralDump`. The expected JSON has to be authored somewhere:

- **(a) User hand-writes the expected-decode JSON** for each captured byte fixture. Tedious for large register blocks (RCC has ~30 registers, each with many fields).
- **(b) Substrate's SvdDb generates the expected JSON once via a regen script, manually reviewed, then frozen** (cubeide Q3 precedent). The script applies SVD decode to the raw bytes; reviewer eyeballs the JSON; freeze.

**Trade-off.** (a) decoder is an independent oracle; catches decoder bugs. (b) substrate is its own oracle; needs review gate. Same trade-off as cubeide Q3.

**Proposal:** (b) for v1 (cubeide precedent), with the manual-review gate. **TODO(v1+):** spot-check 2–3 fixtures by hand-decode against the SVD as a cross-check.

**Pick.** (b)

---

### Q3. DIAG-* outcome fixtures — multi-artifact variant strategy

**Context.** Each `F-DIAG-NNN/` directory carries variants (e.g., DIAG-002 watchdog: IWDG-only, WWDG-only, no-recent-reset). The fixture catalog above proposes ~3 variants per DIAG.

**Question.** Are 3 variants per DIAG enough for v1, or should we aim for more? Concretely:

- **(a) 3 variants per DIAG = ~48 fixture files for diag-outcomes/.** Sufficient for parser/decoder correctness; misses some real-world combinations.
- **(b) Comprehensive coverage = ~10 variants per DIAG = ~160 fixture files.** Future-proof but a lot of upfront authoring.
- **(c) Start with 1 happy-path variant per DIAG and grow** as bugs surface.

**Proposal:** (c). Start with one canonical happy-path fixture per DIAG; add error/edge-case variants as tests need them. The user-provides model + `[out]` skip-cleanly behavior means we don't have to enumerate all variants upfront. Per M-018 simple-now.

**Pick.**

**Resolved (2026-05-11):** **option (c) — one happy-path variant per DIAG, supplied during v1 code phase from F-PROJ-NUCLEO-F401RE-PERIPHERALS captures.** Each F-DIAG-NNN/ stays `[out]` under the user-provides model; status flips to `[in]` as the user captures and supplies the artifact during code phase. **F-DIAG-002 specifically deferred** per user direction (watchdog state awkward to capture in v1; revisit when a real consumer needs it). The other 15 (F-DIAG-001 / 003..017) are in v1 scope. Substrate's own unit tests do not consume these — F-MI-* and F-SVD-* cover the substrate library — so the DIAG fixtures are forward-looking inputs for Claude recipes + future slash-command CLI tests.

---

### Q4. SVD samples — which device families to bundle as fixtures

**Context.** Tests for `SvdDb` need actual SVD files from any of the three priority sources (CubeIDE → CubeProgrammer → CLT per Q5 ratified 2026-05-11). All three ship the same `STM32<family>.svd` naming; coverage varies slightly (CubeIDE 216, CubeProgrammer 227, CLT 218). We want a small subset checked into the repo so tests don't depend on these tools being installed.

**Proposed set (4 device families + 1 ARM core, covering the substrate's known device matrix):**
- L4 (NUCLEO-L476RG is canonical hardware — `STM32L476.svd`). Source: prefer CubeIDE (priority-one); also in CubeProgrammer and CLT.
- H7 (dual-core + FPU — e.g., `STM32H743.svd`).
- U5 (M33 + TrustZone capable — e.g., `STM32U575.svd`).
- N6 (signed-binary + external-flash family — e.g., `STM32N657.svd` if present in any of the three on the user's host).
- **Cortex-M4 core** — for DIAG-001 fault-register decode. **CubeIDE doesn't ship Core SVDs**, so this comes from CubeProgrammer's `Cores/Cortex-M4.svd` (plural) or CLT's `Core/Cortex-M4.svd` (singular).

**Alternative.** Just L4 + Cortex-M4 for v1; add more device families as cross-family tests need them.

**Proposal:** all four families + Cortex-M4 — keeps cross-family DIAG-* coverage real from the start AND covers the gdb-path hardfault decode which needs the core SVD. Each device SVD is ~1–3 MB; Cortex-M4 SVD is ~100 KB; total ~10 MB committed.

**Pick.** Just L4 + Cortex-M4 for v1; add more device families as cross-family tests need them.

**Resolved (2026-05-11):** L4 + Cortex-M4 only for v1. F-SVDX-H7 / F-SVDX-U5 / F-SVDX-N6 stay in the catalog with `[out]` + deferred-per-Q4 rationale so the path is reserved when needed.

---

### Q5. Concurrent-session fixture mechanism

**Context.** Like cubeide's `locked-by-gui/`, the debug module needs a fixture simulating "another debug session is already active" to test the concurrent-session collision raise (Q7 in debug-api.md). The simplest mechanism mirrors cubeide Q5:

- **(a) Set `ctx.session_state.active_debug_session` to a stub `DebugSession` directly in the test fixture.** Pure-Python; no subprocess. No OS-portability issue.
- **(b) Spawn an actual gdbserver subprocess** that holds the port.

**Trade-off.** (a) is the simpler and more direct test of substrate's collision-detection logic. (b) is more realistic but doesn't add coverage; we already test gdbserver-port-busy elsewhere via the port-fallback test.

**Proposal:** (a). Stub `DebugSession` in the test; verify substrate raises `CubeIDEError(cubeide_marker="session-already-active")`.

**Confirm.** (a)

**Resolved (2026-05-11):** (a) stub `DebugSession` in test fixture; no subprocess. Mirrors cubeide Q5.

---

## State

- **Round-1 review answers integrated 2026-05-11.** All 5 questions resolved (see "Round-1 review answers" at top + per-question audit above).
- **Build deferred** to code phase per T-005.
- **Debug module fully signed off** — both `debug-api.md` (RES-012) and this fixture-spec ratified. Module #3 of 6 complete.
- **v1 scope of this fixture catalog:**
  - **In-scope substrate-unit-test fixtures:** F-MI-* (gdb-MI records), F-SVD-* (peripheral SVD-decodes), F-SH-* (session handles), F-SVDX-L4 + F-SVDX-CORTEX-M4 (SVD samples), descriptors/.
  - **In-scope reference projects:** F-PROJ-NUCLEO-L476RG-BLINKY, F-PROJ-NUCLEO-L476RG-FAULTING, F-PROJ-NUCLEO-L476RG-VCP-ECHO, F-PROJ-DISCO-H747XI-DUAL-CORE (reused from cubeide); F-PROJ-NUCLEO-F401RE-PERIPHERALS (new — owned by debug session for code-phase build).
  - **In-scope eval-layer/slash-command inputs** (planned for v1 code-phase authoring per Q3(c)): F-DIAG-001, F-DIAG-003..F-DIAG-017 (15 entries, one happy-path variant each).
  - **Deferred (`[out]` planning artifacts, not planned for v1 code-phase capture):** F-DIAG-002 (watchdog — Q3 user direction); F-HF-* hardfault-input (cubeprogrammer's binary-only `analyze_hardfault` consumes — flag during code phase whether cubeprogrammer fixture-spec already covers); F-SVDX-H7 / F-SVDX-U5 / F-SVDX-N6 (per Q4).
- **Next module after sign-off:** `vcp` (VCP-001..006 sans SWV per cubeprogrammer's VCP-007 ownership).
