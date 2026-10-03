# Changelog

Notable changes to stlink-mcp are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The version is read from the git tag; a build past the last tag reports the tag plus the number of commits since it, as in `0.1.0+3`.

## [Unreleased]

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

[Unreleased]: https://github.com/dii/stlink-mcp/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/dii/stlink-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/dii/stlink-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/dii/stlink-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dii/stlink-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dii/stlink-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dii/stlink-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dii/stlink-mcp/releases/tag/v0.1.0
