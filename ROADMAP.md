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
- **Breadth before depth.** DONE in phase 2: 2 device IDs measured, 99 more seeded from ST's
  own databases, 4 refused as ambiguous.

This plan was red-teamed before anything was built from it, and revised against the result.
Findings RT1 to RT5 are folded in below.

## Phase 0 - one existing bug that every phase sits on (DONE)

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
| N2 missing facts refuse rather than guess | Done. Four device IDs with ambiguous SRAM are refused rather than seeded, and the write path refuses an unmeasured base. Since 2026-09-28 `read_fault_status` refuses cores without fault registers and partial reads, and `read_memory` refuses a short read |
| N3 hidden state is reported | Done: reset, cache, degraded access port, held probe, including after a failed run, and where a console setting came from |
| N4 nothing perturbs silently | Done: `target_was_reset`, which since 2026-09-28 is set when a resetting connect is issued whether or not it connects. `core_registers` halts for its read and no longer claims the core stays halted |
| N5 runs on hardware that is not ours | **Done.** A TOML board file, loaded at start-up, keyed by device ID, everything in it `user-stated` |
| N6 facts record how they were established | **Done.** Four provenances, reported by `board_state`, and the write path refuses an unmeasured base |
| N7 flash, reset, interrogate all first class | **Done.** `reset_target` verifies against the reset vector; `read_fault_status` decodes the SCB fault registers to answer "why did it fault" |
| N8 interface is a parameter | **Done for SWD and JTAG** (2026-10-01): `interface` on every tool that connects, the banner's clock in the result, a held server never reused across interfaces. Measured on an ST-LINK/V2 and the N6's own probe. SWO and RTT are not started |
| N9 no single-caller assumptions | **Not started.** Per-probe state is in module globals, and nothing records who asked for an operation. The process-wide lock that runs tool calls one at a time guards the hardware, which every caller shares, so it adds no caller state |

Standing check: `server.py` uses `mcp.server.MCPServer`. The lock pins SDK 2.0.0 and
`pyproject.toml` caps it below 3. On 2026-09-28 the latest published was 2.2.0, and the suite and
the stdio test in `tests/test_stdio.py` passed against it. Re-verify on every SDK bump rather than
assuming.

## Phase 1 - the core verbs, and letting the user describe the board

Three needs land together because they are one change to the same data path.

**A reset tool (N7). DONE.** The verification turned on something that had to be measured
rather than assumed: **the reset has to BE the connect.** On a NUCLEO-H743ZI whose reset
handler is at 0x0807FA98, a hotplug connect then `-rst -halt` leaves the PC at 0x0807FC2C and
the reverse order leaves it at 0x0807FC3A. Both reset the part correctly and then lose the
race to halt it. Only a resetting connect mode lands on the vector exactly, which is what
makes the check possible. Verified on two H7 boards with different reset handlers, and on the
STM32N6, where the core halts in its boot ROM at an address the table cannot predict and the
tool says so rather than inventing a comparison.

**Provenance (N6). DONE.** Every part-specific field records where it came from. The plan
named three sources and building it produced a fourth: `assumed`, for a value nobody measured
and no source states, which is what `DEFAULT`'s access port 0 actually is. The first version
of the code labelled those `measured`, which is the lie N6 exists to stop arriving through the
mechanism built to stop it, and it is now the first test in the provenance suite.

`board_state` reports provenance beside the values, so an agent knows which numbers a person
has actually seen work. `requires_signing` and `debug_closes_on_run` became tri-state on the
way, because a bool defaulting to False cannot tell "we checked and it does not" from "nobody
has said", and the provenance model would then report an origin for a claim the entry never
made.

The real design question of this phase was **what to do with a value that is not measured**,
and it is answered per path rather than globally. Refusing everywhere wastes the coverage;
acting everywhere breaks N1. So: act and let the observation decide where the operation is
reversible and an observation exists, which is the RAM path, and refuse where it is neither,
which is the write path. `flash_firmware` now refuses a base that is not `measured` unless the
caller names the address themselves, which makes it their claim rather than the table's guess.
(RT7)

The contribution rule is machine-checked rather than requested: any entry in the hand-written
`FAMILIES` dict carrying a non-`measured` value fails the build, and the three prose statements
that forbade what Phase 2 does have been reworded. (RT5)

**User-stated board details (N5). DONE.** A TOML file, one table per device ID, at
`~/.config/stlink-mcp/boards.toml` or wherever `STLINK_MCP_BOARDS` points. Loaded before the
server accepts anything, and a file it cannot use stops the server rather than starting
without the boards the user asked for. TOML rather than JSON for hex integer literals: a load
address written in decimal is a transcription error waiting to happen.

A user entry wins over the built-in table for that device ID, because the built-in entry
describes a device ID and the user is describing the board in front of them.

**The file cannot label its own values.** There is no way to declare a value `measured`, and
that refusal is load-bearing rather than pedantic: `measured` is what lets `flash_firmware`
write without an explicit address, this server cannot tell a measured value from a typed one,
so a config file that could assert it would let a typo unlock an irreversible operation. The
escape hatch is passing `address`, which records the claim as the caller's.

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
readback, and the seeding below does not change what it will write to. (RT7) It has had a
readback since 2026-10-01, and the refusal stays: a readback cannot tell a wrong address.

## Phase 2 - seed from the vendor database (DONE)

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

- **JTAG. DONE** 2026-10-01. A choice, defaulting to SWD, and told rather than falling back:
  a fallback would turn a failed JTAG connect into a working SWD one without the caller asking,
  and the result would then describe a connection nobody requested. Measured through an
  ST-LINK/V2 on the H743's JTAG connector and the N6 board's own probe; the H743's own
  STLINK-V3E does not do JTAG.
- **SWO or ITM, and RTT.** A board with no virtual COM port currently has no verification
  path at all, which is exactly the case where verification matters most.
- Make the console a peer of the debug interfaces rather than a special case bolted to
  `run_firmware`.

## Phase 5 - depth, chosen for the agent user

- **Fault decoding. DONE.** `read_fault_status` reads the six SCB fault registers (CFSR,
  HFSR, DFSR, MMFAR, BFAR, AFSR) without halting or resetting the target, decodes every
  set bit, and reports a plain-English diagnosis with the faulting address when the hardware
  recorded it. When a fault is active the result includes a next step telling the agent how
  to find the faulting instruction via the exception frame on the stack, and since 2026-09-28
  which stack, from EXC_RETURN. The decode logic is pure and tested offline, by the ordinary
  suite and by the blind suite in `tests/blind/`. The registers are at fixed Cortex-M addresses
  and do not vary by family, which is why this was built before JTAG. Cores without them,
  Armv6-M and Armv8-M Baseline, are refused rather than read.
- **Flash readback verification. DONE** 2026-10-01. `flash_firmware` reads the written range
  back with the programmer's upload and compares it with the image itself, rather than parsing
  `-v`'s banner, and only an identical readback is `verified`. First run on hardware the same
  day, an H7 at 0x08000000. The N6 path through the external loader has not been run.
- Halt, resume, step and memory writes, if real use asks for them.

## Phase 6 - reach

- **Attachment ergonomics.** Installing it and pointing an agent client at it should not
  require reading the source. The self-check is DONE, `stlink-mcp --check`; a generated client
  config is not.
- **CI. DONE** 2026-09-28: the suite on Python 3.11 to 3.13, with no hardware attached, and the
  check that the sdist file list equals `git ls-files`, run on every push.
- **Windows.** Executable names and device paths are wrong throughout, and a large share of
  STM32 developers are there.
- Contributing guide asking for captured `-l` output on any parsing problem, issue templates,
  and a changelog: DONE. `py.typed`: not done.
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
