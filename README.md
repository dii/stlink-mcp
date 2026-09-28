# stlink-mcp

An MCP server that lets an AI coding agent program, run and inspect an STM32
target over ST-LINK. It runs locally, next to the board, and drives the
STM32CubeCLT command line tools you already have installed.

## Why you would want this

Firmware development is a loop: change the code, build, load it onto the
board, watch the console, read the fault registers when it hangs, change the
code again. An agent that can only edit files does the first step and then
waits for you to do the rest and report back. Every result reaches it as your
summary of what you saw, and it reasons from that.

With this server attached, the agent runs the loop itself. It identifies the
board, loads the image, captures the console, reads the registers and the
fault status, and decides what to change next from what the target actually
did. You supervise the conclusions instead of relaying the evidence.

## Why the reports have to be true

That loop is only as good as what the tools tell the agent. A developer
catches a tool that overstates, because they hold information the tool never
gave them: the LED that did not change, console text that looks like the
previous build, the board in their hand. An agent has none of that. The text a
tool returns is its entire model of the hardware, so a tool that reports
success for something it did not do makes the agent confidently wrong, and the
agent then writes that wrongness into code, commit messages and documentation,
where it costs far more than the failed operation did.

ST's command line tools do overstate. Three examples from one afternoon with
an STM32N6:

* `STM32_Programmer_CLI -s <address>` printed "Start operation achieved
  successfully" while the program counter never left the boot ROM.
* `ST-LINK_gdbserver` printed "Failed to halt target". The core was fine; the
  server was on the wrong debug access port.
* A GDB load against a running target completed cleanly and changed nothing,
  so the previous program carried on and its output read as a successful new
  run.

An agent driving those tools inherits every one of them. So the rule here is:

> **A tool reports success only after checking that the thing it was asked to do
> actually happened.**

`run_firmware` reports success because the target produced console output, not
because GDB exited zero. Every result has one of three states, and no tool sets
`ok` by hand:

| `status` | `ok` | meaning |
| --- | --- | --- |
| `verified` | `true` | checked, and `verified_by` names the observation |
| `unverified` | `false` | may well have worked, but nothing watched it |
| `failed` | `false` | it did not work, and `error` says why |

`unverified` is deliberately not `ok`: a run nobody watched is not a success,
however likely it is to have worked. `unverified_because` says what was not
checked, so it is not mistaken for a failure.

## Who this is for

Use it if your board has an ST-LINK, STM32CubeCLT is already installed, and
you want an agent whose every report names what was observed on the target.
It is deliberately small: ten tools, and no breakpoints, stepping, RTT or
memory writes.

Use [embedded-debugger-mcp](https://github.com/Adancurusul/embedded-debugger-mcp)
if you need breakpoints, single-stepping, RTT, a J-Link or DAPLink probe, a
target that is not an STM32, a readback check after flashing, or Windows.
[Related projects](#related-projects) has the comparison.

## Status

Alpha. Two families are measured on hardware, STM32N6 and STM32H7. A further
99 device IDs are seeded from STMicroelectronics' own device databases on your
disk, and every value from that source is labelled as such. Four device IDs
(`0x42B`, `0x42C`, `0x472`, `0x49A`) are refused rather than guessed, because
their databases name several separate SRAM banks; `board_state` names the
addresses it would not choose between, and a board file settles it. Anything
else unrecognised falls back to identification and memory reads only.

Versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) and are
read from the git tag; a build past the last tag reports the tag plus the number of
commits since it, as in `0.1.0+3`. The public API that promise covers is the ten tools
in the table below, their parameters and result fields, and the `stlink-mcp` command
line. At 0.y.z any of it may change. Beta is reached when one user outside this project
reports a run on a board this README does not list as measured, the ten tool signatures
and result fields are declared stable, and the JTAG and Windows questions are decided.
Release candidate follows once two known defects are fixed: two tool calls at once can
give one GDB port to two probes or start two servers on one probe, and `run_firmware`
discards GDB's own output on the failure path where the cause matters. 1.0.0 is cut
when every promise in this README has a measurement behind it in
[docs/findings.md](docs/findings.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

Plans are in [ROADMAP.md](ROADMAP.md). The measurements behind the claims in
this file are in [docs/findings.md](docs/findings.md).

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
| `board_state` | Identifies the target, the probe, and the family's quirks | Tries access ports 0 and 1, since a wrong port and an absent target look identical. If no hotplug connect reaches the core it retries with a resetting connect, and reports `target_was_reset` whether or not that connect worked. Reports `unverified` when a held server means it is answering from cache |
| `run_firmware` | Loads an ELF into RAM and starts it | Refuses, before any load, an image not linked for the family's RAM or with any loaded segment outside the SRAM region, since a GDB load writes wherever the image says. Captures console output from before the target resumes. `failed` if the GDB batch did not reach its end markers, since then the load did not happen and any output belongs to the previous image |
| `flash_firmware` | Programs a signed image into boot memory | The programmer's own report that the download completed. Nothing is read back, and `verified_by` says so |
| `read_memory` | Reads 32-bit words without disturbing the target | The words themselves. Connects hotplug-only, and refuses rather than resetting to reach a closed access port |
| `core_registers` | Reads the core registers, halting the core for the read | The registers themselves. Reports `target_was_reset` when reaching the core needed a reset. Does not claim the core is still halted afterwards, since a programmer halt does not outlive its command |
| `read_fault_status` | Reads and decodes the Cortex-M fault registers | The raw registers plus a plain-English diagnosis. Reports the faulting address when the hardware recorded it, and the next step to find the faulting instruction. Refuses Cortex-M0, M0+, M1 and M23 cores, which have no such registers and would read as clean, and refuses a read that did not return all six |
| `capture_console` | Captures serial output | Reads the port mapped to that probe's serial, and refuses if it cannot resolve one |
| `reset_target` | Resets the target and checks it restarted | Halts at the reset vector and compares the PC against word 1 of the vector table. The reset is the *connect*, because `-rst` then `-halt` loses the race and lands hundreds of bytes into start-up |
| `release_probe` | Frees a held GDB server | `unverified` if nothing was held, since then nothing was released |
| `list_probes` | Lists attached probes and their consoles | Deduplicates the tool's own double listing. Works while other probes are held |

### The GDB server stays alive between runs

`run_firmware` starts a GDB server and holds it. On the STM32N6, an image built
from ST's FSBL template closes debug access port 1 when it starts, and only a
power cycle reopens it. The cause is one line at the end of ST's
`system_stm32n6xx_fsbl.c`, which switches the SYSCFG clock off on the way out
of `SystemInit`:

```c
RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;   /* deactivate SYSCFG clock */
```

Remove that write and the port survives across runs. With the stock image,
holding the server is the difference between one power cycle per session and
one per run. Either way the target is reset before every load; without that,
each image inherits the previous run's peripheral and interrupt state and
faults during start-up.

While the server holds a probe, `read_memory`, `core_registers` and
`flash_firmware` cannot reach that probe until you call `release_probe`. Other
probes are unaffected, and so is enumeration.

`run_firmware` is the one to use while developing: faster than flashing, and
an image placed in RAM by the debugger bypasses the boot ROM, so no signing is
involved even on parts that require signed images to boot. The image must be
linked for RAM, with its vector table at the address `board_state` reports for
the family, and every section it loads must lie in the SRAM region, 0x20000000
to 0x3FFFFFFF. A default flash-linked build is refused before anything is
loaded, because a GDB load writes each segment to the address the image
carries. The tool sets the stack pointer and program counter from that vector
table and does not set VTOR, so an image that uses interrupts must point VTOR at
its own table in its start-up code.

### More than one board

Every tool takes a `probe_serial`. With several boards attached, omitting it is
an error, not a guess: probe order is not stable across replugs, and loading
firmware onto the wrong board is a failure that looks like success.

`list_probes` reports which serial port belongs to which probe. That mapping is
not positional, so pairing probes with `/dev/ttyACM*` by order gets it wrong,
and `run_firmware` uses each board's own console to verify it. Each board gets
its own GDB server on its own port.

## Configuration

| variable | meaning |
| --- | --- |
| `STLINK_MCP_BOARDS` | TOML file describing boards this project has not characterised. Defaults to `~/.config/stlink-mcp/boards.toml` if it exists |
| `STLINK_MCP_NO_VENDOR_SEED` | Set to anything to run on measured and user-stated data only, with no seeding from ST's device databases |
| `STLINK_MCP_CLT_ROOT` | STM32CubeCLT install directory |
| `STLINK_MCP_CONSOLE` | Serial device, if the guess is wrong |
| `STLINK_MCP_CONSOLE_BAUD` | Console baud rate, default 115200 |
| `STLINK_MCP_GDB_PORT` | GDB server port, default 61234 |

## Where addresses come from

Every address this tool uses carries a provenance, and `board_state` reports it
beside the value:

| provenance | meaning |
| --- | --- |
| `measured` | somebody ran it on this part and watched it work |
| `vendor-db` | read out of STMicroelectronics' device database |
| `user-stated` | typed into a board file by whoever runs this server |
| `assumed` | nobody measured it and no source states it; a default for the range |

Only `measured` means the value has been seen to work, and the label changes
behaviour. `flash_firmware` refuses to write to a base that is not measured
unless you name the address yourself, because that write persists and nothing
is read back. `run_firmware` will load to a `vendor-db` address, because a
wrong one is undone by a reset, and the result still says the address was not
measured. A measured entry always beats a seeded one: the databases can report
a vector table address as the RAM base, and they do not carry the access port
at all, so a seeded part gets port `0` marked `assumed` and `board_state`
warns you.

The `vendor-db` values are read at run time from the STM32CubeProgrammer device
database inside your own STM32CubeCLT install, which stays under ST's licence
(SLA0048). This project ships none of those files and no table derived from
them.

## Describing your own board

Write a TOML file, one table per device ID as the programmer prints it, at
`~/.config/stlink-mcp/boards.toml` or wherever `STLINK_MCP_BOARDS` points:

```toml
["0x413"]
name = "STM32F4 on a NUCLEO-F429ZI"
access_port = 0
ram_load_base = 0x20000000
vector_table_addr = 0x20000000
flash_base = 0x08000000
notes = "addresses from the reference manual, not yet run on the board"
```

It is loaded before the server accepts anything, and a file it cannot use stops
the server. Your entry wins over the built-in table for that device ID.

Everything in that file is `user-stated`, and there is no way to mark a value
`measured`: that label is what lets `flash_firmware` write without being told
an address, and a typo in a config file must not unlock an irreversible write.
If you have measured your board, pass `address` when you flash.

## Adding a family

Add one entry to `src/stlink_mcp/quirks.py` and map its device ID, filling in
only what you have verified on hardware; a test fails the build otherwise. The
table is keyed on the device ID the probe reports, which is coarser than a part
number: ST's database calls `0x450` `STM32H7xx`, so an STM32H743 and an
STM32H753 share one entry. Put in values that hold for everything behind the
ID, and keep anything that varies by board or by image out of it.

## Limitations

* It does not build your firmware. That is your build system's job.
* It does not sign images, because signing needs keys and options that belong to
  your project rather than to a programmer.
* Only ST-LINK with STM32CubeCLT is supported. There is no abstraction layer
  waiting for a second backend: adding OpenOCD would mean introducing one first.
* Tested on Fedora Linux 44 with STM32CubeCLT 1.22.0 (STM32CubeProgrammer
  2.23.0), Python 3.12 and on-board ST-LINK V3 probes, on NUCLEO-N657X0-Q,
  NUCLEO-H743ZI and STM32H753I-EVAL2 boards. Other Linux distributions should
  work and have not been run. macOS is plausible and untried: the toolchain
  search and the console device patterns cover it. Windows is not supported:
  the executable names and device paths are wrong for it throughout.
* Two device families are verified on hardware, STM32N6 and STM32H7. Parts
  seeded from the vendor database can run firmware; anything unrecognised can
  be identified and read but not run.
* Every probe this has run against is ST-LINK V3 generation. The probe list
  parser should handle an older ST-LINK/V2 or V2-1, but that is reasoning, not
  a measurement. If you hit a parsing problem on a V2, the captured `-l` output
  is the useful bug report.
* It is not affiliated with STMicroelectronics.

## Related projects

Three other MCP servers cover the same ground, and each does more than this
one. The difference is what a result means.

* [Adancurusul/embedded-debugger-mcp](https://github.com/Adancurusul/embedded-debugger-mcp)
  (Rust; probe-rs or OpenOCD underneath) has breakpoints, stepping, RTT, flash
  programming with an optional readback compare, J-Link and DAPLink probes,
  several architectures, and a fault tool that reports the registers without
  asserting a cause. Its success text is the backend call returning: `run`
  reports "resumed execution successfully" with nothing observed afterwards,
  `reset` prints a program counter from a snapshot that is allowed to fail and
  shows zero when it does, and `run_firmware` ends with "Firmware is now
  running on target" whether or not RTT was attached. Read from its source on
  2026-09-04; the file references are in [docs/findings.md](docs/findings.md).
* [shieldyguy/stm32-mcp](https://github.com/shieldyguy/stm32-mcp) builds with
  the STM32CubeIDE headless builder, flashes and reads memory through OpenOCD,
  and adds a full serial toolset, memory writes by symbol and live memory
  monitoring. It returns the tools' own text; its TODO file records a build
  reported OK with nothing compiled.
* [Helistana/mcp-stm32cubeide-server](https://github.com/Helistana/mcp-stm32cubeide-server)
  drives the same CubeIDE loop, with fault decoding, and is documented in
  Chinese.

Here, `run_firmware` is verified by console output from the new image and by
nothing else, a result nobody watched says `unverified`, and no tool can set
`ok` on its own. Every part-specific address carries a provenance that changes
what the tool will do, the STM32N6 behaviour in the findings was measured on
the board, and the tests were seeded and shown to fail. What this project
lacks against the three: building, memory writes, breakpoints, JTAG, RTT, and
a readback after flashing. It was built before its author had looked for any
of them; the field was searched on 2026-09-02, three weeks in, which is the
wrong order and is recorded as such in the findings.

## Licence

MIT. See [LICENSE](LICENSE).
