# stm32-substrate

**STM32 development, by talking to Claude.**

STM32 is one of the most widely used 32-bit microcontroller families in the
world. ST ships a strong, free toolchain for it — CubeMX to configure a chip,
CubeIDE to build and debug, CubeProgrammer to flash it, and a handful more. The
tools are capable, but they're a lot to learn, they don't talk to each other,
and none was built to be driven by an AI coding agent.

This plugin changes that. It lets you do STM32 development by talking to Claude
Code in plain language:

> "Build my project."
> "Flash it to the board."
> "Read the RCC clock register."
> "Why is it crashing?"

Claude calls ST's tools for you and reports back. You stop juggling six separate
applications and just describe what you want done.

**The promise: once it's installed, you don't have to remember anything about
it.** No flags to memorize, no syntax to keep in your head. The five slash
commands are there for when you'd rather point at a tool directly — but most of
the time you just talk.

## What Claude can do for you

**You ask, Claude does:**

- **Configure** a chip and generate project code (STM32CubeMX)
- **Build** firmware (STM32CubeIDE)
- **Flash, erase, read, and verify** device memory (STM32CubeProgrammer)
- **Sign** secure binaries for the chips that require it (STM32 Signing Tool)

**Claude reaches for these on its own:**

- **Read firmware state over the debugger** while diagnosing a crash or working
  a fix (ST-LINK GDB server). For hands-on stepping you'll still want the
  CubeIDE GUI — this path is built for Claude to *read* what your firmware is
  doing, not to replace your debugger.
- **Read the serial port** when it needs to see what your firmware is printing,
  or confirm a board is alive (USB virtual COM port).

The substrate is family-agnostic: if ST's tools support the chip, Claude can
drive them against it — from an 8 MHz low-power part up to the latest Cortex-M85
and NPU-equipped silicon.

## Install — 30 seconds

**Requirements:** [Claude Code](https://docs.claude.com/en/docs/claude-code), [Python 3.11+](https://www.python.org/downloads/), [Git](https://git-scm.com/), and [ST's STM32 tools](https://www.st.com/en/development-tools/stm32-software-development-tools.html) — install the ones you need; the substrate drives them, it doesn't bundle them. Linux or Windows (macOS isn't supported yet). An ST-LINK probe and a board for anything that touches hardware.

### Step 1: Install on your machine

Open Claude Code and paste this. Claude does the rest.

> Install the STM32 substrate: run `pip install git+https://github.com/EmbedAgents/stm32-substrate.git` to get the `stm32` CLI, then register the plugin with `claude plugin marketplace add EmbedAgents/stm32-substrate` and `claude plugin install stm32-substrate@stm32`. Then ask me which ST tools I have installed (STM32CubeProgrammer, CubeIDE, CubeMX, the ST-LINK GDB server, arm-none-eabi-gdb, the Signing Tool) and write a `.claude/stm32-tools.local.jsonc` that points at them.

That installs the `stm32` CLI + `stm32_substrate` library and registers the five `/stm32*` slash commands. Restart Claude Code if the commands don't show up right away.

Prefer to do the plugin half by hand? Run `/plugin marketplace add EmbedAgents/stm32-substrate` then `/plugin install stm32-substrate@stm32`. And once it's on PyPI, the package step is simply `pip install stm32-substrate`.

### Step 2: Point it at your ST tools

The substrate finds each tool by **environment variable → `.claude/stm32-tools.local.jsonc` → your `PATH`**, and fails loud — naming the exact key to set — if it can't. Claude can write that file for you in Step 1; the [schema](src/stm32_substrate/schemas/stm32-tools.local.schema.json) lists every key. Set it once and you're done.

Then just talk:

> **You:** build my project and flash it to the Nucleo
> **Claude:** *(runs `stm32 build` then `stm32 prog flash …`, reports back)*

## Usage

You mostly just talk, like in Step 1 — *"flash the build to my Nucleo and reset
it"* and Claude runs the tools. The five slash commands stay available when
you'd rather point at one directly:

| Command | What it does | ST tool behind it |
|---|---|---|
| `/stm32project` | Configure a chip and generate project code | STM32CubeMX |
| `/stm32build` | Build your firmware | STM32CubeIDE |
| `/stm32prog` | Flash, erase, read/verify memory, sign secure binaries | STM32CubeProgrammer + Signing Tool |
| `/stm32debug` | Read firmware state over the debugger during a fix | ST-LINK GDB server |
| `/stm32agent` | Read the serial port and run cross-tool flows | VCP reader |

Each surface — the library, the `stm32` CLI, and the slash commands — maps to
the same operations, so anything you can ask for in chat you can also script.

### As a Python library

```python
from stm32_substrate.context import SubstrateContext
from stm32_substrate.cubeprogrammer import CubeProgrammer

ctx = SubstrateContext.from_environment()
prog = CubeProgrammer(ctx)
banner = prog.connect()
print(banner.device_name, banner.flash_size_kb)
```

## Safety

Destructive operations are gated, not silent. Mass erase, flashing a `.bin` to
an inferred address, and option-byte / RDP writes all require an explicit
confirmation (`confirm_destructive=True` in the library, a `--confirm-…` flag on
the CLI). The substrate captures tool output and outcomes — it doesn't
second-guess them — and surfaces failures as structured errors with an
actionable hint rather than a raw traceback.

## Uninstall

Remove the plugin and the package — nothing else is left behind.

```bash
# Remove the Claude Code plugin + its marketplace entry
claude plugin uninstall stm32-substrate
claude plugin marketplace remove stm32

# Uninstall the Python package / `stm32` CLI
pip uninstall stm32-substrate
```

If you created one, delete your `.claude/stm32-tools.local.jsonc`. The substrate
leaves nothing else on your machine — no caches, no dotfiles, no daemons.

## Troubleshooting

- **`ConfigurationError: … not found`** — the substrate couldn't locate a tool.
  The error names the exact env var / JSON key to set. Point it at the tool in
  `.claude/stm32-tools.local.jsonc`, or export the named variable (e.g.
  `STM32_PROGRAMMER_CLI`).
- **The `/stm32*` commands don't show up** — restart Claude Code, then check
  `claude plugin list`. Re-run Step 1 if the plugin isn't listed.
- **`macOS is not supported`** — v1 runs on Linux and Windows only; macOS is
  planned based on demand.
- **Probe not found / target-connect errors** — check the ST-LINK cable and board
  power, and make sure nothing else holds the probe. Only one debug client can own
  the SWD probe at a time, so close the CubeIDE GUI debugger or any other running
  gdbserver first.
- **A destructive operation was refused** — that's the safety gate working. Re-run
  with `confirm_destructive=True` (library) or the matching `--confirm-…` flag
  (CLI).
- **Schema validation failed at startup** — fix the reported field in your config.
  For a one-off debug bypass, set `STM32_SUBSTRATE_SKIP_SCHEMA_VALIDATION=1` (it
  warns loudly).

## Privacy & Telemetry

**Nothing is sent anywhere, ever.** The substrate has no telemetry, no analytics,
no crash reporting, no usage tracking, and no phone-home — none. It makes no
network calls at all.

It runs entirely on your machine: it shells out to ST's local vendor CLIs and
reads your serial port, and that's the whole story. No account and no API key are
needed to use it. Its only dependencies are `jsonschema` and `pyserial`, neither
of which contacts a server.

The only things that ever touch the network are tools you already run and control
— ST's own installers (when *you* download them) and Claude Code itself (your
conversation with Claude, under Anthropic's terms). The substrate adds zero
network surface of its own.

## License

[MIT](LICENSE) © 2026 EmbedAgents
