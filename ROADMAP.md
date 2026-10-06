# Roadmap

The intent, and the needs N1 to N9 it is broken into, are in [AGENTS.md](AGENTS.md). This file says where the code stands against those needs and what comes next. The measurements behind each "done" are in [docs/findings.md](docs/findings.md).

Three decisions set the shape:

- **One user, one machine, boards on their desk.** No authentication, approvals or team access. N9 keeps that addable later, so nothing may assume a single implicit caller.
- **The primary user is an agent, with a human supervising.** Where the two want different things, the agent's need not to be misled wins. That is why fault decoding came before a nicer console, and why Windows comes after evidence quality.
- **Breadth before depth.** Three device IDs are measured on hardware: 0x413 on an STM32F407, 0x450 on an STM32H743 and 0x486 on an STM32N657. Other parts that report one of those IDs get the measured values too. The rest of the range is seeded at run time from the user's own STM32CubeProgrammer database and labelled `vendor-db`; device IDs whose database names several separate SRAM banks are refused rather than seeded.

## Where it stands

| Need | State |
| --- | --- |
| N1 verdict and evidence | Done. Every result is `verified`, `unverified` or `failed`, built by constructors that need the evidence; no tool sets `ok` itself, and a contract test enforces it |
| N2 missing facts refuse rather than guess | Done. Device IDs with ambiguous SRAM are refused rather than seeded; the write path refuses an unmeasured flash base; `read_fault_status` refuses cores without fault registers and partial reads; `read_memory` refuses a short read |
| N3 hidden state is reported | Done: reset, cache, degraded or unmeasured access port, held probe, where a console setting came from, and console output the probe kept from before a run |
| N4 nothing perturbs silently | Done: `target_was_reset` is set whenever a resetting connect is issued, whether or not it connected; halts are reported in `halted_during_call` and `core_after`; `locate_fault` never resets |
| N5 runs on hardware that is not ours | Done. A TOML board file, loaded at start-up and keyed by device ID, with everything in it `user-stated` |
| N6 facts record how they were established | Done. Four provenances, `measured`, `vendor-db`, `user-stated` and `assumed`, reported by `board_state`; the write path refuses an unmeasured base |
| N7 flash, reset, interrogate all first class | Done. `reset_target` verifies against the reset vector; `read_fault_status` and `locate_fault` say why and where a fault happened; `flash_firmware` reads back what it wrote |
| N8 interface is a parameter | Done for SWD, JTAG and SWO: `interface` on every tool that connects, and `capture_swo` for an ITM port. RTT is not built |
| N9 no single-caller assumptions | Started. Every tool call carries a caller (the client's declared name and version, its connection and the request) and is traced on stderr, and each held GDB server records who acquired it. Per-probe state still sits in module globals, and there is no per-caller policy |

The server uses the MCP SDK's `mcp.server.MCPServer`. The lock pins SDK 2.0.0 and `pyproject.toml` caps it below 3; re-run the suite and `tests/test_stdio.py` on every SDK bump rather than assuming.

## Version 1.0

What 1.0 does: an agent can load, run, flash and reset a board, read its output, read memory by variable or register name, sample values while the program runs, stop at a location or a write, and find where it crashed and why. All of this is built.

Not in 1.0: single-stepping, memory writes, RTT, and breakpoint lists or conditions.

The gates before 1.0:

1. Every promise in the README has a measurement behind it in the ledger in [docs/findings.md](docs/findings.md), and `tools/hwcheck` checks it on the boards.
2. The tool signatures and result fields are declared stable, as a beta.
3. One report from a user outside the project, on a board the README does not list.
4. Windows is either supported, after the checks not yet run there (SWO, JTAG, writing flash, and the H7 and F407 boards), or documented as run once on one board.
5. The README is read by the author before the repository goes public.

## After 1.0

- **Confirming a seeded part's layout.** A `characterise_target` tool that exercises the values seeded for a part, reports field by field which were confirmed, and emits an entry for `quirks.py` with `measured` only for those, so a user with a board becomes a source of measured data.
- **RTT, or what it is for.** RTT needs no pin, so it reaches boards whose SWO is not wired, but STM32CubeCLT does not support it: it would mean reading the target's ring buffer through memory reads while the program runs. `sample_values` already covers watching values; whether a ring-buffer reader is worth adding depends on real use.
- **The console as a peer of the debug interfaces**, rather than a part of `run_firmware`.
- **A generated client configuration**, so attaching the server needs no hand-written JSON.
- **N9.** As each change touches per-probe state, it moves to something keyed by caller, so adding identity and per-caller policy later is a feature rather than a rewrite.
- **A second backend**, OpenOCD or pyOCD. This needs an abstraction layer that deliberately does not exist yet, so it is last and may never be worth it.

## Not on this roadmap

Building firmware, signing images, a human-facing debugger interface, team access control, and replacing STM32CubeProgrammer. See the non-goals in [AGENTS.md](AGENTS.md).

## Open measurement

An ST-LINK/V2-1 has never been attached, so its `-l` listing has never been parsed. The probe-listing issue template asks for one.
