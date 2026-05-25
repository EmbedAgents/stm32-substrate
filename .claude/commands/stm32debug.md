---
description: STM32 debug session — gdbserver + arm-gdb start, attach, SVD lookup
argument-hint: <subcommand> [args...]
allowed-tools: Bash(stm32 debug:*), Bash(.venv/bin/stm32 debug:*), Bash(python:*), Bash(.venv/bin/python:*)
---

The user wants to start, inspect, or check a debug session. Map the request to a `stm32 debug` recipe subcommand.

User input: `$ARGUMENTS`

## Recipe-flow model (per RES-026)

Every `stm32 debug ...` invocation is one-shot: spawns a fresh `ST-LINK_gdbserver` + `arm-none-eabi-gdb`, performs the composed operation, tears down, emits JSON. Debug state does not persist across CLI calls. For stateful workflows (set N breakpoints, run, hit, inspect, set more, continue) drop into Python (see "Stateful workflows — Python" below).

## Subcommand map

**ELF autodiscovery (applies to every subcommand below that takes an ELF):**

When `stm32-project.jsonc` is in the cwd (or a parent), `debug.elf_path` is read and used automatically — **omit the ELF argument**. Pass an explicit path only when overriding the descriptor or when no descriptor is present. **Do not** Glob for `*.elf` or ask the user — just invoke the recipe without an ELF arg and let the substrate raise a loud `ConfigurationError` if the descriptor is missing.

**N6 device-family auto-cue:** when the descriptor's `firmware.device_family` starts with `STM32N6`, add `--n6-dev-mode` to `start` invocations (BOOT-switch confirmation is mandatory).

**Session lifecycle (DBG-001 / DBG-003 / DBG-012):**
- `stm32 debug start [ELF] [--port N] [--no-halt] [--n6-dev-mode]` — DBG-001 — spawn gdbserver + arm-gdb, optionally halt at reset, optional N6 dev-mode boot. Emits `SessionHandle` + tears down. ELF is autodiscovered from the descriptor if omitted.
  - Without `--no-halt`: target halts at entry (DBG-001 default).
  - With `--no-halt`: attach without halting (DBG-003 — `attach_running` semantics).
  - `--n6-dev-mode`: required for N6 — substrate prompts the user to confirm BOOT pins.

**SVD lookup (D-008 / DBG-007 inputs):**
- `stm32 debug svd-path DEVICE_NAME` — resolve the `.svd` for a device via the CubeIDE → CubeProgrammer → CLT priority chain (P-033). Pure lookup, no subprocess.

**Composed-flow recipes (one-shot — spawn, do, tear down, JSON):**

Every recipe below accepts an optional trailing `[ELF]` positional that's autodiscovered from the descriptor if omitted. The signatures below show the common form; append a path only when overriding.

- `stm32 debug check-variable --at LOCATION --var NAME --expected V [--mask M] [ELF]` — DBG-004 — start session + set breakpoint at LOCATION + run until hit + read variable + compare → `ComparisonResult`.
- `stm32 debug check-register --at LOCATION --reg NAME --expected V [--mask M] [ELF]` — DBG-005 — same with register compare.
- `stm32 debug read-registers [ELF]` — DBG-006 — read CPU registers from a halted target → `RegisterDump`.
- `stm32 debug read-peripheral NAME [INSTANCE] [ELF]` — DBG-007 — start + halt + SVD-decoded peripheral dump → `PeripheralDump`. Names per SVD (e.g. `RCC`, `GPIOA`, `USART1`, `SCB`, `NVIC`).
- `stm32 debug read-memory --address 0x... --size N [ELF]` — start + halt + memory read → `MemoryReadResult`.
- `stm32 debug callstack [--full] [ELF]` — start + halt + callstack → `CallStack`. `--full` includes args/locals per frame.
- `stm32 debug snapshot [--include-peripheral NAME]... [ELF]` — DIAG-021 — start + halt + composite snapshot (registers + callstack + named peripherals + disasm-around-pc) → `DebugSnapshot`.
- `stm32 debug decode-hardfault [ELF]` — DIAG-001 gdb path — start (halted) + compose the raw fault bundle (SCB peripheral dump + registers + callstack) → `DebugSnapshot`. The substrate encodes **no decode rule** (ADR-004); **you** read the raw CFSR/HFSR and classify the fault (MemManage / BusFault / UsageFault / HardFault).

**LOCATION** in `--at` is a gdb location string passed verbatim: `main`, `SysTick_Handler`, `main.c:84`, `stm32n6xx_it.c:SysTick_Handler`, `*0x080012ac`, `+10`, `-5`. The substrate forwards untouched.

## Peripheral & register diagnostics (DIAG-002…017)

For "is X configured / enabled / stuck / right?" diagnostics, **prefer `stm32 debug read-peripheral <NAME>`** over a raw `stm32 prog read-memory` of the register address: it halts the target and returns the **SVD-decoded fields**, so you read named bits (`PLLON`, `SWS`, `MODER`, `PCE`, …) instead of hand-decoding hex. Map the diagnostic to the governing peripheral, then interpret the decoded fields — the verdict is yours (RES-030 "level (a)"):

| Diagnostic (user intent) | `read-peripheral` target |
|---|---|
| Clock tree / SYSCLK / "are we at expected freq" (DIAG-003) | `RCC` |
| Peripheral clock enabled for X (DIAG-004) | `RCC` |
| Watchdog firing / WDT reset (DIAG-002) | `RCC` (CSR reset flags) + `IWDG` / `WWDG` |
| GPIO in AF mode / AF number for X (DIAG-005/006) | `GPIOx` (the peripheral's port) |
| Peripheral BUSY stuck / driver mode / PE bit (DIAG-007/010/015) | the peripheral itself (`SPI1`, `I2C1`, `USART2`, …) |
| NVIC IRQ enabled for X (DIAG-008) | `NVIC` |
| UART parity vs host (DIAG-011) | `USARTx` |
| DMA armed for X (DIAG-013) | `DMAx` |
| SWD pins repurposed as GPIO (DIAG-016) | `GPIOA` (PA13/PA14) |
| Debug port disabled in software (DIAG-017) | `DBGMCU` |
| ISR registered in vector table (DIAG-009) | `SCB` (VTOR) + `read-memory` at the vector slot |

`read-memory` is for raw memory at an explicit address (a vector slot, an arbitrary buffer) — not for decoding a known peripheral whose fields the SVD already names.

## Stateful workflows — Python

Multi-step interactive sessions (set N breakpoints across the run, accumulate observations, change strategy mid-run, enable/disable breakpoints by number, etc.) have no CLI surface — they require the Python `DebugSession` context manager:

```bash
python - <<'PY'
from stm32_substrate.context import SubstrateContext
from stm32_substrate.debug import Debug
ctx = SubstrateContext.from_environment()
with Debug(ctx).start_session(elf_path="Debug/firmware.elf") as s:
    bp1 = s.set_breakpoint("main")
    bp2 = s.set_breakpoint("usart1_irq")
    r = s.run_until_breakpoint(timeout_s=30)
    print(s.read_variable("uart_buf_count"))
    # ...
PY
```

This is the canonical interface for T3 fix loops (B-021 build/fix, DIAG-019/020 crash classification, DBG-008/009 stack/heap). For an iterative loop, render a Python heredoc per iteration with the observations Claude wants to make + the next-iteration hypothesis. (DBG-011 was re-tiered T3→T2 per RES-031 — a single user-requested clock change, not a loop.)

## When to use which

| User intent | Surface |
|---|---|
| "Start a debug session" / "Attach to the running target" | `stm32 debug start` |
| "Where is the SVD for X" | `stm32 debug svd-path` |
| "Check that variable / register X is Y when execution reaches Z" | `stm32 debug check-variable` / `check-register` |
| "Show me the registers / peripheral / memory / callstack right now" | `stm32 debug read-*` / `callstack` (each spawns a fresh halt-and-read session) |
| "Take a debug snapshot" | `stm32 debug snapshot` |
| "Decode the hardfault (with source available)" | `stm32 debug decode-hardfault` |
| "Decode the hardfault (binary-only, no debug session)" | `/stm32prog hardfault` per M-012 |
| Build / fix loop, crash classification, multi-step interactive debugging | Python heredoc against `DebugSession` |

## Output handling

Recipes emit a JSON-serialized result dataclass on stdout. Errors carry `GDBError(gdb_marker=...)` markers: `session-already-active`, `no-free-gdb-port`, `port-busy`, `gdbserver_spawn_timeout`, `n6-boot-not-confirmed`, `command-timeout`, plus `SVDLookupError` for `svd-path`. Surface `message` + `hint` to the user.

`decode-hardfault` vs `/stm32prog hardfault` (M-012 dual-tool routing): the gdb path uses live registers + callstack + source context; the binary-only path uses `STM32_Programmer_CLI -hf`. **Different outputs:** the gdb path returns a raw `DebugSnapshot` you classify yourself (substrate encodes no rule, ADR-004); the binary path returns a typed `HardFaultDecode` (the vendor `-hf` analyzer's own classification, captured). Pick the gdb path when source/ELF is available; pick the binary path post-mortem.

Crash classification (DIAG-019 / DIAG-020) is Wave-3a T3 (signed off per RES-031 — no longer deferred) — Claude composes the gather in Python over `decode-hardfault` + recipe reads + `snapshot`, then classifies (memory-corruption vs interrupt-related; cache-coherency) from the evidence. The verdict is Claude's; the substrate supplies the bundle and encodes no rules (ADR-004).
