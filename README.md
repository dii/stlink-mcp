# stlink-mcp

An MCP server that lets an AI coding agent program, run and inspect an STM32
target attached to your development machine over ST-LINK.

It runs locally, next to the board. It drives the STM32CubeCLT command line
tools that you already have installed.

## Why this exists

You can point an agent at `STM32_Programmer_CLI` and let it work things out. The
problem is that these tools report success for things they did not do.

Three real examples, all from one afternoon of bringing up an STM32N6:

* `STM32_Programmer_CLI -s <address>` printed "Start operation achieved
  successfully" while the program counter never left the boot ROM.
* `ST-LINK_gdbserver` printed "Failed to halt target". The core was fine. The
  server was talking to the wrong debug access port.
* A GDB load against a target that was already running completed cleanly and
  changed nothing, so the previous program carried on and its output looked
  like a successful new run.

An agent driving those tools directly inherits every one of those lies, and then
writes confident, wrong conclusions into your codebase. That is worse than no
tooling, because it costs you the time and leaves bad documentation behind.

So the rule here is simple:

> **A tool reports success only after checking that the thing it was asked to do
> actually happened.**

`run_firmware` does not report success because GDB exited zero. It reports
success because the target produced console output. When it cannot verify a
claim, it says so in the result instead of implying it checked.

## Status

Alpha, and honest about it. The tool surface works and is used daily against an
STM32N6. Only one family is characterised so far. Everything else falls back to
generic behaviour, which covers identification and memory reads but not
RAM execution or external flash programming, because those addresses are
family specific and this project would rather have a gap than a guess.

## Requirements

* [STM32CubeCLT](https://www.st.com/en/development-tools/stm32cubeclt.html).
  It bundles STM32CubeProgrammer, the GDB server, the Arm toolchain and the
  signing tool. Found automatically under `/opt/st`, `~/st` or the macOS default;
  override with `STLINK_MCP_CLT_ROOT`.
* Python 3.11 or newer.
* An ST-LINK, which on most development boards is on the board itself.
* On Linux, membership of the `dialout` group for the serial console, and ST's
  udev rules for the probe (STM32CubeCLT installs these).

## Install

```sh
uv sync
```

## Use it from an MCP client

```json
{
  "mcpServers": {
    "stlink": {
      "command": "uv",
      "args": ["--directory", "/path/to/stlink-mcp", "run", "stlink-mcp"]
    }
  }
}
```

## Tools

| tool | what it does | how it verifies |
| --- | --- | --- |
| `board_state` | Identifies the target, the probe, and the family's quirks | Tries every access port, since a wrong port and an absent target look identical |
| `run_firmware` | Loads an ELF into RAM and starts it | Captures console output from before the target resumes |
| `flash_firmware` | Programs a signed image into boot memory | Requires the programmer to confirm the download completed |
| `read_memory` | Reads 32-bit words | Fails loudly if nothing was read |
| `core_registers` | Reads the core registers. Halts the core | This is the ground truth when there is no console |
| `capture_console` | Captures serial output | |

`run_firmware` is the one to use while developing. It is faster than programming
flash, and an image placed straight into RAM by a debugger bypasses the boot ROM,
so no signing is involved even on parts that require signed images to boot.

## Configuration

| variable | meaning |
| --- | --- |
| `STLINK_MCP_CLT_ROOT` | STM32CubeCLT install directory |
| `STLINK_MCP_CONSOLE` | Serial device, if the guess is wrong |
| `STLINK_MCP_CONSOLE_BAUD` | Console baud rate, default 115200 |
| `STLINK_MCP_GDB_PORT` | GDB server port, default 61234 |

## Adding a family

Add one entry to `src/stlink_mcp/quirks.py` and map its device ID. Fill in only
what you have verified on hardware. A missing value fails loudly; a wrong value
fails silently, which is the failure mode this project exists to remove.

The STM32N6 entry is a good illustration of why the table is needed at all: that
family has no internal flash, its boot ROM authenticates the image it loads out
of external flash, and its debug is on access port 1 rather than 0.

## Limitations

* It does not build your firmware. That is your build system's job.
* It does not sign images, because signing needs keys and options that belong to
  your project rather than to a programmer.
* Only ST-LINK with STM32CubeCLT is supported today. The backend is behind a
  seam so OpenOCD could be added, but that work has not been done.
* It is not affiliated with STMicroelectronics.

## Licence

MIT. See [LICENSE](LICENSE).
