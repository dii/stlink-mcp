# Changelog

Notable changes to stlink-mcp are recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The version is read from the
git tag; a build past the last tag reports the tag plus the number of commits since it, as in
`0.1.0+3`.

## [Unreleased]

These changes were run on an STM32H7 and an STM32N6 board on 2026-10-01; docs/findings.md has
what was run and what it showed.

### Fixed

- `run_firmware` no longer counts console output the probe kept while its serial port was closed. That
  output comes from whatever ran before, and it verified a run of an image that printed nothing.
  `run_firmware` and `capture_console` report it apart, as `console_buffered`, and never as evidence.
- `core_registers` no longer reports `"halted": true` or says the core is still halted; a
  programmer halt does not outlive its command. It reports `halted_to_read_registers`.
- `target_was_reset` is set when a resetting connect is issued, whether or not it connects, and
  rides on the failures of `board_state`, `run_firmware`, `flash_firmware` and
  `core_registers`. The check of an unmeasured access port no longer sends a second reset pulse.
- `read_fault_status` refuses Cortex-M0, M0+, M1 and M23 cores, which have no fault status
  registers, and refuses a partial read; a clean read on an unrecognised core is `unverified`.
  Its next step names the stack the exception frame is on.
- `run_firmware` refuses an image with any loaded segment outside the SRAM region, not only one
  whose vector table is elsewhere, since GDB would program such a segment into flash.
- `run_firmware` keeps GDB's and the GDB server's output when a load fails, says when a failure
  leaves the probe held, and returns a result instead of raising when GDB ports run out.
- Tool calls issued together no longer race for GDB ports, servers or serial ports.
- An unusable `STLINK_MCP_GDB_PORT` or `STLINK_MCP_CONSOLE_BAUD` stops start-up with one line
  and exit 2 instead of a traceback.
- `capture_console` keeps what arrived when the port drops mid-capture.
- A source tree without git metadata, such as a GitHub ZIP download, installs, as version
  `0.0.0+unknown`. `CHANGELOG.md` is in the sdist.

### Changed

- `flash_firmware` reads the written range back and compares it with the image. Only an
  identical readback is `verified`; a difference is `failed` with the first differing address,
  and a readback that fails is `unverified`.
- `flash_firmware` takes raw binaries (`.bin`) only, and checks the file before touching the
  probe.
- `STLINK_MCP_CONSOLE` takes `SERIAL=path` pairs, which win over the probe list; a bare path
  applies only while one probe is attached. `run_firmware` now honours it, and results report
  `console_port_source`.
- `read_memory` and `flash_firmware` take an address as a hex string as well as an integer.
  `read_memory` requires aligned reads of at most 1024 bytes and fails a short read. Console
  captures are limited to 60 seconds.
- `mcp` is capped below 3. `__version__` comes from the installed metadata, and the server
  reports it in the MCP handshake.

### Added

- `stlink-mcp --check`, `--version` and `--help`.
- CI for the suite on Python 3.11 to 3.13 and for the sdist contents, and an end-to-end test
  over stdio.
- `CONTRIBUTING.md` and issue templates.

### Removed

- The fallback to the first `/dev/ttyACM*` when no probe listing was available.

## [0.1.0] - 2026-09-06

First tagged version. Alpha: any tool signature or result field may still change.

### Added

- Ten MCP tools: `board_state`, `run_firmware`, `flash_firmware`, `read_memory`,
  `core_registers`, `read_fault_status`, `capture_console`, `reset_target`, `release_probe`,
  `list_probes`. Every tool verifies its own effect before reporting success.
- Two device families measured on hardware, STM32N6 and STM32H7. A further 99 device IDs are
  seeded from STMicroelectronics' device databases, each value labelled with that provenance;
  four device IDs whose databases name several SRAM banks are refused rather than guessed until
  a board file settles them.
- Board files for describing a board the server does not know.
- Linux only, MIT licence.

[Unreleased]: https://github.com/dii/stlink-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dii/stlink-mcp/releases/tag/v0.1.0
