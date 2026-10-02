# Changelog

Notable changes to stlink-mcp are recorded here. The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The version is read from the git tag; a build past the last tag reports the tag plus the number of commits since it, as in `0.1.0+3`.

## [Unreleased]

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

[Unreleased]: https://github.com/dii/stlink-mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/dii/stlink-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dii/stlink-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dii/stlink-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dii/stlink-mcp/releases/tag/v0.1.0
