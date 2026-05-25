---
description: STM32 agent — VCP read/write + cross-tool flows (compounds defer to Pass 2)
argument-hint: <subcommand> [args...]
allowed-tools: Bash(stm32 vcp:*), Bash(stm32 prog:*), Bash(stm32 build:*), Bash(stm32 debug:*), Bash(stm32 mx:*), Bash(.venv/bin/stm32:*)
---

The user wants a VCP operation, or a multi-tool flow (build-then-flash, sign-then-flash-bench-test, etc.). Pick the path:

User input: `$ARGUMENTS`

## VCP intents (VCP-001 / VCP-002 / VCP-003)

VCP is implicit per SB-001 (auto-attach) and SB-002 (auto-reconnect after reset). Direct subcommands available via the umbrella:

- `stm32 vcp tail [--port P] [--baud B] [--last-n N] [--follow] [--timeout S]` — VCP-001 — yield serial lines as text. `--follow` streams until Ctrl-C.
- `stm32 vcp send LINE [--port P] [--baud B] [--terminator T] [--timeout S] [--inter-line-idle-ms MS] [--echo-filter]` — VCP-002 — write a line, collect reply.
- `stm32 vcp reconnect [--port P] [--max-wait S]` — VCP-003 — force-reconnect after a target reset (SB-002 normally handles this lazily).
- `stm32 vcp close` — release the port so external tools (minicom / screen / picocom / Cutecom) can take over (RES-014 Q5).

Multi-probe ambiguity: `VCPAmbiguousProbe` carries `(port, serial_number, board_name)` candidates. Use AskUserQuestion to pick — substrate latches `default_probe_sn` for the rest of the session.

## Compound flows — Pass 2 deferral (per RES-022)

These prompts are inherently multi-tool and ship in Pass 2:

- **CP-001 .. CP-013** — flash-then-test, build-flash-debug, IOC-to-debug end-to-end, etc.
- **B-003 / B-016 / CP-004 / CP-005** — compound facades over build + flash + reset + verify.
- **F-015** — N6 sign + flash + bench-protocol-confirm.

If the user's request matches a compound flow, do NOT attempt a Pass-2 facade. Instead:

1. Identify the atomic steps the user needs.
2. Compose them manually using the per-tool CLIs:
   - Build: `stm32 build ...`
   - Sign (N6/MP only): `stm32 prog sign ...`
   - Flash: `stm32 prog flash ...` or `stm32 prog flash-signed ...`
   - Reset: `stm32 prog reset` (or `stm32 prog reset --hard`)
   - Tail serial: `stm32 vcp tail --follow`
   - Debug attach: `stm32 debug start ELF [--no-halt]`
3. Run each step; report results between steps so the user can correct course.

Tell the user explicitly when a request hits the Pass-2 boundary: "this compound flow ships in Pass 2; running the atomic steps manually so you can see each result."

## T3 prompts (deferred per M-014)

- **B-021** — build-fix loop. Until shipped, surface build errors to the user verbatim and let them direct the fix.
- **DIAG-019 / DIAG-020** — crash classification.
- **DBG-008 / DBG-009 / DBG-011** — Claude-in-loop debug recipes.
- **VCP-004 / VCP-005** — config-diagnose / baud-raise loops.

For these, compose raw reads (`stm32 debug start`, in-session Python calls) and surface the data; don't shortcut to a classifier.
