# CLAUDE.md — STM32 substrate

Guidance for Claude Code (and human contributors) working **in this repository**.
End users who install the plugin don't need this file — it's for people editing
the substrate's own code.

## What this project is

The STM32 substrate is a deterministic Python library that wraps six ST vendor
CLIs — STM32CubeProgrammer, STM32CubeIDE, STM32CubeMX, the ST-LINK gdbserver,
`arm-none-eabi-gdb`, and `STM32_SigningTool_CLI` — plus a USB virtual COM port
reader. It exposes three layered surfaces:

1. a **Python library** (`stm32_substrate`),
2. a **`stm32` CLI** built on the library, and
3. **five Claude-Code slash commands** (`/stm32prog`, `/stm32build`,
   `/stm32debug`, `/stm32project`, `/stm32agent`) that route natural-language
   intent to the CLI.

Claude Code is the agent that drives the substrate; for v1 a human is at the
keyboard (hardware-in-the-loop). The substrate plumbs tools, captures outputs,
and reports outcomes. It does **not** synthesize hardware configurations from
natural language, does not orchestrate without a human in the loop, and does not
interpret vendor tool output — Claude reads logs and decides.

## Hard rules

1. **HIL-mode v1.** The user is watching. Ambiguity raises with a hint. No silent
   retries, no long waits, no auto-recovery. Destructive operations
   (mass erase, inferred-address flash, option-byte / RDP writes) require an
   explicit `confirm_destructive=True` (library) or `--confirm-…` flag (CLI).

2. **Substrate captures, doesn't interpret.** No typed parsers for vendor
   build / log / fault prose. Result types carry outcomes + paths, not
   interpretations. Parse structured formats (banners, hex dumps, gdb-MI
   records, register/peripheral dumps); never parse interpretive vendor prose.
   `decode-hardfault`, for example, returns the *raw* fault bundle — Claude
   classifies the fault, the substrate encodes no rule.

3. **Linux + Windows are first-class.** macOS is not supported in v1
   (`SubstrateContext.from_environment()` fails loud with a hint on macOS).
   OS-specific operations route through `stm32_substrate/platform/*` wrappers
   (`acquire_exclusive_lock`, `process_alive`, `terminate_process`); `os.kill` /
   `signal` / `fcntl` / `msvcrt` / `winreg` never appear in business-logic code.
   Fixtures and tracked files are LF-only (enforced via `.gitattributes`).

4. **API conventions.** `SubstrateContext` dependency injection (no globals).
   Sync-only public surface (CubeMX's multi-minute generate is hidden behind a
   sync facade). `@dataclass(frozen=True)` result types (no pydantic). A
   three-layer error hierarchy (`SubstrateError` → `ToolError` → per-tool
   subclass with a `<tool>_marker` field). jsonschema config validation is
   mandatory at load (opt out only via `STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION=1`,
   which emits a WARNING).

## Layout

```
src/stm32_substrate/
  context.py            # SubstrateContext + from_environment() (tool/path/schema resolution)
  errors.py             # three-layer error hierarchy
  cubeprogrammer/       # flash / erase / read / verify / option bytes / discovery
  cubeide/              # headless build, .cproject edits, presets, workspace
  cubemx/               # IOC → CubeIDE project generation (sync facade)
  debug/                # ST-LINK gdbserver + gdb recipes, SVD lookup/decode
  vcp/                  # USB virtual COM port reader
  signing/              # STM32_SigningTool_CLI wrapper (N6 / MP1 / MP2 parts)
  platform/             # OS-specific locking / process wrappers
  cli/                  # `stm32` console entry point + subcommand groups
  schemas/              # package-bundled JSON Schemas (2020-12), loaded at runtime
.claude/commands/       # the five slash-command definitions (the plugin surface)
.claude-plugin/         # plugin.json + marketplace.json
tests/                  # pytest suite + committed fixtures/descriptors
```

Each vendor tool gets one class constructed with a dependency-injected
`SubstrateContext`. Public methods are sync; results are frozen dataclasses;
errors raise from the hierarchy above. The same operation has one contract
across all three surfaces — adding an operation means: add to the library,
register a CLI handler, and (if it's a new top-level user intent) add a
slash-command file.

Two intentional routing exceptions: signing routes through `/stm32prog`
(it's conceptually a sub-step of programming); VCP and cross-tool flows route
through `/stm32agent`.

## Tool path resolution

Each tool resolves by **environment variable → configured candidate paths →
`shutil.which` on the executable name → loud `ConfigurationError`** naming the
exact JSON key / env var to set. Configure paths in
`.claude/stm32-tools.local.jsonc` (see the committed `.example`), or set env vars
such as `STM32_PROGRAMMER_CLI`.

## Tests

pytest, with these markers:

| Marker | Needs | Runs |
|---|---|---|
| `unit` (default) | nothing — CLIs mocked | every commit / in CI |
| `smoke` | real vendor CLIs, no hardware | pre-push |
| `smoke_with_probe` | an ST-LINK probe, no specific board | bench |
| `hardware` | an attached NUCLEO/DISCO board | bench |
| `hardware_destructive` | attached board + explicit opt-in | bench |
| `eval` | Claude Code SDK (slow, costs money) | opt-in via `-m eval` |

```bash
pip install -e .[dev]
pytest                       # unit suite — runs anywhere, no hardware
pytest -m eval               # replay eval transcripts (offline, free, deterministic)
```

The hardware layer serializes through a session-scoped probe fixture (the SWD
probe is a singleton). Hardware tests skip cleanly when the required board or
vendor CLI is absent — a fresh clone runs the unit suite green without any
hardware or the (gitignored) ST firmware fixture trees.

## Conventions

- **Schemas** are JSON Schema 2020-12, package-bundled under
  `src/stm32_substrate/schemas/`, and loaded at runtime via
  `importlib.resources`. They ship with the wheel.
- **Commits**: descriptive subject + body; small, focused commits preferred.
- **Documentation surface**: keep it lean. This file, the README, and the
  CHANGELOG are the prose surface; code is documented in docstrings.
