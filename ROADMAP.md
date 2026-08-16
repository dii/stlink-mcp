# Roadmap

The intent, and the needs it decomposes into, are in [AGENTS.md](AGENTS.md). This file is how
they get built, and the order is derived from them rather than from a list of missing
features.

Three decisions set the shape, all taken 2026-08-16:

- **Scope is one user, one machine, boards on their desk.** No authentication, no approvals,
  no team access. N9 keeps that addable later, so nothing here may assume a single implicit
  caller.
- **The primary user is an agent, with a human supervising.** Where the two want different
  things, the agent's need to not be misled wins. That is why fault decoding outranks a
  nicer console, and why Windows sits below evidence quality.
- **Breadth before depth.** Only 2 of 104 device IDs are characterised, so the tool refuses
  to run firmware on most STM32 parts. Widening that is worth more than deepening two.

## Where it stands

| Need | State |
| --- | --- |
| N1 verdict and evidence | Done. `result.verified/unverified/failed`, no tool sets `ok`, enforced by a contract test |
| N2 missing facts refuse rather than guess | Done for RAM execution and external flash |
| N3 hidden state is reported | Done: reset, cache, degraded access port, held probe |
| N4 nothing perturbs silently | Done: `target_was_reset`, `core_registers` documented as halting |
| N5 runs on hardware that is not ours | Half. Quirks are data, but describing a board means editing the installed package |
| N6 facts record how they were established | **Not started.** Blocks phases 2 and 3 |
| N7 flash, reset, interrogate all first class | **Reset missing.** It only happens as a side effect of something else |
| N8 interface is a parameter | **Not started.** `port=SWD` hardcoded in `probe.py:164`, `-d` in `runner.py:145`. No JTAG |
| N9 no single-caller assumptions | **Not started.** Per-probe state is in module globals |

Standing check, currently passing: the MCP SDK is 2.0.0, which is the latest published, and
`server.py` uses `mcp.server.MCPServer`. Re-verify on every SDK bump rather than assuming.

## Phase 1 - the core verbs, and letting the user describe the board

Three needs land together because they are one change to the same data path.

**A reset tool (N7).** Reset is one of the three things the intent promises and there is no
way to ask for it. It happens today only inside `identify` and the GDB batch. It needs to be
a tool, to take the reset kind (hardware pulse, software, connect-under-reset), and to report
what it actually achieved, since on some parts a reset does not reopen a closed access port
and saying so is the whole point.

**Provenance (N6).** Every part-specific field records where it came from: `measured`,
`vendor-db`, or `user-stated`. Nothing else in this file can be built honestly without it,
because the moment a value arrives from anywhere but a board, presenting it like a measured
one breaks the project's central claim inside its own data. `board_state` reports provenance
beside the values, so an agent knows which numbers a person has actually seen work.

The real design question of this phase: **what to do with a value that is not measured.**
Refusing wastes the coverage. Acting silently breaks N1. The intended answer is to act,
verify by observation, and return `verified` only on the observation, with `unverified` and a
named reason when no observation is available.

**User-stated board details (N5).** A user must be able to say what their board is without
editing the installed package: a config file or a tool parameter carrying the same fields the
quirks table holds. This is the third provenance and it is what makes "provide details about
the board attached" real rather than a code contribution.

## Phase 2 - seed from the vendor database

**Measured 2026-08-16, before this plan was written.** STM32CubeProgrammer 2.23.0 ships 104
device databases at `STM32CubeProgrammer/Data_Base/STM32_Prog_DB_0x*.xml`, one per device ID,
covering 32 series from C0 through to N6, MP and V8:

- **103 of 104** carry a named SRAM region with both address and size.
- **94 of 104** carry a flash base at `0x0800xxxx`.
- The values agree with what this project measured by hand. 0x450 gives `0x24000000` size
  `0x80000`, exactly the H7 entry's `ram_load_base`.

So the two fields that currently force a part to refuse a run are already on the user's own
disk, for effectively the whole range. They arrive with provenance `vendor-db` and are
treated accordingly.

What the database does **not** give, and what therefore still comes from measurement or from
the existing scan:

- **Access port.** Not in the database. Nearly every part uses 0 and the N6 uses 1, and
  `Probe.identify` already scans both, so a seeded entry can leave this to the scan.
- **Signing, external loaders, and image behaviour** such as `debug_closes_on_run`. These
  belong to a boot design or a vendor startup file, not to a device ID.

**The known trap, and the first thing to test.** For the N6 the database gives SRAM at
`0x34180400`, which is this project's `vector_table_addr` and not its `ram_load_base` of
`0x34180000`. The difference is 0x400, the signing header. Most parts have no header and the
two coincide, but the mapping must be worked out and tested rather than assumed, and the part
we know best is the one that proves a naive reading wrong.

Open choice: parse the databases at runtime from the user's own install, which stays current
and vendors nothing, or generate a checked-in table, which survives an install that lacks the
directory. Runtime is preferred with a fallback.

## Phase 3 - close the loop, so users produce measured entries

After Phase 2 most parts have a proposed layout that nobody has run a board against. This
turns every user with a board into a source of measured data.

- A `characterise_target` tool that takes the proposed values, exercises them, and reports
  field by field which were confirmed and which were not.
- It emits a quirks entry ready to contribute back, with provenance set to `measured` only
  for the fields it actually confirmed.
- This is the argument the project makes about its own tools, applied to its own data: do not
  report a value as known because a vendor document says so.

## Phase 4 - the interfaces (N8)

- **JTAG.** `port=SWD` and the gdbserver's `-d` become a choice, defaulting to SWD and
  falling back or being told. Some targets and some board wirings only offer JTAG.
- **SWO or ITM, and RTT.** A board with no virtual COM port currently has no verification
  path at all, which is exactly the case where verification matters most.
- Make the console a peer of the debug interfaces rather than a special case bolted to
  `run_firmware`.

## Phase 5 - depth, chosen for the agent user

- **Fault decoding.** Read and interpret the Cortex-M fault registers to answer "why did it
  fault". Family independent, and it is the question an agent actually has when a run fails.
  Highest daily value of anything in this file.
- **Flash readback verification.** `flash_firmware` trusts the programmer's own "File
  download complete" and says so honestly in `verified_by`. The one operation that writes
  permanently is the one that does not read back and compare, which is an open N1 gap.
- Halt, resume, step and memory writes, if real use asks for them.

## Phase 6 - reach

- **Attachment ergonomics.** Installing it and pointing an agent client at it should not
  require reading the source. A generated client config, and a self-check that reports what
  it found, beats a README snippet.
- **CI.** All 97 tests run with no hardware attached and should run on every push, along with
  the assertion that the sdist file list equals `git ls-files`, currently run by hand.
- **Windows.** Executable names and device paths are wrong throughout, and a large share of
  STM32 developers are there.
- Contributing guide asking for captured `-l` output on any parsing problem, `py.typed`, and
  a changelog.
- OpenOCD or pyOCD as a second backend. This means introducing an abstraction layer that
  deliberately does not exist yet, so it is last and may never be worth it.

## Cross-cutting, not a phase

**N9, no single-caller assumptions.** Per-probe state currently sits in module globals. As
each phase touches that state it moves to something keyed by caller, so that adding identity
and per-caller policy later is a feature rather than a rewrite. No authentication is built.

## Not on this roadmap

Building firmware, signing images, a human-facing debugger interface, team access control,
and replacing STM32CubeProgrammer. See the non-goals in [AGENTS.md](AGENTS.md).

## Open measurement

No ST-LINK/V2 or V2-1 probe has ever been attached to the machine this was developed on.
Every board here is V3 generation. The parser is expected to handle an older probe, and that
expectation is reasoning rather than a measurement, which is why it is in the README
limitations and guarded by a test.
