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

## Requirements

- **Python 3.11+**
- **Linux or Windows.** macOS is not supported in v1 (planned based on demand;
  the substrate fails loud with a hint if run on macOS).
- **ST's vendor tools installed** — the substrate drives them, it does not
  bundle them. Install the ones you need from
  [st.com](https://www.st.com/en/development-tools/stm32-software-development-tools.html):
  STM32CubeProgrammer, STM32CubeIDE, STM32CubeMX, the ST-LINK GDB server,
  `arm-none-eabi-gdb`, and (for signed parts) STM32_SigningTool_CLI.
- An **ST-LINK probe** and a board for anything that touches hardware.

## Install

There are two pieces: the **`stm32` CLI / Python package** (which actually
drives the tools) and the **Claude Code plugin** (the slash commands and the
natural-language surface).

### 1. The package (the `stm32` CLI)

```bash
pip install git+https://github.com/EmbedAgents/stm32-substrate.git
```

This installs the `stm32` console command and the `stm32_substrate` Python
library.

> Once published to PyPI, this becomes `pip install stm32-substrate`.

### 2. The Claude Code plugin

```text
/plugin marketplace add EmbedAgents/stm32-substrate
/plugin install stm32-substrate@stm32
```

Or, for local development, point Claude Code straight at a checkout:

```bash
claude --plugin-dir /path/to/stm32-substrate
```

### 3. Tell the substrate where ST's tools live

The substrate resolves each tool by **environment variable → configured path →
`PATH` lookup**, and fails loud (naming the exact key to set) if it can't find
one. Configure paths once in `.claude/stm32-tools.local.jsonc`, or set env vars
such as `STM32_PROGRAMMER_CLI`. See the schema at
`src/stm32_substrate/schemas/stm32-tools.local.schema.json` for every key.

## Usage

Mostly, you talk:

> **You:** flash the build to my Nucleo and reset it
> **Claude:** *(runs `stm32 prog flash … && stm32 prog reset`, reports the result)*

The slash commands stay available when you want to reach for a tool directly:

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

## License

[MIT](LICENSE) © 2026 EmbedAgents
