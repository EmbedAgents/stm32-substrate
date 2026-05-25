---
description: STM32 programmer — flash, erase, reset, read memory, option bytes, signing
argument-hint: <subcommand> [args...]
allowed-tools: Bash(stm32 prog:*), Bash(.venv/bin/stm32 prog:*)
---

The user wants a programmer-side operation against the attached STM32. Map the request to a `stm32 prog` subcommand and run it; JSON output is emitted on stdout.

User input: `$ARGUMENTS`

## Subcommand map

Pick the subcommand that matches the intent. Pass through positional args + flags as the user described them.

**Discovery + connect (D-*):**
- `stm32 prog connect [--ur] [--freq N]` — D-001 / D-002 — banner the attached device. Use `--ur` for connect-under-reset (D-011).
- `stm32 prog diagnose-micro` — D-002 SWD recovery ladder (5 modes × 4 freqs).
- `stm32 prog list-probes` — D-005 — enumerate ST-LINK probes.
- `stm32 prog ping-swd` — D-006 — fast aliveness check; exit code 0/1 reflects responding/not.
- `stm32 prog cores` — D-007 — core(s) on the attached device.
- `stm32 prog read-ob` — D-009 — option bytes dump.

**Flash family (F-*):**
- `stm32 prog flash FILE [--address 0xNNNN] [--confirm-inferred-address]` — F-003 / CP-001 — auto-route by file extension (`.elf` / `.hex` need no address). A `.bin` with no `--address` infers `0x08000000` and writes there; that's a destructive write to a *guessed* address, so it needs `--confirm-inferred-address`. Prefer passing an explicit `--address` for `.bin`; confirm the inferred address with the user before adding the flag.
- `stm32 prog flash-data FILE --address 0xNNNN` — F-007 — arbitrary-extension data write.
- `stm32 prog flash-signed FILE [--address]` — F-006 — trusted binary; vendor reports unsupported family if non-N6/MP*.
- `stm32 prog flash-pair BOOT APP [--signed [--sign-unsigned]]` — F-008 / F-009 — dual-image boot+app.
- `stm32 prog flash-external FILE --address 0xNNNN [--loader PATH]` — F-010 — QSPI / external-flash via `.stldr`.
- `stm32 prog flash-bank {1,2} FILE --address 0xNNNN` — F-011.
- `stm32 prog read-flash --address 0xNNNN --size N --output FILE` — F-019.
- `stm32 prog read-mem --address 0xNNNN [--size N]` — F-020.

**Atomic target control (F-016/017/018):**
- `stm32 prog reset [--hard]` — soft or hardware reset.
- `stm32 prog halt` — halt the core.
- `stm32 prog resume` — resume from halt.

**Erase + option bytes:**
- `stm32 prog erase [--with-reset] --confirm-destructive` — F-001 / F-002. Mass erase wipes the **entire flash** and is irreversible; `--confirm-destructive` is required. Confirm with the user first (e.g. via `AskUserQuestion`) before passing it.
- `stm32 prog write-ob NAME=VALUE [...] [--confirm-destructive] [--confirm-irreversible]` — F-021. RDP=0xCC needs `--confirm-irreversible`; any OB write needs `--confirm-destructive`.
- `stm32 prog verify-ob NAME=VALUE [...]` — DIAG-018 — diff current OB against expected.

**Signing (F-013; routes through `prog` per ADR-002 §M1):**
- `stm32 prog sign FILE --la 0xNNNN --type {fsbl|ssbl|teeh|teed|teex|copro} --hv {1|2|2.1|2.2|2.3} [--ep 0xNNNN] [--of 0xNNNN] [--no-key] [--align|--no-align] [-o OUTPUT] [--device-family STM32N…]` — N6 / MP1 / MP2 only.

**Streaming + diagnostics:**
- `stm32 prog swo --freq N [--port N] [--log FILE]` — VCP-007 SWV/SWO capture; NDJSON one ITM record per line.
- `stm32 prog hardfault` — DIAG-001 binary-only fault decode (no debug session).

## Output handling

Every subcommand emits JSON on stdout. Read the JSON, summarise the relevant fields to the user in plain language. On error, stderr carries a JSON envelope with `error_type` + `vcp_marker` / `error_code` / etc. — surface the `message` + `hint` fields.

If the user's request maps to a compound multi-tool flow (build-then-flash, sign-then-flash-bench-test, full CP-* pipelines), tell the user that compound flows ship in Pass 2 and compose atomics manually (e.g. `stm32 build` then `stm32 prog flash`).
