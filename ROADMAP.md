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
- **Breadth before depth.** Only 2 of 103 device IDs are characterised, so the tool refuses
  to run firmware on most STM32 parts. Widening that is worth more than deepening two.

This plan was red-teamed before anything was built from it, per ADR-0018, and revised against
the result. Findings RT1 to RT5 are folded in below; the report is
`red-team-roadmap-2026-08-16.md`, which is kept locally and not published.

## Phase 0 - one existing bug that every phase sits on

`find_toolchain` promises "newest first when several are installed" and delivers the oldest.
`toolchain.py:80` sorts install directories with a plain string sort, so
`stm32cubeclt_1.9.0` orders ahead of `stm32cubeclt_1.22.0`. Today that means a user with two
installs silently runs the older programmer. From Phase 2 onward it means the seeded addresses
come out of a stale vendor database with no signal that a newer one was skipped, so it is
fixed first rather than inherited. (RT4)

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
quirks table holds, keyed by device ID, since `for_device_id` is the only entry point the code
has and the by-name lookup was deliberately removed. This is the third provenance and it is
what makes "provide details about the board attached" real rather than a code contribution.

Two consequences of that channel, both found by the red-team:

- **The consistency invariants have to move.** They exist today only as pytest
  parametrisations over the compile-time `FAMILIES` dict, so a runtime-supplied entry gets
  none of them. A user entry needs the same checks at the point it is loaded, not only in the
  test suite. (RT6)
- **Three places forbid what Phase 2 does and must be reworded in the same change.**
  `AGENTS.md` under "Quirks belong in the table", the `quirks.py` module docstring, and the
  user-facing string at `server.py:365` all say to fill in only values verified on hardware,
  with no provenance escape hatch. Leaving them is the same defect this repo has already
  shipped twice: each statement locally true, the whole self-contradicting, and the
  user-facing one is what a reader meets first. (RT5)

**And say plainly what the policy does NOT cover.** Act-then-observe is written for the RAM
path, where an observation exists and a wrong guess is undone by a reset. It is not a licence
for the write path. `flash_firmware` keeps its current behaviour until Phase 5 gives it a
readback, and the seeding below does not change what it will write to. (RT7)

## Phase 2 - seed from the vendor database

**Measured, then re-measured after the red-team found the first survey wrong.** Every figure
here comes from `tools/survey_device_db.py`, which is checked in so the numbers can be re-run
against any install rather than believed. STM32CubeProgrammer 2.23.0 ships **104 database
files holding 103 distinct device IDs**, covering 32 series from C0 through to N6, MP and V8:

- **104 of 104** carry a named SRAM region with both address and size.
- **94 of 104** carry a flash base at `0x0800xxxx`, and that address is **unambiguous in
  every one of them**.
- The values agree with what this project measured by hand. 0x450 gives `0x24000000` size
  `0x80000`, exactly the H7 entry's `ram_load_base`.

So the two fields that currently force a part to refuse a run are already on the user's own
disk, for effectively the whole range. They arrive with provenance `vendor-db` and are
treated accordingly.

**Why there is a script.** The first version of this section said "103 of 104", and that
number measured the survey's own regex rather than the data: `0x45A` writes its address as
`"0x20004000U"`, with a trailing `U` that a strict hex match rejects, and two other files
write a size with no `0x` prefix at all. A parser for this data has to tolerate ST's own
inconsistency, and a figure quoted from a shell one-liner nobody can re-run is exactly the
kind of claim this project exists to distrust. (RT3)

**One device ID appears in two files.** `0x485` is declared by both `STM32_Prog_DB_0x485.xml`
and `STM32_Prog_DB_0x485_swv.xml`. They agree on the fields we would seed, so it is harmless
today, but a glob into a device-ID-keyed dict resolves the collision by file order, which is
not a decision anyone made. (RT3)

**SRAM is not always one region, and the rule for choosing is stateable.** 14 of the 103
device IDs offer more than one SRAM base address:

- **10 are TrustZone secure and non-secure views of the same memory**, differing by exactly
  `0x10000000` (`0x2xxxxxxx` against `0x3xxxxxxx`). These resolve mechanically: take the
  non-secure view.
- **4 are genuinely separate banks, and a real choice**: `0x42B`, `0x42C`, `0x472` (three
  banks) and `0x49A`. **Seeding must refuse these rather than pick one.** That is N2 applied
  to our own data, and it costs 4 device IDs out of 103.

The combined `(address, size)` count is 16 rather than 14, but the extra two differ only in
size, which is a die or package variant and does not affect a base address. Quoting 16 would
repeat the 103 mistake in a new place. (RT2)

**Access port cannot be left to the existing scan, and the scan needs fixing first.** The
first version of this plan said a seeded entry could leave access port alone, because
`Probe.identify` scans ports 0 and 1. It does not, in precisely the case that matters. It
returns as soon as AP0 answers, and `_settle_access_port` then compares against
`info.quirks.access_port`, which for a seeded entry carrying no measured port is `0`. `0 == 0`
returns immediately and **AP1 is never tried**. A seeded family needing a non-zero port would
report AP0 with no `degraded_reason`, and the first symptom would be "Failed to halt target",
which `AGENTS.md` records as having already cost this project time once.

So Phase 2 either fixes the scan to try every port when the family's own port is unmeasured,
or leaves access port absent so the part refuses. It does not inherit a claim that is false.
(RT1)

What the database does **not** give, and what therefore still comes from measurement:

- **Access port.** Not present in any of the 104 files, by both text and structural search.
- **Signing, external loaders, and image behaviour** such as `debug_closes_on_run`. These
  belong to a boot design or a vendor startup file, not to a device ID.

**The known trap, and the first thing to test.** For the N6 the database gives SRAM at
`0x34180400`, which is this project's `vector_table_addr` and not its `ram_load_base` of
`0x34180000`. The difference is 0x400, the signing header. Measured across every SRAM base in
the set, **exactly one carries a 0x400 offset and it is the N6**, with 106 of 119 bases
perfectly round, so treating the database address as the load base is right in the common
case. That is a reason to expect the mapping to be simple, not a reason to skip testing it,
and the part we know best is the one that proves a naive reading wrong.

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
