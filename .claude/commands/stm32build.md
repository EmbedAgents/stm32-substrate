---
description: STM32CubeIDE headless build — preset, options, .cproject edits
argument-hint: [--project PATH] [--preset fast|size|balanced] [--config NAME] [flags...]
allowed-tools: Bash(stm32 build:*), Bash(.venv/bin/stm32 build:*)
---

The user wants to build a CubeIDE project. Map the request to a `stm32 build` invocation and run it.

User input: `$ARGUMENTS`

## CRITICAL — argument shape

The positional slot on `stm32 build` is `<action>` (one of `add-symbol`, `add-lib`, `add-source`, `add-include`, `in-folder`, `named`). **The project path is NOT positional — it MUST be passed via `--project PATH`.** Running `stm32 build /some/path` fails with `error: argument <action>: invalid choice: '/some/path'`. Always use the flag form, or omit the path entirely to let the substrate autodiscover from cwd / descriptor.

## Subcommand map

The `build` group has one verb + several `add-*` shapes mapping to the B-* prompts. Pick by request shape:

**Base build (B-001..B-009):**
- `stm32 build [--project PATH] [--config NAME] [--clean] [--debug-level {0|1|2|3}] [--opt {O0|O1|O2|O3|Os|Ofast|Og}] [--preset {fast|size|balanced}] [--all-configs]`
  - No `--project` → autodiscover from cwd / descriptor.
  - `--preset fast` → `-O3 -ffast-math -funroll-loops -mfpu=...` (FPU via `firmware.device_family`).
  - `--preset size` → `-Os -fdata-sections -ffunction-sections -Wl,--gc-sections`.
  - `--preset balanced` → defaults (no preset).
  - `--all-configs` → modify both Debug + Release configurations (default: active only).

**Symbol / library / source / include edits (B-010..B-014):**
- `stm32 build add-symbol NAME[=VALUE] [NAME...]` — B-010 — preprocessor defines.
- `stm32 build add-lib LIB [LIB...]` — B-011 — linker libs.
- `stm32 build add-source PATH [--target FOLDER]` — B-012 — source file inclusion.
- `stm32 build add-include PATH [PATH...]` — B-013 — header paths.
- `stm32 build in-folder FOLDER ...` — B-014 — restrict to a source folder.

**Named-configuration (B-018 / B-019):**
- `stm32 build named NAME ...` — explicit configuration name (overrides active-only default).

## Output handling

Build emits a `BuildResult` JSON: `success` / `exit_code` / `log_path` / `console_output` / `artifact_path` / `map_path`. `success=false` is a normal result (build failure, not a substrate crash) — surface compile/link errors from `console_output` to the user.

`.cproject` edits are atomic per the project-settings protocol: protocol-level failures (XML rollback) raise `CProjectEditError`; build-level failures keep the change (caller iterates). If the build fails and the user wants to iterate, future B-021 build-fix-loop is Pass-2 / Wave-3 territory — for now, surface the error and let the user direct the next step.

If the workspace is GUI-held, the CLI raises `WorkspaceLockedError` immediately (HIL-mode M-019) — tell the user to close the CubeIDE GUI.
