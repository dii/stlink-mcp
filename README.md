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
success because the target produced console output.

Every result therefore has one of three states, and `ok` is never set by hand:

| `status` | `ok` | meaning |
| --- | --- | --- |
| `verified` | `true` | checked, and `verified_by` names the observation |
| `unverified` | `false` | may well have worked, but nothing watched it |
| `failed` | `false` | it did not work, and `error` says why |

`unverified` is the one that matters, and it is deliberately not `ok`. A run
nobody watched is not a success however likely it is to have worked, so
`unverified_because` is there to tell it apart from a real failure. Reporting
that case as success is exactly the harm this project exists to prevent, and an
earlier version of this tool did it: `ok: true` alongside `verified_by:
"nothing"`.

## Status

Alpha, and honest about it. The tool surface works and is used daily against an
STM32N6. Only one family is characterised so far. Everything else falls back to
generic behaviour, which covers identification and memory reads but not
RAM execution or external flash programming, because those addresses are
family specific and this project would rather have a gap than a guess.

## Requirements

* [STM32CubeCLT](https://www.st.com/en/development-tools/stm32cubeclt.html).
  It bundles STM32CubeProgrammer, the GDB server and the Arm toolchain. Found
  automatically under `/opt/st`, `~/st`, `/usr/local/st` or the macOS default;
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
| `board_state` | Identifies the target, the probe, and the family's quirks | Tries every access port, since a wrong port and an absent target look identical. Reports `unverified` when a held server means it is answering from cache |
| `run_firmware` | Loads an ELF into RAM and starts it | Captures console output from before the target resumes. `failed` if the GDB batch did not reach its end markers, since then the load did not happen and any output belongs to the previous image |
| `flash_firmware` | Programs a signed image into boot memory | The programmer's own report that the download completed. Nothing is read back, and `verified_by` says so |
| `read_memory` | Reads 32-bit words without disturbing the target | The words themselves. Connects hotplug-only, and refuses rather than resetting to reach a closed access port |
| `core_registers` | Reads the core registers. Halts the core | The registers themselves. Reports `target_was_reset` when reaching the core needed a reset |
| `capture_console` | Captures serial output | Reads the port mapped to that probe's serial, and refuses if it cannot resolve one |
| `release_probe` | Frees a held GDB server | `unverified` if nothing was held, since then nothing was released |
| `list_probes` | Lists attached probes and their consoles | Deduplicates the tool's own double listing. Works while other probes are held |

### Why the GDB server is kept alive

`run_firmware` starts a GDB server and **holds it** rather than tearing it down.
That is not laziness. On the STM32N6, once firmware has run the debug access port
cannot be re-acquired once a typical image has run, and only removing power gets
it back. Holding the server is therefore the difference between one power cycle
per session and one per run.

**That is a property of the image, not of the part**, which is worth stating
plainly because this project got it wrong for two days and wrote the wrong
version into its own docs. The cause is one line at the end of ST's
`system_stm32n6xx_fsbl.c`:

```c
RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;   /* deactivate SYSCFG clock */
```

`SystemInit` turns the SYSCFG clock on early so it can write `INITSVTORCR`, then
turns it off again on the way out. On the N6 that clock is what keeps debug
access port 1 alive. Remove that single write and the port survives: four
consecutive load-and-run cycles with no power cycle, against the stock image
closing it on the very next run of the same board.

Found by bisecting `SystemInit`, which also eliminated the boot ROM handoff (a
bare image with no startup chain leaves the port open), the startup assembly,
TrustZone and the SAU, `INITSVTORCR`, the PWR supply block, and every peripheral
reset in there.

Two methodology notes, both earned the hard way. Confirm a probe is free **by
port** with `ss -ltnp`, never by process name: Linux truncates process names to
fifteen characters, so `pgrep ST-LINK_gdbserver` matches nothing at all and reads
exactly like a clean control. And connect-under-reset (`mode=UR`) is not a way
out here; it fails on a healthy board too.

An already established connection keeps working across repeated load-and-run
cycles, **provided the target is reset before every load**. That precondition is
not optional and is easy to lose: the server's own `--halt` resets once, when the
server starts, so without an explicit reset each image inherits the previous
run's live peripherals and interrupt state, and faults during start-up in a way
that reads as a firmware bug.

The cost is that the server owns **that** probe, so `read_memory`,
`core_registers` and `flash_firmware` cannot reach it until you call
`release_probe`. Other probes are unaffected, and so is enumeration.

`run_firmware` is the one to use while developing. It is faster than programming
flash, and an image placed straight into RAM by a debugger bypasses the boot ROM,
so no signing is involved even on parts that require signed images to boot.

### More than one board

Every tool takes a `probe_serial`. With several boards attached, omitting it is
an **error**, not a guess: probe order is not stable across replugs, and loading
firmware onto the wrong board is a failure that looks like success.

`list_probes` also reports which serial port belongs to which probe. That mapping
is not positional, so pairing probes with `/dev/ttyACM*` by order gets it wrong,
and `run_firmware` uses each board's own console to verify it. Each board gets
its own GDB server on its own port.

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
* Only ST-LINK with STM32CubeCLT is supported. There is no abstraction layer
  waiting for a second backend: adding OpenOCD would mean introducing one first.
* Linux is the tested platform. macOS should work, since the toolchain search
  and the console device patterns both cover it, but it has not been run there.
  Windows is not supported: the executable names and device paths are wrong for
  it throughout.
* Exactly one device family, STM32N6, is verified on hardware. Everything else
  falls back to a generic entry that can identify a target but will refuse to
  run firmware on it, because it has no verified RAM layout to load into.
* It is not affiliated with STMicroelectronics.

## Licence

MIT. See [LICENSE](LICENSE).
