# Test fixtures — `cubeprogrammer` module

**Last updated:** 2026-05-10
**Status:** **Round-1 signed off.** All 4 review questions answered (see bottom). Paired with `cubeprogrammer-api.md` per T-005.
**Scope:** fixtures the cubeprogrammer module's tests will need across all four layers (unit / smoke / hardware / eval).
**Build status:** spec only. Fixtures are **not built** during the API-surface phase per T-005 step 1 — they're built incrementally during the code phase as wrappers land.

---

## Layout

```
tests/fixtures/cubeprogrammer/
├── fixture-spec.md            # this file
├── banners/                   # captured STM32_Programmer_CLI -c port=swd stdout
│   ├── nucleo-l476rg-good.txt
│   ├── nucleo-l476rg-suspicious-voltage.txt
│   ├── nucleo-l476rg-rdp1.txt
│   ├── stm32h7-dual-core.txt
│   ├── stm32n6570-dk.txt
│   ├── stm32u5-tzen.txt
│   ├── unknown-device-id.txt          # device newer than DB
│   └── multidrop-firmware-old.txt
├── option-bytes/              # -ob displ stdout per family
│   ├── stm32l4-default.txt
│   ├── stm32l4-rdp1.txt
│   ├── stm32u5-tzen-on.txt
│   ├── stm32h7-dual-bank.txt
│   └── stm32n6-secwm.txt
├── hex-dumps/                 # -rd stdout (memory peek)
│   ├── ram-32bytes.txt
│   ├── flash-256bytes.txt
│   ├── peripheral-rcc-block.txt
│   ├── unmapped-region-allff.txt      # to exercise suspicious_unmapped detection
│   └── alignment-fallback-16bit.txt
├── hardfaults/                # -hf stdout
│   ├── no-fault.txt
│   ├── usagefault-undefinstr.txt
│   ├── memmanage-mpu-violation.txt
│   ├── busfault-imprecise.txt         # BFAR not valid case
│   └── escalated-mem-to-hard.txt      # multi-fault chain
├── probe-lists/               # -l stdout
│   ├── empty.txt                      # no probe attached
│   ├── single-stlink-v3.txt
│   ├── two-stlinks.txt
│   ├── multidrop-two-targets.txt
│   └── stlink-firmware-old.txt
├── swv/                       # -swv stream samples
│   ├── itm-printf-port0.txt
│   ├── itm-mixed-ports.txt
│   └── dropped-bytes-marker.txt
├── errors/                    # stderr + exit-code pairs for parse_error()
│   ├── target-dll-err.txt              # code 2; no probe
│   ├── target-no-device.txt            # code 4
│   ├── target-unknown-mcu.txt          # code 5
│   ├── target-firmware-old.txt         # code 6
│   ├── target-held-reset.txt           # code 8
│   ├── target-not-halted.txt           # code 9
│   ├── target-stlink-select-req.txt    # code 16
│   ├── target-stlink-serial-not-found.txt # code 17
│   ├── flash-protected-rdp.txt         # erase rejected
│   ├── flash-alignment-stm32u5.txt     # 16-byte alignment requirement
│   ├── invalid-file-extension.txt
│   └── el-loader-board-mismatch.txt
├── external-loaders/          # synthetic ExternalLoader/ dir contents
│   ├── canonical-h7s78-loader.json     # filename + magic-byte fixture metadata
│   ├── canonical-h7s78-loader-alt.json # same memory-part on different board
│   └── stub-stldr-files/               # zero-byte .stldr files for filename matching
├── projects/                  # ELF/BIN reference artifacts
│   ├── F-PROJ-NUCLEO-L476RG-BLINKY.elf
│   ├── F-PROJ-NUCLEO-L476RG-BLINKY.bin
│   └── F-PROJ-NUCLEO-L476RG-BLINKY.hex
└── descriptors/               # stm32-project.jsonc samples for resolution tests
    ├── single-image.jsonc
    ├── dual-image-boot-app.jsonc
    └── n6-signed-external.jsonc
```

Reference projects under `projects/` are shared with other tool modules per T-004; the cubeprogrammer copy is symlinked to a top-level `tests/fixtures/projects/` once that's established (see "Cross-tool sharing" below).

---

## Unit-layer fixtures (T-001) — primary asset

**Goal:** every parser path and error mapping covered without touching CLI or hardware. Fixtures are recorded stdout/stderr text files; tests load and parse.

### `banners/`

| File | Covers prompts | Why |
|---|---|---|
| `nucleo-l476rg-good.txt` | D-001, D-003, D-004, D-007, D-008, D-011 | Baseline happy path; project's primary hardware target. |
| `nucleo-l476rg-suspicious-voltage.txt` | D-001 | `voltage_v < 2.5` edge case → `voltage_suspicious=True`. |
| `nucleo-l476rg-rdp1.txt` | D-001, D-009 (paired with `option-bytes/stm32l4-rdp1.txt`) | RDP-1 banner shape (some fields redacted). |
| `stm32h7-dual-core.txt` | D-007 | Multi-core; `secondary_cores != []`. |
| `stm32n6570-dk.txt` | D-001 | N6 family for signed-binary path tests. |
| `stm32u5-tzen.txt` | D-007, D-009 | TrustZone enabled; expanded OB set. |
| `unknown-device-id.txt` | D-001, D-008 | Device newer than DB; `device_name="unknown"`, `svd_path=None`. |
| `multidrop-firmware-old.txt` | D-005 | Firmware too old for multidrop; per UM2237 §3.2.10. |
| `no-devicedb-ram-unknown.txt` | D-004 | Post-cubemx-cut: any banner where substrate can't derive RAM size → `MemoryLayoutResult(ram_size_kb=None, bank_layout=None)`; test asserts callers handle None gracefully. |
| `no-devicedb-cores-unknown.txt` | D-007 | Post-cubemx-cut: any banner where substrate can't derive secondary cores → `CoresResult(secondary_cores=[], multi_core=None)`; same gracefully-None contract. |

### `option-bytes/`

| File | Covers | Tests |
|---|---|---|
| `stm32l4-default.txt` | D-009 | Field-by-field parse against `ob-schemas/stm32l4.json`. |
| `stm32l4-rdp1.txt` | D-009 | RDP-1 set; `rdp_level=1`. |
| `stm32u5-tzen-on.txt` | D-009, F-021 | TZEN field present; F-021 destructive-classifier sees TZEN. |
| `stm32h7-dual-bank.txt` | D-009 | DBANK / SWAP_BANK fields. |
| `stm32n6-secwm.txt` | D-009 | Secure-area watermarks per family. |

### `hex-dumps/`

| File | Covers | Tests |
|---|---|---|
| `ram-32bytes.txt` | F-020 | Baseline parse of 32-byte hex+ASCII gutter. |
| `flash-256bytes.txt` | F-020 | Larger contiguous block. |
| `peripheral-rcc-block.txt` | F-020 | Peripheral region; `sr_or_dr_warning=False` (RCC.CR is not clear-on-read). |
| `unmapped-region-allff.txt` | F-020 | All-`0xFF` → `suspicious_unmapped=True`. |
| `alignment-fallback-16bit.txt` | F-020 | Region requiring 16-bit access; CubeProgrammer falls back; output format differs. |

### `hardfaults/`

| File | Covers | Tests |
|---|---|---|
| `no-fault.txt` | DIAG-001 | `hardfault_detected=False`. |
| `usagefault-undefinstr.txt` | DIAG-001 | `fault_type="UsageFault"`, faulty_pc populated. |
| `memmanage-mpu-violation.txt` | DIAG-001 | MMFAR populated. |
| `busfault-imprecise.txt` | DIAG-001 | BFAR-not-valid edge case. |
| `escalated-mem-to-hard.txt` | DIAG-001 | Multi-fault chain decode. |

### `probe-lists/`

| File | Covers | Tests |
|---|---|---|
| `empty.txt` | D-005 | Empty list = empty result, not error. |
| `single-stlink-v3.txt` | D-005 | Baseline. |
| `two-stlinks.txt` | D-001, D-005 | Drives `TARGET_STLINK_SELECT_REQ` (16) raise on `connect()` without `sn=`. |
| `multidrop-two-targets.txt` | D-005 | `target_sel` populated for multidrop. |
| `stlink-firmware-old.txt` | D-005 | `multidrop_unavailable=True` warning. |

### `swv/`

| File | Covers | Tests |
|---|---|---|
| `itm-printf-port0.txt` | VCP-007 | Standard printf-via-ITM stream; lines on port 0. |
| `itm-mixed-ports.txt` | VCP-007 | Multiple ports interleaved; parser distinguishes. |
| `dropped-bytes-marker.txt` | VCP-007 | ST-LINK buffer overflow; per UM2237 §3.2.25 note. |

### `errors/`

**Population strategy (per round-1 Q3): mixed.** Recorded stderr (real captures from forcing the failure on the user's host) for codes that are easy to reproduce; synthetic stderr (hand-authored against the parser regex) for codes that aren't. Each fixture file's header comment names which strategy was used and, for synthetic fixtures, carries a `TODO: replace with recorded stderr when seen in the wild` line.

| Code | Strategy | How to reproduce (for recorded) |
|---|---|---|
| 1 `TARGET_CONNECT_ERR` | recorded | open STM32CubeProgrammer GUI, then run the CLI; probe is held |
| 2 `TARGET_DLL_ERR` | recorded | unplug the ST-LINK |
| 4 `TARGET_NO_DEVICE` | recorded | keep ST-LINK plugged in, disconnect SWD cable from target |
| 16 `TARGET_STLINK_SELECT_REQ` | recorded **iff** two probes available; else synthetic with TODO | plug in two ST-LINK probes, omit `sn=` |
| 17 `TARGET_STLINK_SERIAL_NOT_FOUND` | recorded | pass `sn=DEADBEEF` (a serial that doesn't exist) |
| 3, 5, 6, 7–14 | synthetic with TODO | rare or hard to force on the user's host; replace when encountered |
| 10 `TARGET_CMD_ERR` (RDP-protected erase) | recorded | flash a binary that sets RDP=0xBB, then attempt erase |
| 10 `TARGET_CMD_ERR` (U5 alignment) | synthetic with TODO | needs U5 hardware |
| `el-loader-board-mismatch` | synthetic with TODO | needs N6 hardware + intentional loader mismatch |

Each fixture is a **pair**: `<name>.stderr` carrying the verbatim stderr (no header comment, no preamble) plus a sibling `<name>.exit_code.txt` containing the integer exit code. This sidecar pattern matches the rest of the substrate (cubeide / cubemx / signing fixture-specs) and keeps the recorded stderr byte-identical to what CubeProgrammer actually wrote — no comment-parser needed in test code, no risk of a "# exit_code=N" line being mis-parsed by `parse_error()`.

`parse_error(stderr, exit_code)` returns the expected `CubeProgrammerError`:

| Fixture pair | Expected `error_code` | Expected `recoverable` | Pertinent prompts |
|---|---|---|---|
| `target-dll-err.stderr` + `target-dll-err.exit_code.txt` | `TARGET_DLL_ERR` (2) | False | D-001 (and every method that connects) |
| `target-no-device.stderr` + sidecar | `TARGET_NO_DEVICE` (4) | True | D-001, F-001 |
| `target-unknown-mcu.stderr` + sidecar | `TARGET_UNKNOWN_MCU_TARGET` (5) | True | D-001 |
| `target-firmware-old.stderr` + sidecar | `TARGET_FIRMWARE_OLD` (6) | False | D-001 (D-002 ladder bails on this) |
| `target-held-reset.stderr` + sidecar | `TARGET_HELD_UNDER_RESET` (8) | True | D-006 |
| `target-not-halted.stderr` + sidecar | `TARGET_NOT_HALTED` (9) | True | F-017 |
| `target-stlink-select-req.stderr` + sidecar | `TARGET_STLINK_SELECT_REQ` (16) | False | any method without `sn=` against multi-probe host |
| `target-stlink-serial-not-found.stderr` + sidecar | `TARGET_STLINK_SERIAL_NOT_FOUND` (17) | False | any method with bad `sn=` |
| `flash-protected-rdp.stderr` + sidecar | `TARGET_CMD_ERR` (10) | False | F-001 erase against RDP-1 |
| `flash-alignment-stm32u5.stderr` + sidecar | `TARGET_CMD_ERR` (10) | False | F-003 misaligned writes on U5 |
| `invalid-file-extension.stderr` + sidecar | `None` (parser falls back to `code=exit_code`) | False | F-003 with bogus extension |
| `el-loader-board-mismatch.stderr` + sidecar | `TARGET_CMD_ERR` (10) | False | F-010 with wrong loader |

### `external-loaders/`

The fixture is a synthetic `ExternalLoader/` directory. Tests construct a temp directory, populate it from `external-loaders/stub-stldr-files/` (filenames carry the metadata; file contents are zero bytes), and exercise `discover_external_loader()`:

| Scenario | Files in the fixture dir | Expected return |
|---|---|---|
| Single match | `MX66UW1G45G_STM32H7S78-DK.stldr` | `[that path]` |
| Multiple matches | `MX66UW1G45G_STM32H7S78-DK.stldr`, `MX66UW1G45G_STM32H7B3I-DK.stldr` | `[both paths]` (caller resolves via callback) |
| Zero matches | (only unrelated `.stldr` files) | `[]` |
| EL dir missing | (no dir) | `[]` (caller raises `ConfigurationError` with hint) |
| Explicit override | (whatever; user passes `loader_path=...`) | `[user's path]` after existence check |

Filename-magic metadata for the substrate's matcher lives in `canonical-h7s78-loader.json` etc. Per RES-020, the matcher does **filename-substring matching against `banner.device_name` prefix only** — no `address_range → memory_type` derivation. The sidecar JSON only carries the expected device-family substring per filename; tests assert "loader filename contains expected family substring".

### `descriptors/`

| File | Drives |
|---|---|
| `single-image.jsonc` | F-003 / F-005 default-address resolution. |
| `dual-image-boot-app.jsonc` | F-008 address resolution from `firmware.images[]`. |
| `n6-signed-external.jsonc` | F-009, F-010, F-015 (compound). |

---

## Smoke-layer fixtures (T-002) — secondary asset

Real CLI, no hardware:

| Fixture | Use |
|---|---|
| `STM32_Programmer_CLI --help` snapshot | Smoke test that the CLI is reachable and the version is recent enough that our flags work. Updated via `tools/refresh-cli-snapshots.sh` (P0-T15 territory). |
| `STM32_Programmer_CLI -V` snapshot | Version-check parser fixture; covers the "warn on < 2.21" path. |
| `STM32_Programmer_CLI -d nonexistent.bin 0x08000000` (expected to fail) | Verifies the error-pattern parser against the live tool, not just recorded fixtures. |
| Empty `ExternalLoader/` directory exists in `programmer.cube_programmer_path/bin/` | Sanity: the path layout is what we believe. |

Smoke tests use the user's installed CubeProgrammer (path from `.claude/stm32-tools.local.json`); skipped if the tool isn't present.

---

## Hardware-layer fixtures (T-003) — for the hardware-marked tests

**Hardware:** attached NUCLEO-L476RG (per CLAUDE.md + T-003).

### Reference projects (under top-level `tests/fixtures/projects/` per T-004)

| Project | Built artifacts | cubeprogrammer tests it drives |
|---|---|---|
| `F-PROJ-NUCLEO-L476RG-BLINKY/` | `.elf`, `.bin`, `.hex` | F-003 (all extensions), F-019 (read-back), CP-001 (router) |
| `F-PROJ-NUCLEO-L476RG-VCP-ECHO/` | `.elf` | F-003 + VCP follow-up (compound test) |
| `F-PROJ-NUCLEO-L476RG-FAULTING/` | `.elf` | DIAG-001 hardfault path (load + reset + observe `-hf`) |
| `F-PROJ-NUCLEO-L476RG-BROKEN-COMPILE/` | (no artifact; build fails) | Not exercised by cubeprogrammer; lives here for the cubeide module. |

These are owned at the top level (`tests/fixtures/projects/`) so multiple modules share them; the cubeprogrammer module only references the paths.

### Descriptor for hardware tests

A `tests/fixtures/cubeprogrammer/hardware-descriptor.jsonc` carrying the NUCLEO-L476RG defaults: flash start `0x08000000`, expected device_id, expected `ram_size_kb`, etc. — the unit-test equivalent at the hardware layer.

### What hardware tests cover

- **Read-only** (D-*): every method against the live board; assertions are loose (firmware version may vary on different hosts) but field presence and types are strict.
- **Destructive** (F-001, F-002, F-021): erase / OB-write tests are **opt-in** behind `@pytest.mark.hardware_destructive`. Default `hardware` marker excludes them; CI explicitly enables when running on a dedicated test board.
- **Round-trip** (F-003 + F-019): write `F-PROJ-NUCLEO-L476RG-BLINKY.bin`, read back, byte-compare.
- **VCP-007** (SWV): NUCLEO-L476RG routes SWO through ST-LINK; load `F-PROJ-NUCLEO-L476RG-VCP-ECHO.elf` (which uses ITM_SendChar), capture, assert lines arrive.

### What's skipped on this hardware

- F-006, F-009 — N6/MP* required.
- F-010 with N6 loader — N6 hardware required.
- F-011 dual-bank (L4+ if available; L476RG is single-bank).
- D-005 multidrop (no multidrop board on the standard fixture).

These tests are written but `pytest.skip()` with a clear message naming the missing hardware.

### Hardware fixture dependencies

```python
# tests/conftest.py (top-level)

@pytest.fixture(scope="session")
def hardware_probe():
    """Singleton SWD probe — hardware tests serialize on this fixture per T-006."""
    if not _hw_env_check():
        pytest.skip("NUCLEO-L476RG not attached")
    return ProbeHandle(...)

@pytest.fixture(scope="session")
def cubeprogrammer_client(substrate_context, hardware_probe):
    return CubeProgrammer(substrate_context)
```

`tools/check-hw-env.sh` (P0-T15) is invoked from `_hw_env_check()`.

---

## Eval-layer fixtures (T-007) — placeholder

Per M-014, T3 eval tests are deferred. Cubeprogrammer module has no T3 prompts of its own (DIAG-019/020 are diagnostic; CP-007/012/013 are compound). The eval-layer fixtures live under `tests/fixtures/eval/` once that work begins; this module's fixtures get exercised transitively (e.g., when DIAG-019 calls `analyze_hardfault()` from the binary-only path).

No fixtures specified here for now — flagged for the eval-scaffolding session per T-005 step 4.

---

## Cross-tool sharing

The following fixtures are intended to be **shared** across tool modules; cubeprogrammer just references them. Owned at top-level `tests/fixtures/`:

| Shared fixture | Owners |
|---|---|
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-BLINKY/` | cubeprogrammer (flash), cubeide (build), debug (gdb sessions), vcp (output observation) |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-VCP-ECHO/` | cubeprogrammer (flash), vcp (echo round-trip), compound (CP-002/006) |
| `tests/fixtures/projects/F-PROJ-NUCLEO-L476RG-FAULTING/` | cubeprogrammer (DIAG-001 -hf path), debug (DIAG-001 gdb path), compound (DIAG-019 routing) |
| `tests/fixtures/svd/` | debug, cubeprogrammer (D-008 lookup) |
| `tests/fixtures/devicedb/` | cubemx (owner), cubeprogrammer (RAM size, OB schema, family→memory-type map) |
| `tests/fixtures/configs/` | substrate-wide schema validation tests |

Each owner module's `fixture-spec.md` references the shared fixtures by path; only one module *defines* each shared fixture's contents (the owner). For cubeprogrammer, that's the project artifacts above (we're an owner) plus referenced consumers of `svd/` and `devicedb/` (we're a consumer).

---

## Build sequence

Per T-005:

1. **API-surface phase (now):** spec only. No fixtures built.
2. **Code phase, when cubeprogrammer wrappers land:**
   - Recorded fixtures (`banners/`, `option-bytes/`, `hex-dumps/`, `hardfaults/`, `probe-lists/`, `swv/`, `errors/`) — captured from real CubeProgrammer invocations against the test boards, saved as text files. Each fixture is committed.
   - Synthetic fixtures (`external-loaders/stub-stldr-files/`, `ob-schemas/*.json`, `descriptors/*.jsonc`) — hand-authored.
   - Reference projects (`projects/`) — built once via `tools/build-fixture-projects.sh`, ELF/BIN/HEX checked in (small enough; deterministic-build flags applied so checksums are stable).
3. **Hardware phase, after T1+T2 substrate works locally:** hardware tests run against `projects/` artifacts.
4. **Eval phase, once T3 begins:** eval fixtures added under `tests/fixtures/eval/`.

---

## Round-1 review answers (2026-05-10)

User-confirmed answers, integrated above. Note: original-draft question 2 (OB-schemas-as-fixtures-vs-code) is implicitly resolved by `cubeprogrammer-api.md` Q7 [OUT] — the `ob-schemas/` directory is removed from the layout. Renumbered below.

| # | Topic | Resolution |
|---|---|---|
| 1 | Reference-project source | **Write new** minimal projects in this repo. Honors M-004 (greenfield); avoids importing from `stm32agent/`. |
| 2 | Destructive hardware-test marker | **Two markers** kept: `@pytest.mark.hardware` (default fixture) + `@pytest.mark.hardware_destructive` (opt-in for erase / OB writes). |
| 3 | Recorded vs synthetic stderr fixtures | **Mixed strategy.** Recorded for codes easy to force on the user's host (1, 2, 4, 17, plus 16 if dual probes available); synthetic for the rest with `TODO: replace with recorded stderr when seen in the wild`. See the strategy table in the `errors/` section above. |
| 4 | External-loader fixture realism | **Zero-byte stubs only.** Filename matcher doesn't read content; header-byte sniff is itself a TODO per `cubeprogrammer-api.md`. If/when sniff lands, capture real headers then. |

**Global directive M-018 applied throughout:** every "or later?" question resolved by the simple option with a TODO surfaced in this doc or in the corresponding section above.

---

## State

- **Round-1 signed off 2026-05-10.** All 4 review questions answered (above). `cubeprogrammer-api.md` round-1 also signed off (RES-008).
- **Build deferred** to code phase per T-005.
- **Cubeprogrammer module fully signed off** — next module per M-015 step 2 = `cubeide` (B-* + Project-settings protocol per P-030).
