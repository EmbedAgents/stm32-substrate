# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-06-12

Naming release: every identifier we publish is now brand-first — nothing
leads with ST's mark. **STM32 remains a registered trademark of
STMicroelectronics International N.V.; this project is independent and
community-driven** (see the README disclaimer). Functionally identical to
0.2.0 — no behavior changes.

### Changed

- **Distribution renamed `stm32-substrate` → `embedagents-stm32`**, and the
  import package moved into a PEP 420 namespace:
  `import stm32_substrate` → **`from embedagents import stm32`**. Future
  sibling tools (e.g. `embedagents-esp32`) install into the same
  `embedagents` namespace from their own distributions; `pip install
  EmbedAgents` (the meta-package) pulls the whole family.
- **Claude-plugin renamed `stm32-substrate` → `embedagents-stm32`** and the
  marketplace renamed `stm32` → `embedagents`: the install line is now
  `/plugin install embedagents-stm32@embedagents`. **Upgrading from an
  earlier install:** `/plugin uninstall stm32-substrate`, then reinstall —
  and if you pip-installed the old package, `pip uninstall stm32-substrate`
  first (both distributions own the `stm32` console script).
- **Unchanged on purpose:** the `stm32` CLI binary (every subcommand, flag,
  and JSON contract), the five slash commands (`/stm32prog`, `/stm32build`,
  `/stm32debug`, `/stm32project`, `/stm32agent`), the GitHub repo, the
  project-descriptor format, and the `.stm32-substrate/` workspace
  convention. How you *use* the tool is identical.
- Log-record prefixes follow the package: `stm32_substrate.*` →
  `embedagents.stm32.*`.
- New `Release` workflow publishes to PyPI via Trusted Publishers (OIDC) on
  GitHub Releases; CI and the release pipeline both assert the wheel ships
  no `embedagents/__init__.py` (the namespace stays mergeable).

## [0.2.0] — 2026-06-12

Hardening release. A comprehensive 10-dimension adversarial audit (129
independently-verified findings, 66 fixed this cycle) plus end-to-end
re-validation of the eval suite on `claude-fable-5` against real hardware —
39 of 41 runnable scenarios green live on NUCLEO-L476RG + STM32N6570-DK,
including the full break→diagnose→fix→flash→verify loop on silicon.

### Added

- **`stm32 prog svd`** — resolve the attached device's `.svd` file from a
  fresh banner (D-008).
- **Keyless N6 signing**: `stm32 prog sign --no-key` and
  `flash-pair --signed --sign-unsigned --no-key` — one-call sign+flash of an
  unsigned boot/app pair, hardware-validated on the N6570-DK.
- `stm32 prog flash` now routes `.axf` / `.s19` / `.srec` like `.elf`
  (address-embedded formats need no `--address`).
- `vcp tail --follow --timeout S` — an explicit wall-clock bound on follow
  mode (bare `--follow` keeps its until-Ctrl-C contract); `vcp send
  --terminator` decodes `"\r\n"`-style escapes.
- All nine `debug.*` runtime-default knobs in `stm32-runtime-defaults` are
  now honored (timeouts, port walk, handshake budgets) — previously several
  were silently dead.
- Every `Path`-typed public entry point accepts `str | Path`, and
  descriptor-configured relative paths resolve against the project root, not
  the process CWD.
- Test depth: an MI-record corpus of 23 real `arm-none-eabi-gdb` MI3 captures
  with round-trip tests, and a hardware test reading sticky fault registers
  (CFSR/HFSR) from a genuinely faulted target.

### Changed

- **Debug read recipes attach without reset and halt in place** — sticky
  fault registers and live peripheral state now survive into the dump. The
  previous reset-first behavior wiped the very fault evidence
  `decode-hardfault` exists to read.
- **ST-LINK gdbserver control vocabulary corrected**: `mi-async` is enabled
  before target-select, halt/resume route through MI-level commands (the
  gdbserver rejects OpenOCD-style `monitor reset halt`), `halt()` picks the
  safe interrupt form for faulted cores, and `snapshot()` tolerates an
  unwindable faulted stack. This latent cluster would have broken
  `start_session(halt=True)` on all real hardware.
- **Compound flows are Claude-composed from atomics**: no dedicated compound
  API ships; `/stm32agent` carries the composition contract (build→flash→
  serial-verify, sign→flash, build-fix chains — live-validated on hardware).
- **`/stm32debug` rewritten lean** (8.7 KB → 3.3 KB), validated by a live
  A/B on `claude-fable-5`: pass rates held with zero lean-attributable
  failures, at lower cost per scenario.
- One-shot debug CLI timeouts are real deadlines — a silent gdbserver or a
  never-hit breakpoint can no longer hang an invocation forever.
- gdb `^error` result records now raise a typed `GDBError("command-error")`
  instead of being silently treated as success (a typo'd variable name used
  to return an empty value with exit 0).
- Multi-probe benches: the `STM32_PROGRAMMER_DEFAULT_SN` pin idiom is
  documented across the command surfaces (`prog`/`debug` one-shots do not
  board-match the descriptor; the first-probe fallback is silent).
- Default eval model is `claude-fable-5`; 51 replay scenarios, with the four
  T3 transcripts re-recorded against real problem states (a faulted target, a
  broken build, a 4× baud mismatch).

### Fixed

- CubeMX regeneration on a previously-generated project no longer reports
  success on a nonzero exit, and the JVM doing the actual generation is
  terminated with its launcher instead of being orphaned to keep writing the
  output tree.
- Workspace safety: CubeIDE GUI-lock detection now actually works on both
  OSes (it was dead on both, allowing cleanup to delete a live GUI's
  `.metadata`); destructive workspace cleanup runs under the lock; the
  Windows `.location` URI decode no longer spuriously purges project
  metadata.
- gdb/MI robustness: spawn/attach failures tear down both processes (no more
  leaked gdbserver holding the probe), truncated MI records raise a typed
  `protocol-violation`, multi-block memory reads are stitched by declared
  offset and truncated at unreadable holes, and user-supplied breakpoint
  locations / expressions / monitor strings are MI-quoted (no command
  injection via an embedded newline).
- VCP: streaming replies are bounded by wall clock (fast-printing firmware
  could block `send` unboundedly), CLI `reconnect` polls enumeration up to
  `--max-wait`, a dead drain thread flags the reader stale instead of
  zombifying the port, `COM*` names no longer fail the POSIX path probe on
  Windows, and follow-mode no longer drops lines that arrive during the
  backlog snapshot.
- CubeProgrammer: vendor-CLI timeouts keep their diagnosis (was "exited with
  code -1"), `tail_swo` merges stderr and raises on failure instead of
  yielding a silent empty stream, and an RDP level-2 write no longer
  read-backs the now-locked target and falsely reports the irreversible
  operation as failed.
- Configuration: a set-but-broken tool-path env var or a typo'd explicit
  config path raises a loud `ConfigurationError` instead of silently falling
  through to PATH / built-in defaults.
- `callstack --full` returns populated frames; build-option values are
  validated before being written into `.cproject` (invalid forms used to be
  written verbatim and reported as success); plus ~25 further audit fixes
  (full per-finding ledger in the development repo).

### Security

- **Destructive verbs are never pre-authorized**: the slash commands'
  `allowed-tools` no longer match `stm32 prog erase` / `write-ob` (they now
  always raise a permission prompt — the human-in-the-loop gate), and
  `/stm32debug` no longer pre-authorizes arbitrary `python`. All five
  command files frame captured device/project output as untrusted data, not
  instructions.
- CI: least-privilege `permissions:` block, actions pinned by commit SHA,
  and a packaging job that builds sdist+wheel and asserts their contents;
  the sdist no longer ships the test tree.

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
