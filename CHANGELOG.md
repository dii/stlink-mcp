# Changelog

Notable changes to stlink-mcp are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The version is read from the git tag; a build past the last tag reports the tag plus the number of commits since it, as in `0.1.0+3`.

## [Unreleased]

### Changed

- The related-projects section of the README says when the other servers were last read in source.

## [0.8.3] - 2026-10-28

### Added

- From an STM32CubeIDE install, register names come from the SVD files in its product database plugin, so `read_memory` and `sample_values` decode them as with STM32CubeCLT. Run on the Linux bench with STM32CubeIDE 2.2.0 and no STM32CubeCLT in use: `tools/hwcheck` on the three boards passed 112 of 112, its images built with the IDE's compiler, and the programmer, the GDB server and GDB were each started from the IDE's plugin folders.

## [0.8.2] - 2026-10-28

From a user's report on an STM32G474. Run on the Linux bench on 2026-10-28: `tools/hwcheck` on the STM32H7, STM32N6 and STM32F407 boards, without `--flash` or the closed-port section, passed 112 of 112. Finding the tools inside STM32CubeIDE is tested against that report's folder names, not run on an install.

### Added

- Without a complete STM32CubeCLT, the programmer, the GDB server and GDB inside an STM32CubeIDE install are used, each from its newest plugin folder that has it; `--check` names the source. `STLINK_MCP_CLT_ROOT` may name a CubeIDE install.
- A tree from GitHub's "Download ZIP" or `git archive` reports its version from the tag; it reported `0.0.0+unknown`.

### Fixed

- A GDB server that refused the probe's firmware as too old was explained as a debug port the image had closed, on the error line alone. The explanation now follows the server's "Reason:" line: refused firmware names the probe's version and how to update it, and the closed-port text is kept for "Reason: Unknown". Reported on an STM32G474 whose programmer still used the same probe.
- The hints for a probe whose firmware the programmer refuses give the upgrade tool's command line, since an agent cannot use its window.

## [0.8.1] - 2026-10-08

The README and the package description only; no change to the code.

### Changed

- The README opens with what a result means, and its first section is why the reports have to be true: ST's tools reporting success for things that did not happen, and the rule that follows.
- The package description said every tool verifies its own effect before reporting success. A result can be `unverified`, and the description now says so.

## [0.8.0] - 2026-10-07

Run on an STM32H7, an STM32N6 and an STM32F407 board on 2026-10-07. With `--flash` and the N6's closed-port section, `tools/hwcheck` passed the H7's 62 of 62, the N6's 24 of 24 with its flash checks 2 of 2 and the closed-port section 4 of 4, and the F407's 39 of 39; on the code this release carries, 112 of 112 without `--flash` or the closed-port section. On a Windows 11 laptop with an STM32N6 board, 23 passed, and the check of ST's sharing daemon reported itself skipped, since without /proc it cannot look. The N6 booted from its external flash with `tools/hwcheck/n6_flash_boot.py` on 2026-10-06.

### Added

- `sample_values`: variables, addresses or registers sampled together while the program runs, without halting or resetting it, each summarised (first, last, range, changes and when it first changed) with how many samples found the core running or not, and `until` to stop at the first sample where a condition holds.
- `disassemble`: a function by name, or the code around an address, from the ELF, each instruction with its source file and line and the function an inlined copy came from; at most 256 instructions, the rest of a longer function by address. A name two static functions share is refused with both, where objdump's own lookup takes the first. Given `probe_serial` it compares the target's code with the ELF's, without halting it, and is verified only when they match.
- `read_option_bytes`: the option bytes as STM32_Programmer_CLI reads and explains them, readout protection first, then each of ST's sections with every value and its meaning. Read only: nothing in the package writes option bytes. On the STM32N6 it fails with ST's reason.
- `flash_firmware` takes ELF and Intel HEX files, written to the addresses inside them. Every run of bytes must lie in the part's flash, from its measured base and the size its connect reports, or nothing is written; an image linked for RAM is refused, and so is any such file on the STM32N6, whose connect gives no size. `address` with such a file is refused, since the file says where.
- `flash_firmware` writes the STM32N6's external flash through ST's loader for the NUCLEO-N657X0-Q, verified by reading it back.
- `run_firmware` takes `keep_probe`. By default it lets go of the probe once the program starts; `keep_probe=True` keeps a GDB server holding it, and the reading tools work beside it. While a server holds the probe, a core that locks up is reset and halted at its reset vector, which clears its fault registers; `board_state` and `read_fault_status` say so when they find a core halted that way. Holding was the default for a while, to keep a program that closes the debug port from locking the debugger out, and it did not do that.
- `run_firmware` verifies an image that prints nothing, or a board with no console, when program-counter samples lie inside the image; it was `unverified` before.
- `board_state` says whether the core runs, is halted, locked up or sleeping, from DHCSR, with program-counter samples read without halting it; given `elf_path`, where in the source they fall.
- `locate_fault` and `run_until` give the instruction at the fault or the stop, disassembled from the ELF. `run_until` also stops on a function's inlined copies, since GDB breaks only on its out-of-line copy.
- Reads that change state: `read_memory` refuses, before anything is read, a register whose read is known to change it, such as the UART, SPI, I2C and ADC data registers and the RTC's time registers, from a list kept by peripheral block and from ST's own readAction marks. `read_effects_ok` reads it anyway and says what the read did. Any other read in the peripheral range names the registers it covered. Board files take `[[read_effects]]` entries for registers the list does not cover.
- `board_state` says whether an STM32N6 is in development boot or flash boot, from its boot pins.
- `tools/hwcheck/n6_flash_boot.py`: boots the N6 from its external flash with an image that opens debug, and checks the tools around it, a step per position of the BOOT1 switch.
- The server sends MCP instructions when a client connects: what it is for, how to read a result, and the usual order of calls. The README carries the same text for clients that do not pass it on to their agent.
- `tools/hwcheck` checks read effects on the H7 and the F407, writes and restores the flash of the H7, the F407 and the start of the N6's external flash with `--flash`, and can power-cycle probes first with `--cycle-port`.

### Changed

- An argument no parameter of the tool has is refused, naming the nearest parameter, and nothing is done. The MCP SDK dropped it without a word, so a misspelt `length_bytes` read the default length.
- The tools reach a probe a GDB server holds: the server is started in ST's shared mode, and the reading tools connect beside it without halting the core while GDB stays attached. `reset_target` and `flash_firmware` stop the server first and say so in `released_probe_first`. Shared mode needs `stlink-server`, which STM32CubeCLT installs; without it those tools refuse until `release_probe`, as before.
- On Linux, ST's `stlink-server` is stopped before a programmer call when no ST-LINK GDB server runs on the machine, so calls after `release_probe` connect directly and reach a probe the daemon kept. The server assumes it is the probes' only user.
- `release_probe` resumes a core `run_until` left halted.
- Every GDB server keeps the log that records the chip's reason for a stop, so `run_until` uses whichever server holds the probe rather than replacing it.
- Tools that may reset (`board_state`, `core_registers` without `elf_path`, `run_firmware`, `flash_firmware`) try a connect under reset after a hotplug and a reset-pulse connect have failed.
- `flash_firmware` says what its erase took beyond the image: the bytes beside it in each 128 KB block it touches are read before and after the write. A write that fails partway says flash may already have changed. On a part that boots only signed images, its next step says to sign the image first.
- `read_fault_status` is `unverified` for a clean read beside a held GDB server that caught a vector, since a lockup the server reset looks clean. MMFAR and BFAR appear only when CFSR marks them valid.
- `run_until` refuses to watch a register whose read changes state, since GDB reads a watched address when it sets the watchpoint.
- Result fields renamed so one fact has one name across the tools: `halted_during_call` and `core_after` replace `target_halted`, `halted_to_read_registers`, `halted_to_read_pc`, `core_released`, `core_after_release` and the prose notes about the core; `capture_console` returns `console`, `console_port` and `console_port_source` as `run_firmware` does, and `capture_swo` returns `swo`; `run_firmware` echoes `elf_path` and `flash_firmware` echoes `image_path`; `flash_firmware`'s `detail` is `programmer_output`; `read_fault_status`'s `debug_halt_reason` is `debug_events_since_cleared`; `locate_fault` gives a place in the source as function, file, line and address, as `run_until` does; a register's `fields` are numbers, with ST's explanations in `field_meanings`. `run_until` reports `target_was_reset`.
- `core_registers` gives the same registers whichever way it reads them, and `core_registers` and `locate_fault` say from DHCSR whether the core runs afterwards.
- A call stack names a file as addr2line does, "src/main.c" rather than "main.c".
- Result text is cut to what changes what the agent does: `board_state` gives each family a short `note`, and the notes on halts, watchpoints, trace, buffered console text, read effects and fault registers are a sentence or two. The connect-failure hint names the likely causes and what to do about each.
- The tool descriptions say that `reset_target` cannot get past a debug port the running program closed and `board_state` can, and that the default `keep_probe` resets a core that locks up.
- A `STLINK_MCP_CLT_ROOT` that names no complete install stops start-up with the reason, as the other settings do.
- On an STM32N6 in flash boot, `flash_firmware` refuses before ST's flash loader runs, since that loader then hangs the core until a power cycle.
- The STM32N6 entry no longer says it boots only signed images: an image with ST's header and no keys boots on a part whose secure-boot fuse is not blown. `flash_firmware`'s next step and `board_state`'s note say how to add that header and how to open debug in the image; the connect-failure and reset hints say that a flash-booted N6 stays closed unless its image opens debug.
- `list_probes` returns the programmer's errors beside the probes it listed, since a probe this user cannot open prints an error and no entry.

### Fixed

- `locate_fault` connected with resets allowed, and a reset loses the fault it looks for; it now never resets and refuses instead.
- `sample_values` and `disassemble` raised on hex digits without their `0x` prefix, such as "08000000", and returned no status; they now refuse them, as `read_memory` does.
- `run_firmware` connected, possibly resetting the board, before finding that the ELF did not exist; it now checks first, and its refusals after the connect say whether the board was reset.
- A failed `capture_swo` did not say that the trace output may have been left on.
- A relative `elf_path` or `image_path` was looked up from the server's own working directory, which the README's client set-up makes the server's checkout, and failed as a missing file; it is now refused, naming that directory. `~` is expanded in every tool, where some did not. `board_state` given a missing ELF returned program-counter places with no source and no error; it now refuses before connecting.
- A failed `run_firmware` did not say that the GDB server it started had reset and halted the target, and `run_until`'s refusals after attaching did not say that GDB had halted the core.
- An exception a tool did not handle, such as the programmer timing out on a hung probe in `board_state`, reached the agent with no status; it is now a `failed` result that says whether the target was touched is not known.
- A probe serial typed in lower case was not matched to the server holding that probe, so another call could reset beside it. Serials are now taken in upper case, as the programmer prints them.
- A GDB server that failed to start, such as on a target that would not connect, left the folder for its log in the temporary directory. It is now removed.
- `release_probe` read the core's state over SWD after a run over JTAG; it now uses the interface of the server it released.
- A run that could not be sampled because the GDB server keeps the probe to itself now says why, and that `keep_probe=False` would let it be sampled.
- `read_memory` read an ADC's common data register without a word, though on an STM32H743 reading it cleared EOC as reading the ADC's own data register does. CDR is now on the read-effects list.
- Tools that halt the core to read it leave it as they found it. In shared mode a halt made through the programmer outlived the command, so `locate_fault` left a running core halted; and `core_registers` through GDB resumed a core it found halted.
- A board released while ST's `stlink-server` ran could not be reached again: the daemon kept the probe open, a plain connect failed with DEV_CONNECT_ERR, which the parser dropped, and `list_probes` showed the board with no firmware. DEV_CONNECT_ERR is now reported and explained.
- Reads that returned wrong data with no error were reported `verified`: on an STM32H743 whose core slept on WFI, every address read 0x01010001. Identification and every memory read now also read the core's CPUID register, and fail when its top byte is not Arm's 0x41.
- Texts an agent reads that had gone out of date: which tools refuse beside a server that keeps the probe to itself, that tool calls run one at a time (calls on different probes run together), and that `core_registers` cannot reach a held probe.

## [0.7.0] - 2026-10-03

Run on an STM32H7, an STM32N6 and an STM32F407 board on 2026-10-03: `tools/hwcheck` passed 80 of 80 checks with `--f4`, `--f4-console`, `--jtag` and `--flash`, and 74 of 74 without `--flash` on the release commit. On a Windows 11 laptop with an STM32N6 board, `tools/hwcheck --n6` passed 14 of 14. docs/findings.md has what was run and what each README promise rests on.

### Changed

- Without udev's `/dev/serial/by-id`, a named probe's console is found by the USB serial number pyserial reports for each port, instead of by listing every probe. On Windows that found COM3 for an STLINK-V3 on the first try.
- On Windows the toolchain's executables are looked for with `.exe`, and `C:\ST` is searched for an install, where STM32CubeCLT's installer puts it. A probe's console is reported as `COM3` rather than the `\\.\COM3` the programmer prints.

### Added

- `tools/hwcheck` checks that `run_until` refuses a wide watch and stops verified on a write by the caller, on the H7 and the F407, and with `--jtag` that it stops at a function and after a write over JTAG, both verified, and refuses SWD while that server holds the probe.
- Windows 11 measured with a NUCLEO-N657X0-Q: every check in `tools/hwcheck --n6` passed, 14 of 14, a run verified by its console text over COM3 among them. `tools/hwcheck` builds its images on Windows, removes them at the end unless `--keep-images`, and its N6 sequence now also reads by name and takes the call stack at a stop. Its new `n6_print` image prints on the N6 board's virtual COM port, so a run on the N6 is verified by console.

### Fixed

- `run_until` refuses a watch that is not 1, 2 or 4 bytes at an address aligned to its size. ST's GDB server accepted a 36-byte one, watched only its first byte, and the run reported no write while the program wrote the rest on every tick; a 2-byte one at an odd address was accepted and never fired.
- `run_until` fails at once, with GDB's reason, when GDB cannot insert a breakpoint, as at an address where nothing answers. It read GDB's `^running` as a run, waited out the time limit and an interrupt, and reported a target that did not stop, which had never run.
- A watchpoint result's `called_just_before` is now `returned_from`, is left out when the core is still inside that call, and is described as a candidate for the writer only. It had named a function that wrote nothing, on the H7 and the F407, whenever the caller made the write.
- An unexpected GDB error part-way through `run_until` left the core halted and the server the call had started holding the probe, with a failure that said neither. It now detaches, which resumes the core, stops that server, and says so in `core_after`.
- `run_until` and `core_registers` no longer reuse a GDB server held over the other interface, which they did without a word for a call that asked for SWD while a JTAG server held the probe; they refuse, as `run_firmware` already did. Their results now report the interface.
- `run_firmware` reports `target_was_reset`: true once its GDB batch has run, since the batch resets the target before it loads, and null with a note when the batch stopped early. Its docstring said so; its result did not.
- Texts an agent reads no longer send it to `release_probe` before `core_registers`, which works through a held GDB server, nor to `run_firmware`'s server to hold a core stopped, which is `run_until`'s job. On an STM32N6 a needless release can close the debug port until a power cycle. `release_probe` now names the tools that do need it, as measured on the H7 and the N6.
- Every tool's description is within the 80 words the repo sets, checked as an MCP client sees it; `capture_console` and `capture_swo` were over.
- A breakpoint's kind reads `software` rather than `software, in RAM`: ST's GDB server describes everything but flash as read-write, ROM included.
- A register named after its peripheral's type, as the STM32N657 file names USART1's (`USART_BRR`), is found by `USART1.BRR`, and one the file gives in two forms (`USART_ISR_FIFO_ENABLED`, `USART_ISR_FIFO_DISABLED`) is refused with both named, where it was "no register".
- Tests that assumed POSIX path separators, symlinks or chmod now pass or skip on Windows, and `.gitattributes` keeps git from rewriting the line endings of captured fixtures on checkout.

## [0.6.0] - 2026-10-02

Run on an STM32H7, an STM32N6 and an STM32F407 board on 2026-10-02: `tools/hwcheck` passed 68 of 68 checks with `--f4`, `--f4-console`, `--jtag` and `--flash`. docs/findings.md has what was run and what each README promise rests on.

### Added

- `read_memory` reads a variable by name, decoded with the ELF's types, and `verified` only when the target runs that ELF; and a peripheral register by name, decoded into its fields from ST's SVD files, with the file chosen from the board name or the device name and used only where every candidate file agrees. Measured on an STM32H7 and an STM32F407.
- `pyelftools` is a dependency, for reading types from an ELF's DWARF.
- `run_until`, which runs the target until it reaches a location or writes a variable and holds the core there, with the call stack, `verified` only on the Debug Fault Status Register the GDB server read at the stop. Measured on an STM32H7, an STM32F407 and an STM32N6.
- `core_registers` takes `elf_path` and returns the call stack with each frame's arguments and locals, through GDB, and works while `run_until` holds the core.
- `release_probe` reads DHCSR after releasing a core `run_until` held, and says whether it runs again.
- `tools/hwcheck` reads a variable and a register by name on the H7 and the F407, and checks `run_until`, the call stack and the resume on all three boards.

## [0.5.0] - 2026-10-02

Run on an STM32H7, an STM32N6 and an STM32F407 board on 2026-10-02: `tools/hwcheck` passed 48 of 48 checks with `--f4`, `--f4-console`, `--jtag`, `--flash` and `--n6-closed`. docs/findings.md has what was run and what each README promise rests on.

### Added

- `capture_swo`, which captures what the target writes to an ITM stimulus port over the SWO pin, through STM32_Programmer_CLI's own SWO capture. It takes the core clock, is `unverified` when nothing arrives, notes unreadable text, and says in every result that the target's trace output was left on. Measured on an STM32H7 and an STM32F407.
- `tools/hwcheck` checks SWO on the H7 and the F407, and with `--n6-closed` closes the N6's debug port the way ST's start-up code does and checks what the tools say to it.

### Changed

- A read that connects and then fails names its two measured causes: a range past the end of flash or RAM, and a debug access port that has closed, as an STM32N6 image's start-up code does. A GDB server that cannot attach no longer leaves its "check power and cabling" as the only explanation.
- A `flash_firmware` that fails because the programmer could not erase now says first that write protection in the option bytes fails this way, and how to show it. Measured on an STM32F407 with one sector protected.

## [0.4.0] - 2026-10-02

Run on an STM32H7, an STM32N6 and an STM32F407 board on 2026-10-02: `tools/hwcheck` passed 42 of 42 checks with `--f4`, `--f4-console`, `--jtag` and `--flash`. docs/findings.md has what was run and what each README promise rests on.

### Added

- STM32F4 (device 0x413) is a measured family: run on an STM32F407IGT6 board through an ST-LINK/V2, RAM run, faults, flash written and read back, reset to it.
- `tools/hwcheck` checks an STM32F407 board named with `--f4`, by its console on UART5 when `--f4-console` names an adapter, and prints every result since the previous check when one fails. A board whose first call fails gets one failure, not one per check.

### Fixed

- `run_firmware` no longer tells an unverified run on an unmeasured address that "the run itself IS verified"; the note now claims nothing about the run. Found on the first seeded part run on a board, an STM32F407.
- A connect that fails with "Serial number not found" no longer says the probe is absent: an ST-LINK/V2 too old for STM32CubeProgrammer fails the same way, and the hint now names STLinkUpgrade.
- `capture_console` returned `verified` with empty output when nothing arrived, which reads as a silent target. Silence is now `unverified`, naming the causes it cannot tell apart, a missing wire among them.

## [0.3.0] - 2026-10-01

Run on an STM32H7 and an STM32N6 board on 2026-10-01: `tools/hwcheck` passed 33 of 33 checks with `--jtag` and `--flash`. docs/findings.md has what was run and what each README promise rests on.

### Added

- `py.typed`, so type checkers read the package's annotations.
- A caller for every tool call, from the MCP context: one trace line per call on stderr, and `held_by` on refusals and releases naming the client whose run holds a probe.
- `tools/hwcheck`, which runs the README's promises against a NUCLEO-H743ZI and a NUCLEO-N657X0-Q through the server and prints PASS or FAIL for each, with the test images it builds from source.

### Fixed

- The warning that an access port was never measured now rides on every tool that connects and on their failures, not only on `board_state`, and is no longer skipped when a probe's remembered port differs from the assumed one.
- The newest complete STM32CubeCLT install is used, not the newest one with a programmer; `--check` names a newer install it passed over, and the error for an incomplete one names the others.
- `list_probes` is `unverified`, with the programmer's error lines, when it printed errors and listed no probe; an empty bus stays verified and points at `lsusb`.

### Changed

- `read_memory` and `read_fault_status` connect and read in one programmer call when the probe's remembered port is its family's measured port: an STM32N6 read takes 1.15 s against 2.3.

## [0.2.0] - 2026-10-01

These changes were run on an STM32H7 and an STM32N6 board on 2026-10-01; docs/findings.md has what was run and what it showed.

### Fixed

- `board_state` reports `board` as null for a probe with no board of its own, as `list_probes` does, where it passed on the banner's `--`.
- A GDB server that starts but never opens its port is reported with its own output.
- `run_firmware` no longer counts console output the probe kept while its serial port was closed. That output comes from whatever ran before, and it verified a run of an image that printed nothing. `run_firmware` and `capture_console` report it apart, as `console_buffered`, and never as evidence.
- `core_registers` no longer reports `"halted": true` or says the core is still halted; a programmer halt does not outlive its command. It reports `halted_to_read_registers`.
- `target_was_reset` is set when a resetting connect is issued, whether or not it connects, and rides on the failures of `board_state`, `run_firmware`, `flash_firmware` and `core_registers`. The check of an unmeasured access port no longer sends a second reset pulse.
- `read_fault_status` refuses Cortex-M0, M0+, M1 and M23 cores, which have no fault status registers, and refuses a partial read; a clean read on an unrecognised core is `unverified`. Its next step names the stack the exception frame is on.
- `run_firmware` refuses an image with any loaded segment outside the SRAM region, not only one whose vector table is elsewhere, since GDB would program such a segment into flash.
- `run_firmware` keeps GDB's and the GDB server's output when a load fails, says when a failure leaves the probe held, and returns a result instead of raising when GDB ports run out.
- Tool calls issued together no longer race for GDB ports, servers or serial ports.
- An unusable `STLINK_MCP_GDB_PORT` or `STLINK_MCP_CONSOLE_BAUD` stops start-up with one line and exit 2 instead of a traceback.
- `capture_console` keeps what arrived when the port drops mid-capture.
- A source tree without git metadata, such as a GitHub ZIP download, installs, as version `0.0.0+unknown`. `CHANGELOG.md` is in the sdist.

### Changed

- Tool calls on different probes run at the same time; calls on one probe wait for each other, and a call that lists the probes runs alone. A call that names its probe no longer lists them, and finds its console by the probe's serial in `/dev/serial/by-id` on Linux. `probe_serial` must be hex digits, and every answer is checked to come from the probe named.
- `flash_firmware` reads the written range back and compares it with the image. Only an identical readback is `verified`; a difference is `failed` with the first differing address, and a readback that fails is `unverified`.
- `flash_firmware` takes raw binaries (`.bin`) only, and checks the file before touching the probe.
- `STLINK_MCP_CONSOLE` takes `SERIAL=path` pairs, which win over the probe list; a bare path applies only while one probe is attached. `run_firmware` now honours it, and results report `console_port_source`.
- `read_memory` and `flash_firmware` take an address as a hex string as well as an integer. `read_memory` requires aligned reads of at most 1024 bytes and fails a short read. Console captures are limited to 60 seconds.
- `mcp` is capped below 3. `__version__` comes from the installed metadata, and the server reports it in the MCP handshake.

### Added

- `console_garbled_note` on `run_firmware` and `capture_console` when most of the output is not text, which is what a wrong baud rate looks like.
- `locate_fault`, which finds the instruction that faulted from the exception frame: the stack from EXC_RETURN, the stacked PC and LR, and with the ELF their functions and lines.
- `interface`, `swd` or `jtag`, on every tool that connects. Results report the interface and the clock from the probe's connect banner, and a failed JTAG connect says the probe may not do JTAG.
- `stlink-mcp --check`, `--version` and `--help`.
- CI for the suite on Python 3.11 to 3.13 and for the sdist contents, and an end-to-end test over stdio.
- `CONTRIBUTING.md` and issue templates.

### Removed

- The fallback to the first `/dev/ttyACM*` when no probe listing was available.

## [0.1.0] - 2026-09-06

First tagged version. Alpha: any tool signature or result field may still change.

### Added

- Ten MCP tools: `board_state`, `run_firmware`, `flash_firmware`, `read_memory`, `core_registers`, `read_fault_status`, `capture_console`, `reset_target`, `release_probe`, `list_probes`. Every tool verifies its own effect before reporting success.
- Two device families measured on hardware, STM32N6 and STM32H7. A further 99 device IDs are seeded from STMicroelectronics' device databases, each value labelled with that provenance; four device IDs whose databases name several SRAM banks are refused rather than guessed until a board file settles them.
- Board files for describing a board the server does not know.
- Linux only, MIT licence.

[Unreleased]: https://github.com/dii/stlink-mcp/compare/v0.8.3...HEAD
[0.8.3]: https://github.com/dii/stlink-mcp/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/dii/stlink-mcp/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/dii/stlink-mcp/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/dii/stlink-mcp/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/dii/stlink-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/dii/stlink-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/dii/stlink-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dii/stlink-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dii/stlink-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dii/stlink-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dii/stlink-mcp/releases/tag/v0.1.0
