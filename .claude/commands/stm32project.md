---
description: STM32 project generation — STM32CubeMX IOC-to-CubeIDE-project
argument-hint: <subcommand> [args...]
allowed-tools: Bash(stm32 mx:*), Bash(.venv/bin/stm32 mx:*)
---

The user wants to regenerate the CubeIDE project from a `.ioc` file or otherwise work with the CubeMX side of the project. Map the request to a `stm32 mx` subcommand.

User input: `$ARGUMENTS`

## Subcommand map

**Regenerate (MX-001 / CP-008):**
- `stm32 mx generate [IOC] [--output DIR] [--name NAME] [--timeout S]` — open the IOC, regenerate `.cproject` + `Core/` source into `--output`; CubeMX preserves USER CODE BEGIN / END regions across regen.
  - **No IOC path** → autodiscover from `stm32-project.jsonc` (`cubemx.ioc_path`).
  - `--output` defaults to the IOC's parent dir (or descriptor's `cubemx.output_path` if set).
  - `--name` defaults to the IOC stem (or descriptor's `cubemx.project_name`).
  - **Do not** Glob for `*.ioc` or ask the user — just invoke `stm32 mx generate` and let the substrate raise a loud `CubeMXError(cubemx_marker="ioc-missing")` if neither an argument nor a descriptor is present.

## Scope notes (per P-037 cubemx scope cut)

The cubemx module is thin-wrapper-only — substrate invokes the tool, observes external signals (subprocess state, marker filesystem state, log mtime), reports success/failure, hands the log path to the caller on failure. It does NOT parse IOC content or output content.

Out of scope in v1 (`[out]` per RES-022 / P-037):
- **MX-002 / MX-003** — `new_project` / `saveas_and_modify` flows (redundant with MX-001 + CubeIDE GUI).
- **MX-004** — `diff_iocs` (Claude reads files directly; no substrate method).
- **MX-005** — T3 IOC-driven flow.
- **MX-006** — new-project-from-board.
- **B-017** — MCU retarget.

If the user asks for one of these, tell them the workflow is: open CubeMX GUI → edit the IOC → save → run `/stm32project generate IOC` to regenerate the CubeIDE project tree.

## Output handling

`generate` emits a `CubeMXResult` JSON with `success` / `duration_s` / `extensions_used` / `terminated_after_marker` / `log_path` / `output_path`. `success=false` is a normal result (CubeMX error in the log). `CubeMXError(cubemx_marker="ioc-missing")` raises pre-subprocess for missing / wrong-suffix IOC paths. `CubeMXLauncherError` raises when the launcher isn't resolvable. Surface log_path to the user on failure for them to inspect.

CubeMX runs can take several minutes (long_call_s default 60s with up to 3 × 60s extensions when log activity is observed). Don't fret over apparent hangs within the timing window.
