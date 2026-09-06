# Changelog

Notable changes to stlink-mcp are recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The version is read from the
git tag; a build past the last tag reports the tag plus the number of commits since it, as in
`0.1.0+3`.

## [Unreleased]

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
