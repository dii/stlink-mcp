# Roadmap

The intent and the six needs it decomposes into are in [AGENTS.md](AGENTS.md). This file is
how they get built, and the order is derived from them rather than from a list of missing
features.

Two decisions set the shape, both taken 2026-08-16:

- **The primary user is an agent, with a human supervising.** Where the two want different
  things, the agent's need to not be misled wins. That is why fault decoding outranks a
  nicer console, and why Windows support sits below evidence quality.
- **Breadth before depth.** The binding constraint today is that only two device families
  are characterised, so the generic fallback refuses to run firmware on the large majority
  of STM32 parts. Widening that is worth more than deepening the two we have.

## Where it stands

| Need | State |
| --- | --- |
| N1 result carries verdict and evidence | Done. `result.verified/unverified/failed`, no tool sets `ok`, enforced by a contract test |
| N2 missing facts refuse rather than guess | Done for RAM execution and external flash |
| N3 hidden state is reported | Done: reset, cache, degraded access port, held probe |
| N4 nothing perturbs silently | Done: `target_was_reset`, `core_registers` documented as halting |
| N5 runs on hardware that is not ours | Half. Quirks are data, but only 2 of 104 device IDs are populated |
| N6 facts record how they were established | **Not started.** Blocks everything below |

## Phase 1 - provenance, which is N6

Nothing else in this file can be built honestly until a field in the quirks table can say
where it came from. Today every value in it is measured, so the distinction has never been
needed. The moment a vendor database seeds a value, presenting the two the same way would
break the project's central claim in its own data.

- Each part-specific field carries a provenance: `measured`, `vendor-db`, or absent.
- `board_state` reports provenance alongside the values, so an agent reading a result knows
  which numbers a person has actually seen work.
- Decide the policy for acting on a `vendor-db` value, and this is the real design question
  of the phase. Refusing outright wastes the coverage. Acting silently breaks N1. The
  intended answer is to act, verify by observation, and return `verified` only on the
  observation, with `unverified` and a clear reason when the observation is unavailable.
- The `notes` field stays prose and stays mandatory. Provenance is a machine-readable
  addition to it, not a replacement for it.

## Phase 2 - seed from the vendor database

**Measured 2026-08-16, not assumed.** STM32CubeProgrammer 2.23.0 ships 104 device databases
at `STM32CubeProgrammer/Data_Base/STM32_Prog_DB_0x*.xml`, one per device ID, covering 32
distinct series from C0 through to N6, MP and V8:

- **103 of 104** carry a named SRAM region with both address and size.
- **94 of 104** carry a flash base at `0x0800xxxx`.
- The values agree with what this project measured by hand: 0x450 gives `0x24000000` size
  `0x80000`, which is the H7 entry's `ram_load_base` exactly.

So the two fields that currently force a part to refuse a run are already on the developer's
own disk, for effectively the whole range.

What the database does **not** give, and what therefore still has to come from measurement
or from the existing scan:

- **Access port.** Not in the database. Nearly every part uses 0 and the N6 uses 1, and
  `Probe.identify` already scans both, so a seeded entry can leave this to the scan.
- **Signing, external loaders, and image behaviour** such as `debug_closes_on_run`. These
  are properties of a boot design or of a vendor startup file, not of a device ID.

**The known trap, and it is the first thing to test.** For the N6 the database gives SRAM at
`0x34180400`, which is this project's `vector_table_addr` and not its `ram_load_base` of
`0x34180000`. The difference is 0x400, the signing header. So the database's SRAM address is
not reliably the load base on parts that carry a header. Most parts have no header and the
two coincide, but the mapping must be worked out and tested rather than assumed, and the N6
is the case that proves a naive reading wrong.

Open choice: parse the databases at runtime from the user's own CubeProgrammer install, or
generate a checked-in table. Runtime is preferred, since it stays current with whatever the
user has installed and vendors nothing, but it needs a fallback for an install that lacks
the directory.

## Phase 3 - close the loop, so users produce measured entries

With Phase 2, most parts have a proposed layout that nobody has run a board against. Phase 3
turns every user with a board into a source of measured data.

- A `characterise_target` tool that takes the proposed values, exercises them, and reports
  field by field which were confirmed and which were not.
- It emits a quirks entry ready to contribute back, with provenance already set to
  `measured` only for the fields it actually confirmed.
- This is the same argument the project makes about its own tools, applied to its own data:
  do not report a value as known because a vendor document says so.

## Phase 4 - depth, chosen for the agent user

- **Fault decoding.** Read and interpret the Cortex-M fault registers to answer "why did it
  fault". Family independent, and it is the question an agent actually has when a run fails.
  Highest daily value of anything in this file.
- **Flash readback verification.** `flash_firmware` currently trusts the programmer's own
  "File download complete" and says so honestly in `verified_by`. The one operation that
  writes permanently is the one that does not read back and compare, which is an N1 gap.
- **Console beyond UART.** SWO or ITM, and RTT. A board with no virtual COM port currently
  has no verification path at all, which is exactly the case where verification matters
  most.
- Halt, resume, step, and memory writes, if real use asks for them.

## Phase 5 - reach

- **CI.** All 97 tests run with no hardware attached and should run on every push, along
  with the assertion that the sdist file list equals `git ls-files`, which is currently run
  by hand.
- **Windows.** Executable names and device paths are wrong throughout, and a large share of
  STM32 developers are there.
- Contributing guide asking for captured `-l` output on any parsing problem, `py.typed`, and
  a changelog.
- OpenOCD or pyOCD as a second backend. This means introducing an abstraction layer that
  deliberately does not exist yet, so it is last and may never be worth it.

## Not on this roadmap

Building firmware, signing images, a human-facing debugger interface, and replacing
STM32CubeProgrammer. See the non-goals in [AGENTS.md](AGENTS.md).

## Open measurement

No ST-LINK/V2 or V2-1 probe has ever been attached to the machine this was developed on.
Every board here is V3 generation. The parser is expected to handle an older probe and that
expectation is reasoning rather than a measurement, which is why it is written into the
README limitations and guarded by a test.
