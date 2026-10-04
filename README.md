# stlink-mcp

An MCP server that lets an AI coding agent program, run and inspect an STM32 target over ST-LINK. It runs locally, next to the board, and drives the STM32CubeCLT command line tools you already have installed.

## Why you would want this

Firmware development is a loop: change the code, build, load it onto the board, watch the console, read the fault registers when it hangs, change the code again. An agent that can only edit files does the first step and then waits for you to do the rest and report back. Every result reaches it as your summary of what you saw, and it reasons from that.

With this server attached, the agent runs the loop itself. It identifies the board, loads the image, captures the console, reads the registers and the fault status, and decides what to change next from what the target actually did. You supervise the conclusions instead of relaying the evidence.

## Why the reports have to be true

That loop is only as good as what the tools tell the agent. A developer catches a tool that overstates, because they hold information the tool never gave them: the LED that did not change, console text that looks like the previous build, the board in their hand. An agent has none of that. The text a tool returns is its entire model of the hardware, so a tool that reports success for something it did not do makes the agent confidently wrong, and the agent then writes that wrongness into code, commit messages and documentation, where it costs far more than the failed operation did.

ST's command line tools do overstate. Three examples from one afternoon with an STM32N6:

* `STM32_Programmer_CLI -s <address>` printed "Start operation achieved successfully" while the program counter never left the boot ROM.
* `ST-LINK_gdbserver` printed "Failed to halt target". The core was fine; the server was on the wrong debug access port.
* A GDB load against a running target completed cleanly and changed nothing, so the previous program carried on and its output read as a successful new run.

An agent driving those tools inherits every one of them. So the rule here is:

> **A tool reports success only after checking that the thing it was asked to do actually happened.**

`run_firmware` reports success because the target produced console output, not because GDB exited zero. Every result has one of three states, and no tool sets `ok` by hand:

| `status` | `ok` | meaning |
| --- | --- | --- |
| `verified` | `true` | checked, and `verified_by` names the observation |
| `unverified` | `false` | may well have worked, but nothing watched it |
| `failed` | `false` | it did not work, and `error` says why |

`unverified` is deliberately not `ok`: a run nobody watched is not a success, however likely it is to have worked. `unverified_because` says what was not checked, so it is not mistaken for a failure.

## Who this is for

Use it if your board has an ST-LINK, STM32CubeCLT is already installed, and you want an agent whose every report names what was observed on the target. It is debugging shaped for an agent rather than a person: thirteen tools, which stop the target where it is asked to and say why it stopped, and no single-stepping, RTT or memory writes.

Use [embedded-debugger-mcp](https://github.com/Adancurusul/embedded-debugger-mcp) if you need single-stepping, breakpoints you manage yourself, RTT, a J-Link or DAPLink probe, a target that is not an STM32, or Windows support beyond what is measured here. [Related projects](#related-projects) has the comparison.

## Status

Alpha. Three families are measured on hardware, STM32N6, STM32H7 and STM32F4. Another 96 device IDs are seeded from STMicroelectronics' own device databases on your disk (the seeding reads 99, and the three measured ones take precedence), and every value from that source is labelled as such. Four device IDs (`0x42B`, `0x42C`, `0x472`, `0x49A`) are refused rather than guessed, because their databases name several separate SRAM banks; `board_state` names the addresses it would not choose between, and a board file settles it. Anything else unrecognised falls back to identification and memory reads only.

Versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html) and are read from the git tag; a build past the last tag reports the tag plus the number of commits since it, as in `0.1.0+3`. The public API that promise covers is the thirteen tools in the table below, their parameters and result fields, and the `stlink-mcp` command line. At 0.y.z any of it may change. Beta is reached when one user outside this project reports a run on a board this README does not list as measured, the thirteen tool signatures and result fields are declared stable, and the Windows question is decided. Release candidate follows beta. The two defects that gated it are fixed: two tool calls at once could give one GDB port to two probes or start two servers on one probe, and `run_firmware` discarded GDB's own output on a failed load. 1.0.0 is cut when every promise in this README has a measurement behind it in [docs/findings.md](docs/findings.md), whose last section lists what each promise rests on and what is not yet measured. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

Plans are in [ROADMAP.md](ROADMAP.md). The measurements behind the claims in this file are in [docs/findings.md](docs/findings.md). How to report a problem or contribute a board you have measured is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Requirements

* [STM32CubeCLT](https://www.st.com/en/development-tools/stm32cubeclt.html). It bundles STM32CubeProgrammer, the GDB server and the Arm toolchain. Found automatically in `stm32cubeclt*` directories under `/opt/st`, `~/st` or `/usr/local/st`, and on Windows under `C:\ST`, where its installer puts it. Anywhere else, set `STLINK_MCP_CLT_ROOT` to the install directory.
* Python 3.11 or newer.
* An ST-LINK, which on most development boards is on the board itself.
* On Linux, membership of the `dialout` group for the serial console, and ST's udev rules for the probe (STM32CubeCLT installs these).

## Install

With [uv](https://docs.astral.sh/uv/) installed:

```sh
git clone https://github.com/dii/stlink-mcp
cd stlink-mcp
uv sync
uv run stlink-mcp --check
```

`--check` reports the toolchain it found, the vendor database, any board file and the probes attached, then exits, without starting the server. It lists probes the same way `list_probes` does, which neither halts nor resets them.

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

With Claude Code, the same thing is:

```sh
claude mcp add stlink -- uv --directory /path/to/stlink-mcp run stlink-mcp
```

## Tools

| tool | what it does | how it verifies |
| --- | --- | --- |
| `board_state` | Identifies the target, the probe and the family, and says whether the core runs, without halting it | Tries access ports 0 and 1, since a wrong port and an absent target look identical. If no hotplug connect reaches the core it retries with resetting connects, ending with a connect under reset, and reports `target_was_reset` whether or not they worked. `core` gives the state from DHCSR, running, halted, locked up or asleep, and five program-counter samples, with their places in the source given `elf_path`. Beside a held GDB server it reads live and never resets; without `stlink-server` it answers from cache, `unverified` |
| `run_firmware` | Loads an ELF into RAM and starts it | Refuses, before any load, an image not linked for the family's RAM or with any loaded segment outside the SRAM region, since a GDB load writes wherever the image says. Captures console output from before the target resumes; what the probe kept from before the run is reported apart and never verifies it. `failed` if the GDB batch did not reach its end markers, since then the load did not happen and any output belongs to the previous image. Keeps the probe afterwards unless `keep_probe` is false |
| `flash_firmware` | Programs a raw binary (.bin) into boot memory | Reads the written range back and compares it with the image. Only an identical readback is `verified`; a difference is `failed`, naming the first differing address. Refuses HEX, S-record and ELF files, which the programmer writes to their own addresses rather than the one the tool checked. The erase takes whole sectors, so the bytes after the image up to the next 128 KB boundary are read before and after the write, and the result says which of them it erased. On a part that boots only signed images, such as the STM32N6, it says to sign first; it does not sign |
| `read_memory` | Reads 32-bit words, a variable by name, or a peripheral register by name, without halting or resetting the target, and refuses a register whose read is known to change state unless told `read_effects_ok` | The words themselves. Connects hotplug-only, and refuses rather than resetting to reach a closed access port. A read that returns fewer words than asked for is `failed`, naming the missing addresses. A variable is decoded with the ELF's types, and is `verified` only when the start of the ELF's code matches the target's memory. A register is decoded into its fields from ST's SVD file. A read in the peripheral range names the registers it covered, and what reading them is known to do |
| `core_registers` | Reads the core registers, halting the core for the read; with the ELF, the call stack with each frame's arguments and locals | The registers themselves. Reports `target_was_reset` when reaching the core needed a reset. Leaves the core as it found it, running or halted, and says which afterwards, from DHCSR read without halting it |
| `run_until` | Runs the target until it reaches a function, a line or an address, or writes a variable, and holds the core there | A function's inlined copies get breakpoints too, since GDB breaks on its out-of-line copy only. The result gives the instruction at the stop, from the ELF. `verified` only when the chip's Debug Fault Status Register, as the GDB server read it at the stop, names a breakpoint or a watchpoint. A time limit that runs out is `unverified` and says where the core is. Refuses an ELF the target does not run, and a hardware breakpoint the part cannot place. Every result says the rest of the chip keeps running |
| `read_fault_status` | Reads and decodes the Cortex-M fault registers | The raw registers plus a plain-English diagnosis; MMFAR and BFAR only when CFSR marks them valid, since otherwise they hold a stale value. Reports the faulting address when the hardware recorded it, and the next step to find the faulting instruction. A clean read beside a held GDB server that caught a vector is `unverified`, since that is how a lockup looked once the server reset the core. Refuses Cortex-M0, M0+, M1 and M23 cores, which have no such registers and would read as clean, and refuses a read that did not return all six |
| `read_option_bytes` | Reads the option bytes as ST's programmer explains them: readout protection, brown-out level, user configuration, boot addresses, write protection | Read only, by design: no tool here writes option bytes, since some writes cannot be undone. Never halts or resets the target. Fails with ST's reason on a part the programmer has no option bytes for, such as the STM32N6 |
| `locate_fault` | Finds the instruction that faulted, from the exception frame. Halts the core for the register read, and lets it go afterwards if it was running | EXC_RETURN in LR names the stack holding the frame, and word 6 of the frame is the stacked PC; with the ELF, its function, line and instruction. `unverified` when the words found do not look like a frame, as when a handler pushed registers first; refuses in thread mode or when LR no longer holds EXC_RETURN |
| `capture_console` | Captures serial output | Reads the port mapped to that probe's serial, or the one `STLINK_MCP_CONSOLE` names for it, and refuses if it cannot resolve one. What the probe kept from before the capture comes back apart, in `console_buffered`, and output that is mostly not text carries a note about the baud rate. A capture that hears nothing is `unverified`, because a port not wired to the target reads the same as a silent one |
| `capture_swo` | Captures what the target writes to an ITM stimulus port, over the SWO pin | Text that arrived on that port during the capture. Needs the clock the core runs at while it writes; a capture that hears nothing is `unverified` and names a wrong clock among the causes, and mostly unreadable text carries a note about the clock. Every result says the target's trace output was left on |
| `reset_target` | Resets the target and checks it restarted | Halts at the reset vector and compares the PC against word 1 of the vector table. The reset is the *connect*, because `-rst` then `-halt` loses the race and lands hundreds of bytes into start-up. Stops a GDB server holding the probe first, and says so |
| `release_probe` | Stops the GDB server holding a probe, so other software can use it, which resumes a core `run_until` held | `unverified` if nothing was held, since then nothing was released. Reads DHCSR afterwards to say whether a held core runs again |
| `list_probes` | Lists attached probes and their consoles | Deduplicates the tool's own double listing. Works while other probes are held |

### Holding the probe, and when the program closes the debug port

`run_firmware` loads through a GDB server and keeps it holding the probe after the program has started. The other tools reach the core beside it, in ST's shared mode: the server is started with `-t`, and the programmer connects with `shared`, which reads without halting the core while GDB stays attached. A reset or a flash write made that way broke the GDB session, so `reset_target` and `flash_firmware` stop the server first, and say so in `released_probe_first`. Shared mode needs `stlink-server`, which STM32CubeCLT installs on the PATH; without it the server keeps the probe to itself, and `read_memory`, `read_fault_status`, `locate_fault` and `capture_swo` refuse until `release_probe`.

Whether to hold is a trade, and the agent makes it: `run_firmware(keep_probe=False)` lets go as soon as the program starts, and `release_probe` lets go later.

- Held, the debugger stays connected whatever the program does, and only this server's tools and ST's own `STM32_Programmer_CLI -c port=SWD shared` reach the probe; OpenOCD and other debuggers cannot. A core that locks up, by faulting inside its fault handler, is reset by the held server and halted at its reset vector, which clears the fault registers: measured on the H7, the F407 and the N6, and not in ST's documentation. Results that find a core halted that way say so, and suggest running again with `keep_probe=False`. A reset the program asks for itself restarts it as usual.
- Released, the lockup stays visible, with its fault status. But a program can close the debug port, by taking the SWD pins for other use, turning off a clock the debug port needs, or sleeping with debug off, and getting back in then takes a connect under reset, which restarts the program. A program that takes the SWD pins cuts off a held server too: reads beside it fail, and after `release_probe`, `board_state` gets back in with a connect under reset, as it did on an STM32H743.

On the STM32N6, an image built from ST's FSBL template closes debug access port 1 when it starts, and only a power cycle reopens it once the probe is let go. The cause is one line at the end of ST's `system_stm32n6xx_fsbl.c`, which switches the SYSCFG clock off on the way out of `SystemInit`:

```c
RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;   /* deactivate SYSCFG clock */
```

Remove that write and the port survives across runs. With the stock image, keeping the server is the difference between one power cycle per session and one per run. Either way the target is reset before every load; without that, each image inherits the previous run's peripheral and interrupt state and faults during start-up.

While a server holds a probe, `core_registers` and `run_until` work through it and `capture_console` still reads its console. Other probes are unaffected. Listing the probes takes about 5 seconds while one is held, which is one reason a call that names its probe does not list them.

`run_firmware` is the one to use while developing: faster than flashing, and an image placed in RAM by the debugger bypasses the boot ROM, so no signing is involved even on parts that require signed images to boot. The image must be linked for RAM, with its vector table at the address `board_state` reports for the family, and every section it loads must lie in the SRAM region, 0x20000000 to 0x3FFFFFFF. A default flash-linked build is refused before anything is loaded, because a GDB load writes each segment to the address the image carries. The tool sets the stack pointer and program counter from that vector table and does not set VTOR, so an image that uses interrupts must point VTOR at its own table in its start-up code.

### More than one board

Every tool takes a `probe_serial`. With several boards attached, omitting it is an error, not a guess: probe order is not stable across replugs, and loading firmware onto the wrong board is a failure that looks like success.

`list_probes` reports which serial port belongs to which probe. That mapping is not positional, so pairing probes with `/dev/ttyACM*` by order gets it wrong, and `run_firmware` uses each board's own console to verify it. On Linux a tool given a `probe_serial` finds that port by the serial in `/dev/serial/by-id`, where udev names each port after its probe, and reports that path, which stays the same across a replug. Each board gets its own GDB server on its own port.

Calls on different probes run at the same time, and calls on one probe wait for each other. A call that has to list the probes, `list_probes` or any call without a `probe_serial`, waits until no probe is in use and runs alone, because listing beside a command on another probe broke some of those commands. With several boards attached, name the probe in every call.

### Who asked for what

Every tool call writes one line to the server's stderr, which MCP clients keep in their logs: the client's declared name and version, which connection and request it came on, the tool, the probe and the verdict. A call refused because a GDB server holds the probe says, in `held_by`, which client's run is holding it, and `release_probe` says whose it was. Nothing is authenticated: the caller is recorded, not checked.

### SWD or JTAG

Every tool that connects takes `interface`, `swd` (the default) or `jtag`, and its result reports the interface and, where the call connected through the programmer, the clock the probe's connect banner gave for it. Not every probe does JTAG. On the boards measured, the NUCLEO-N657X0-Q's own probe does; the NUCLEO-H743ZI2's own STLINK-V3E does not, and answers a JTAG connect exactly as if no target were attached, so the result's hint names that first. A JTAG failure there also takes over a minute, because the tool tries both access ports and a resetting connect. An ST-LINK/V2 does both, on the H743's JTAG connector and on an F407 board's. There is no automatic fallback from one to the other: the tool connects over the interface it was given, and a GDB server kept for one interface is not reused for the other.

### Names instead of addresses

`read_memory` reads a variable by name, given `symbol` and the `elf_path` the target runs, and returns it decoded with the ELF's own types: integers, floats, enums, bitfields, arrays, structs, unions and pointers. It reads the variable without halting the target, and compares the first 64 bytes of the ELF's code with the target's memory in the same call, so a result is `verified` only when the target runs that ELF; with another build loaded, the decoded bytes come back apart, as `decoded_with_this_elf`. A name that matches two variables, such as a file static and a function static, is refused with both, and a local that lives in a register or on the stack is refused as having no fixed address.

It reads a peripheral register by name, given `register` as `PERIPHERAL.REGISTER`, such as `RCC.CR`, and returns its value and each field, from ST's SVD files in the STM32CubeCLT install. The file comes from `svd` when given, otherwise from the board name the probe reports (a NUCLEO-H743ZI2's probe reports NUCLEO-H743ZI, which names the STM32H743), otherwise from the part names in the device name, and where that leaves several files the register is used only when every one of them describes it the same way: an STM32F407 board with no board name reads RCC.CR because all four files for its device ID agree. Core registers such as NVIC are in ST's core files, named with `svd`, for example `svd="Core/Cortex-M7"`.

### Is the program running, and where

`board_state` reads the core's debug status register and samples the program counter five times, none of which halts the core: running, halted, locked up or asleep, and where it is executing, by function and line when given the ELF. On the H7, the F407 and the N6 every sample of a running program lay inside its code. Two limits, both measured. The sampler works only while the debug trace unit is on, which a GDB session turns on and a reset turns off, so straight after a reset it gives nothing and says why. And a core that sets neither the halt nor the lockup bit and retires no instructions between two reads is reported as `not seen running`, not guessed at: the H7 read that way both after a fault inside its fault handler and after a reset into its own firmware.

`run_firmware` uses the same samples: an image that prints nothing, or a board with no console, is `verified` when every sample lies inside the image it just loaded.

Every programmer call that connects or reads also reads the core's CPUID register, whose top byte is 0x41 on every Cortex-M part. On an H7 whose core slept on WFI, every address read 0x01010001 with no error, and without that check the tool returned it as fact. Now such a read fails with the reason, and the tools that may reset get past it with a connect under reset.

### Reads that change state

Some registers change when they are read. Reading a UART's receive register takes the character out of it, so the firmware never sees it; reading an ADC's data register clears "conversion done"; reading an RTC's sub-second register holds the time and date until the date register is read. ST's SVD files have a field for this, and 14 of the 233 in STM32CubeCLT 1.22.0 use it at all, none of them for those registers.

So `read_memory` keeps its own list, by peripheral block rather than by part, since ST reuses its blocks across the range and they keep their register names: the UART, SPI, I2C and ADC data registers and the RTC's time registers, which between them appear in 227 of the 233 files. Each entry says how it is known, `measured` on a named part or `assumed`. ST's own marks are used too, including a mark in another part's file for the same block. A read by name, or by address anywhere in the peripheral range, of a register on the list is refused, before anything is read, unless the call passes `read_effects_ok`; then it is read and the result says what it did. Any other read in the peripheral range names the registers it covered and says that no effect is known, which does not mean there is none. Looking a range up costs one extra connect, and building the index of ST's own marks takes about 8 seconds once, after which it is kept in the user's cache directory.

`run_until` refuses to watch a register on the list, because GDB reads a watched address when it sets the watchpoint, and reads no peripheral register's value itself.

### Stopping the target

`run_until` runs the program until it reaches a `location`, a function, `file.c:line` or an address, or until it writes `watch`, a variable or an address, for at most `seconds`, and leaves the core halted there with the call stack, each frame's arguments and locals, and the registers. It needs the ELF the target runs, compares its first 64 bytes of code with the target's memory before it places anything, and refuses another program, since a breakpoint would otherwise be written into code it does not describe. It starts from the program as it runs; to stop early in a program, load it with `run_firmware` and call `run_until` straight after.

A stop is `verified` only on the chip's own account. ST's GDB server reads the Debug Fault Status Register at each stop and then clears it, so the tool runs its own server with the log that records the value, and a stop is verified when that value names a breakpoint (0x3) or a watchpoint (0x5) and, for a breakpoint, the core is at the location. A watchpoint stops after the write, not on it: one to four instructions later on the parts measured, past a return, a branch or into a call. The write was made either just before the stop or in the function the last call before LR went to, which the result gives as `returned_from` unless the core is still inside it; neither PC nor LR can say which, and the result says so. A watch covers 1, 2 or 4 bytes at an address aligned to its size, and anything else is refused: ST's GDB server accepted a wider one and watched only its first byte, and one at an unaligned address never fired. A time limit that runs out is `unverified`, with the core halted where it was; if that is a fault handler, the result says so. A breakpoint GDB cannot insert, at an address where nothing answers, fails at once with GDB's reason.

While the core is held, the rest of the chip is not: timers, DMA, USB and watchdogs keep running, and every result says so. `run_until` again continues from the stop, `core_registers` with the ELF reads the call stack there and leaves the core halted, and `release_probe` resumes it and reads DHCSR to say it runs. A hardware breakpoint at an address the part's breakpoint unit cannot cover is refused: an STM32F407's unit covers only code below 0x20000000, and there GDB and the server accepted one in RAM without a word and it never fired. In RAM GDB uses software breakpoints, which work on every part measured. `run_until` uses whichever GDB server holds the probe, `run_firmware`'s included, since every server keeps the log that records the chip's reason for a stop; it starts its own, in attach mode, only when none holds the probe. So a stop never lets go of the probe, which a program that closes the debug port would not give back. Measured on an STM32H7, an STM32F407 and an STM32N6.

### SWO

`capture_swo` reads what firmware writes to the Cortex-M ITM, the debug unit's own printf channel, through the probe's SWO pin, so a board with no serial console still has something to verify a run by: load with `run_firmware`, then capture. It needs `core_clock_hz`, the clock the core runs at while it writes, because the SWO rate is derived from it; only the firmware knows it, and it may differ from the clock at reset. On an STM32F407 a clock twice the real one gave nothing, and one 6% off gave unreadable text. It uses STM32_Programmer_CLI's own SWO capture, which sets up the trace unit and the pin itself, and works over SWD only, because the SWO pin is JTAG's TDO.

The capture changes the target, and every result says so: it turns on ITM and hands the SWO pin from the application to the trace unit. On the STM32F407 it was measured on, where the pin is PB3, that setting survived `reset_target` and only a power cycle cleared it. Measured on an STM32H7 through its own STLINK-V3E and on an STM32F407 through an ST-LINK/V2.

## Configuration

| variable | meaning |
| --- | --- |
| `STLINK_MCP_BOARDS` | TOML file describing boards this project has not characterised. Defaults to `~/.config/stlink-mcp/boards.toml` if it exists |
| `STLINK_MCP_NO_VENDOR_SEED` | Set to anything to run on measured and user-stated data only, with no seeding from ST's device databases |
| `STLINK_MCP_CLT_ROOT` | STM32CubeCLT install directory |
| `STLINK_MCP_CONSOLE` | Serial console per probe, for a board whose console is not the probe's own, such as a USB serial adapter: `SERIAL=/dev/ttyUSB0`, comma-separated for several. It wins over the probe list's mapping. A bare device path is used only while a single probe is attached |
| `STLINK_MCP_CONSOLE_BAUD` | Console baud rate, default 115200 |
| `STLINK_MCP_GDB_PORT` | First GDB server port, default 61234. Each probe takes a pair of ports, four apart |

A setting that cannot be used stops the server at start-up with a one-line message, the same way a bad board file does.

## Where addresses come from

Every address this tool uses carries a provenance, and `board_state` reports it beside the value:

| provenance | meaning |
| --- | --- |
| `measured` | somebody ran it on this part and watched it work |
| `vendor-db` | read out of STMicroelectronics' device database |
| `user-stated` | typed into a board file by whoever runs this server |
| `assumed` | nobody measured it and no source states it; a default for the range |

Only `measured` means the value has been seen to work, and the label changes behaviour. `flash_firmware` refuses to write to a base that is not measured unless you name the address yourself, because that write persists, and reading it back afterwards confirms what was written, not that it went to the right place. `run_firmware` will load to a `vendor-db` address, because a wrong one is undone by a reset, and the result still says the address was not measured. A measured entry always beats a seeded one: the databases can report a vector table address as the RAM base, and they do not carry the access port at all, so a seeded part gets port `0` marked `assumed` and `board_state` warns you.

The `vendor-db` values are read at run time from the STM32CubeProgrammer device database inside your own STM32CubeCLT install, which stays under ST's licence (SLA0048). This project ships none of those files and no table derived from them.

## Describing your own board

Write a TOML file, one table per device ID as the programmer prints it, at `~/.config/stlink-mcp/boards.toml` or wherever `STLINK_MCP_BOARDS` points:

```toml
["0x419"]
name = "STM32F429 on a NUCLEO-F429ZI"
access_port = 0
ram_load_base = 0x20000000
vector_table_addr = 0x20000000
flash_base = 0x08000000
notes = "addresses from the reference manual, not yet run on the board"
```

It is loaded before the server accepts anything, and a file it cannot use stops the server. Your entry wins over the built-in table for that device ID.

The same file can add registers whose read changes state, for parts or peripherals the built-in list does not cover:

```toml
[[read_effects]]
block = "USART"         # the SVD file's groupName, a regular expression
registers = ["RDR"]     # names without the peripheral's prefix
effect = "takes the received character and clears RXNE"
```

Everything in that file is `user-stated`, and there is no way to mark a value `measured`: that label is what lets `flash_firmware` write without being told an address, and a typo in a config file must not unlock an irreversible write. If you have measured your board, pass `address` when you flash.

## Adding a family

Add one entry to `src/stlink_mcp/quirks.py` and map its device ID, filling in only what you have verified on hardware; a test fails the build otherwise. The table is keyed on the device ID the probe reports, which is coarser than a part number: ST's database calls `0x450` `STM32H7xx`, so an STM32H743 and an STM32H753 share one entry. Put in values that hold for everything behind the ID, and keep anything that varies by board or by image out of it.

## When something does not work

Run `uv run stlink-mcp --check` first. It names what it found and what it did not.

* **No STM32CubeCLT install found.** Set `STLINK_MCP_CLT_ROOT` to the directory that contains `STM32CubeProgrammer/bin`.
* **No ST-LINK probes found.** On Linux the probe needs ST's udev rules, which STM32CubeCLT installs; unplug the probe and plug it in again after installing them. A probe the programmer lists but cannot read usually means a USB problem, and `list_probes` says so.
* **The serial port cannot be opened.** On Linux the user needs to be in the `dialout` group, and a new group membership takes effect at the next login.
* **Could not reach the core.** The result's `hint` lists the likely causes. One is another process holding the probe, such as an IDE or a GDB server left by an earlier session. Check by port with `ss -ltnp`, not by process name: Linux truncates process names to fifteen characters, so `pgrep ST-LINK_gdbserver` finds nothing even while one is running.
* **Serial number not found, or old firmware.** STM32CubeProgrammer refuses ST-LINK firmware that is too old: an ST-LINK/V2 on V2J16S4 was listed and then refused. STLinkUpgrade, in the STM32CubeCLT install, updates it (select the probe, Open in update mode, Upgrade).
* **run_firmware refuses the image.** It has to be linked for RAM; see [When the program closes the debug port](#when-the-program-closes-the-debug-port).
* **Other tools report the probe as held.** A GDB server holds the probe without `stlink-server`, so nothing can connect beside it. Install STM32CubeCLT's `stlink-server`, or call `release_probe`. After a program that closes the port, such as ST's stock start-up code on an STM32N6, the board may then need a power cycle before the next run.

## Limitations

* It does not build your firmware. That is your build system's job.
* It does not sign images, because signing needs keys and options that belong to your project rather than to a programmer.
* Only ST-LINK with STM32CubeCLT is supported. There is no abstraction layer waiting for a second backend: adding OpenOCD would mean introducing one first.
* Tested on Fedora Linux 44 with STM32CubeCLT 1.22.0 (STM32CubeProgrammer 2.23.0), Python 3.12 and on-board ST-LINK V3 probes, on NUCLEO-N657X0-Q, NUCLEO-H743ZI2 and STM32H753I-EVAL2 boards, and with two ST-LINK/V2s, one on the NUCLEO-H743ZI2's JTAG connector (firmware V2J38S7) and one on a custom STM32F407IGT6 board (V2J48S7), over SWD and JTAG. Other Linux distributions should work and have not been run. macOS is untried; set `STLINK_MCP_CLT_ROOT` to the install there. Windows 11 has been run with one NUCLEO-N657X0-Q, through STM32CubeCLT 1.22.0 and Windows' own USB drivers: every N6 check passed, from a run verified by its console text over COM3, found by the probe's serial, to `run_until` and reading by name. Not yet run on Windows: SWO, JTAG, writing flash, and the H7 and F407 boards.
* Three device families are verified on hardware, STM32N6, STM32H7 and STM32F4. Parts seeded from the vendor database can run firmware, as the F407 did before its family was measured; anything unrecognised can be identified and read but not run.
* The probes it has run against are on-board ST-LINK V3s and two ST-LINK/V2s. An ST-LINK/V2-1 has not been tried. If you hit a parsing problem on any probe, the captured `-l` output is the useful bug report.
* It is not affiliated with STMicroelectronics.

## Related projects

Three other MCP servers cover the same ground, and each does more than this one. The difference is what a result means.

* [Adancurusul/embedded-debugger-mcp](https://github.com/Adancurusul/embedded-debugger-mcp) (Rust; probe-rs or OpenOCD underneath) has breakpoints, stepping, RTT, flash programming with an optional readback compare, J-Link and DAPLink probes, several architectures, and a fault tool that reports the registers without asserting a cause. Its success text is the backend call returning: `run` reports "resumed execution successfully" with nothing observed afterwards, `reset` prints a program counter from a snapshot that is allowed to fail and shows zero when it does, and `run_firmware` ends with "Firmware is now running on target" whether or not RTT was attached. Read from its source on 2026-09-04; the file references are in [docs/findings.md](docs/findings.md).
* [shieldyguy/stm32-mcp](https://github.com/shieldyguy/stm32-mcp) builds with the STM32CubeIDE headless builder, flashes and reads memory through OpenOCD, and adds a full serial toolset, memory writes by symbol and live memory monitoring. It returns the tools' own text; its TODO file records a build reported OK with nothing compiled.
* [Helistana/mcp-stm32cubeide-server](https://github.com/Helistana/mcp-stm32cubeide-server) drives the same CubeIDE loop, with fault decoding, and is documented in Chinese.

Here, `run_firmware` is verified by console output from the new image and by nothing else, a result nobody watched says `unverified`, and no tool can set `ok` on its own. Every part-specific address carries a provenance that changes what the tool will do, the STM32N6 behaviour in the findings was measured on the board, and the tests were seeded and shown to fail. What this project lacks against the three: building, memory writes, single-stepping and RTT; it stops the target only through `run_until`, one stop at a time. It was built before its author had looked for any of them; the field was searched on 2026-09-02, three weeks in, which is the wrong order and is recorded as such in the findings.

## Licence

MIT. See [LICENSE](LICENSE).
