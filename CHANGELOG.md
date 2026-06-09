# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-06-09

First public release. STM32 development by talking to Claude Code in plain
language, backed by ST's own toolchain.

### Added

- **Python library** (`stm32_substrate`) wrapping six ST vendor CLIs —
  STM32CubeProgrammer, STM32CubeIDE, STM32CubeMX, ST-LINK gdbserver,
  arm-none-eabi-gdb, and STM32_SigningTool_CLI — plus a USB virtual COM port
  reader. One class per tool, constructed from a dependency-injected
  `SubstrateContext`; sync public surface; `@dataclass(frozen=True)` results;
  a three-layer error hierarchy (`SubstrateError → ToolError → per-tool`).
- **`stm32` CLI** — subcommand groups for terminal use: `stm32 prog …`,
  `stm32 build …`, `stm32 debug …`, `stm32 mx …`, `stm32 vcp …`. Every command
  emits JSON; errors surface as a structured envelope, never a raw traceback.
  Each subcommand takes its primary target positionally (`prog flash FILE`,
  `build /path/to/proj`, `debug start [ELF]`, `mx generate [IOC]`), matching
  how agents and humans naturally invoke CLI tools; `stm32 build` also accepts
  the project via `--project PATH`.
- **Five Claude-Code slash commands** — `/stm32prog`, `/stm32build`,
  `/stm32debug`, `/stm32project`, `/stm32agent` — routing natural-language
  intent to the CLI. Each maps 1:1 to a library operation.
- **Flash / erase / read / verify / option-byte / signing** flows
  (STM32CubeProgrammer + Signing Tool), with explicit confirmation gates on
  every destructive operation (mass erase, inferred-address flash, option-byte
  and RDP writes).
- **Headless build** (STM32CubeIDE) with preset and per-flag `.cproject` edits,
  atomic edit/rollback, and a "Nothing to build" no-op guard. A build path with
  no `.project` file (typically the repo root of an ST-example-shaped tree)
  resolves through the descriptor: when `build.project_path` lands strictly
  under the given path and is itself importable, that project is built (logged
  at INFO); any other non-importable path raises a `ConfigurationError` whose
  hint names the descriptor-resolved path, instead of handing Eclipse a
  directory it fails on with `Project: file://… can't be found!`.
- **Project generation** (STM32CubeMX) from an IOC to a CubeIDE project, behind
  a bounded sync facade.
- **Debug recipes** (ST-LINK gdbserver + arm-none-eabi-gdb) — one-shot register,
  peripheral (SVD-decoded), memory, callstack, and snapshot reads; a
  `decode-hardfault` recipe that composes the raw fault bundle for Claude to
  classify (the substrate captures, it does not interpret).
- **USB VCP reader** for tailing / round-tripping serial output.
- **Device resolution** software-validated across the full installed Cube SVD
  catalog (family→core mapping, SVD lookup, peripheral decode), not just
  bench-tested boards.
- **Cross-platform**: Linux and Windows are first-class. macOS is not supported
  in v1 (planned based on demand) — `SubstrateContext.from_environment()` fails
  loud with a hint on macOS.
- Package-bundled JSON Schemas (2020-12) validated at config load.

[0.1.0]: https://github.com/EmbedAgents/stm32-substrate/releases/tag/v0.1.0
