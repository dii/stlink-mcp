# Findings

The README states conclusions. This file holds the measurements behind them and
the things this project got wrong on the way to them, because a claim without
its provenance is the failure this project exists to remove. Dates are when the
measurement was made. Boards: NUCLEO-N657X0-Q, NUCLEO-H743ZI, STM32H753I-EVAL2,
all with on-board ST-LINK V3, driven by STM32CubeCLT 1.22.0 (STM32CubeProgrammer
2.23.0) on Fedora 44.

## The result contract came out of a defect in this tool (2026-08-15)

An early version returned `ok: true` alongside `verified_by: "nothing"`. A review
pass found the rule "a tool reports success only after checking" broken in four
tools in four different ways. The fix was to stop letting tools set `ok` at all:
three constructors, `verified`, `unverified` and `failed`, one of which refuses
to build without the evidence that justifies it. The test pinning that rule was
itself mutation-checked and failed on the first try, because a non-empty string
such as `"nothing"` passes a truthiness check.

## STM32N6: the debug access port closes when a typical image runs (2026-08-14 to 08-16)

**Observation (08-14).** Running an FSBL built from ST's template closes debug
access port 1 the moment the image starts. A reset does not reopen it; only
removing power does. This was first recorded as a family quirk of the part,
which was wrong: it is a property of the image.

**Cause (08-16).** One line at the end of ST's `system_stm32n6xx_fsbl.c`:

```c
RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;   /* deactivate SYSCFG clock */
```

`SystemInit` turns the SYSCFG clock on early so it can write `INITSVTORCR`, then
turns it off again on the way out. On the N6 that clock is what keeps access
port 1 alive. Remove that single write and the port survives: four consecutive
load-and-run cycles with no power cycle, against the stock image closing it on
the very next run of the same board.

**Method.** Bisecting `SystemInit`. Eliminated on the way: the boot ROM handoff
(a bare image with no startup chain leaves the port open), the startup
assembly, TrustZone and the SAU, `INITSVTORCR`, the PWR supply block, and every
peripheral reset in there.

**Two wrong confirmations.** The closure was "confirmed" twice by
`pgrep ST-LINK_gdbserver`, which matches nothing on Linux, ever: the kernel
truncates process names to fifteen characters, so the check reads exactly like
a clean control while a process is holding the probe. Confirm a probe is free by
port, with `ss -ltnp`, never by process name. Connect-under-reset (`mode=UR`) is
not a way out either; it fails on a healthy board too.

**Consequence.** `run_firmware` holds its GDB server across runs, so with a stock
image the cost is one power cycle per session rather than one per run.

## Reset before every load

An established GDB connection keeps working across repeated load-and-run
cycles, provided the target is reset before every load. The server's own
`--halt` resets once, when the server starts. Without an explicit reset per
load, each image inherits the previous run's live peripherals and interrupt
state and faults during start-up in a way that reads as a firmware bug. Found
by running many consecutive loads through one session; the GDB batch now
issues `monitor reset` before each load.

## The reset has to be the connect (2026-08-16)

NUCLEO-H743ZI, reset handler at `0x0807FA98`:

| sequence | PC afterwards |
| --- | --- |
| hotplug connect, then `-rst -halt` | `0x0807FC2C` |
| hotplug connect, then `-halt -rst` | `0x0807FC3A` |
| `mode=HWRSTPULSE` connect, then `-halt` | `0x0807FA98` |
| `mode=UR` connect | `0x0807FA98` |

Both non-resetting orders reset the part correctly and then lose the race to
halt it, landing hundreds of bytes into start-up where the PC cannot be
compared against anything. Only a resetting connect lands on the vector, which
is what makes `reset_target`'s check (PC against word 1 of the vector table)
possible. Verified on two H7 boards with different reset handlers. On the N6
the core halts in its boot ROM at an address the table cannot predict, and the
tool says so rather than inventing a comparison.

## A halt does not survive the invocation that made it (2026-08-17)

`reset_target` had a `leave_halted` option for one day. Sampling the PC across
separate reads after a reset-and-halt on a NUCLEO-H743ZI gave `0x0807FA98`,
then `0x0807FE96`, `0x08060AFC`, `0x08080482`; the STM32H753I-EVAL2 behaved the
same. The option reported `core_halted: true` while the core was demonstrably
running, so it was removed. Nothing in this project claims whether the core is
halted now: a static program counter is what a halted core looks like and also
what a tight loop looks like, and on an N6 sitting in its boot ROM the PC read
identically three times while proving nothing either way. To hold a target
stopped, use the GDB server that `run_firmware` keeps alive.

## Identity reads correctly on a port that cannot halt the core (2026-08-14)

On the N6, access port 0 reports device ID, name, CPU and revision perfectly
well while being unable to halt the core. `identify()` originally stopped at
the first port that answered. Now the family's recorded port wins, verified
before adopting, and any fallback is reported as a degradation. A wrong access
port and an absent target produce the same error from the vendor tools, which
is why `board_state` tries every port.

## The vendor database reports the N6 vector table, not its RAM base

ST's device database gives the N6's SRAM at `0x34180400`. That is the vector
table address, not the RAM load base of `0x34180000`, because the boot ROM
prepends a signing header. Nothing in the database says which of the two it is
reporting, which is why a measured entry always beats a seeded one. Four device
IDs (`0x42B`, `0x42C`, `0x472`, `0x49A`) name several separate SRAM banks in
their databases and are refused rather than guessed.

## A field named for the silicon held a fact about a startup file

The N6 port closure was first stored as `debug_closes_on_run`, a family flag.
It described what ST's startup file does, in a field named for what the part
does, and it took two days and the bisection above to get back out. The rule
since: the quirks table holds what is true of everything behind a device ID,
and anything that varies by board or by image stays out of it.

## Enumeration works while a server holds a probe

`list_probes` used to refuse whenever any GDB server was held, on an untested
assumption. Measured: `STM32_Programmer_CLI -l` listed all five attached probes
in 1.5 seconds with one held, the same as with none. Held probes are marked,
not hidden. What fails is connecting to a held probe, not listing it.

## The first ttyACM is the wrong board about half the time

`capture_console` once computed the ambiguity error and then fell through to
the first matching device path anyway. With two boards attached that is the
wrong board about half the time; with five it is a one-in-five pick. Silence
from the wrong board reads as "firmware not running", and output from the wrong
board reads as "it works". The port is now resolved from the probe serial via
the programmer's own listing, and the tool refuses when it cannot.

## The packaging tool publishes what git hides (2026-08-16)

Hatchling builds an sdist from the filesystem, honouring the repo's
`.gitignore` but not the user's global ignore file. A local review report
carrying an absolute path from the author's machine was in the built sdist
while correctly absent from every commit, and `git status` said nothing,
because from git's point of view nothing was wrong. The sdist now has an
explicit include list rather than an exclude rule, so the next locally-ignored
working file cannot be published the same silent way.

## Newest install, not alphabetically last

`find_toolchain` promised "newest first when several are installed" and
delivered the oldest: a plain string sort puts `stm32cubeclt_1.9.0` ahead of
`stm32cubeclt_1.22.0`. Fixed with a version sort before anything else was
built on it, because from the vendor-database seeding onward it would have
meant reading a stale database with no signal that a newer one was skipped.

## The MemManage bit map was wrong, and 52 tests agreed with it (2026-09-03)

`_MMFSR_BITS` placed MUNSTKERR at CFSR bit 5 and had nothing at bit 3. Arm's CMSIS
headers put MUNSTKERR at bit 3 and MLSPERR (MemManage during lazy floating-point state
preservation) at bit 5. A real unstacking fault therefore decoded to no faults at all,
`any_fault` false, "no fault is active": the confidently wrong answer this project exists
to prevent. The suite did not catch it because its tests were written from the table, one
of them asserting `1 << 5` decodes to MUNSTKERR. Found by a contract author working from
Arm's header with the implementation withheld (the blind-tests protocol, first run on this
repo), before any test was written. The same run found the diagnosis attached the fault
address to three specific bits rather than to the VALID flag, so a stacking fault with a
valid MMFAR got no address in its text while IACCVIOL, which never records one, did. Both
fixed the same day; the address is now reported for the register whenever its VALID flag
is set.

## The run path could write flash (red-team RT1, 2026-09-03)

`run_firmware` handed GDB an ELF and said `load`, and GDB writes each loadable segment to
the address the image carries. With a default flash-linked build (0x08000000, what
STM32CubeIDE and CMake produce), ST-LINK_gdbserver programs flash (that is what its `-cp`
path to CubeProgrammer is for; UM2576 says so), and the tool then reads a stack pointer and
program counter from the family's RAM vector table, which holds unrelated bytes, and starts
the core there. It reported `unverified`, or `verified` on a stale RAM image's output. The
project's own written policy treated the run path as reversible, which it was only for an
image linked for RAM, and nothing said the image had to be. Found by a red-team reviewer
reading the GDB script and the gdbserver's own help text; confirmed by an independent
skeptic against GDB's documentation and the gdbserver's binary. Fix: the image's loadable
segments are read before any server is started, and an image that does not cover the
family's vector table address is refused by name (`image.py`). A newcomer persona reading
the README had asked exactly this question, unprompted: whether a normal Debug build works.

## The field was not searched before building (2026-09-02)

This project was built on 2026-08-14 and went through four review passes without anyone
asking whether it already existed. The search happened on 2026-09-02, while checking a
proposed rename, and found shieldyguy/stm32-mcp (six months older), Helistana's CubeIDE
server, and, a day later, Adancurusul/embedded-debugger-mcp, the closest neighbour on this
project's own axis. A separate project would likely still have been the outcome, because
the difference is the result contract rather than a feature, but that should have been a
decision. The lesson went into the review checklist: search before the roadmap, and name
the neighbours in the README.

## What the neighbours report (read 2026-09-04)

The README's comparison with embedded-debugger-mcp rests on its source at commit 7113ae44
(2026-07-16), files `src/tools/debugger_tools/target_control.rs` and
`src/tools/debugger_tools/flash.rs`:

- `run` returns "Target resumed execution successfully! ... Status: Running" once
  `backend.run()` returns Ok. No read of the core follows.
- `reset` calls `backend.snapshot().await.ok()` and maps a failed snapshot to `(0, 0)`, so the
  message "Target reset completed successfully." can carry `PC: 0x00000000` when the read
  failed. That is the same shape as the "Start operation achieved successfully" that opened
  this project.
- `run_firmware` emits "Firmware is now running on target." after `core.reset()` and
  `core.run()` return Ok. The RTT attach in its step 4 is behind `args.attach_rtt`, and the
  closing line is emitted either way.
- `flash_program` takes `verify` (or the config's `verify_after_program`), and a separate
  `flash_verify` compares raw bytes against target flash. This project reads nothing back after
  `flash_firmware`, and its result says so.

The comparison is fair only while that is what their code does. Re-read before repeating it.

## Fixes that stopped at one tool (review, 2026-09-28)

A review before sharing read the code and reproduced its findings against a faked
programmer, with no board attached. Several were a defect already fixed once, surviving in a
sibling:

- `reset_target` stopped claiming the core was halted on 2026-08-17, and `core_registers`
  kept returning `"halted": true`.
- `reset_target` relies on a resetting connect pulsing nRST even when the port then does not
  answer. `identify()` set `target_was_reset` only when that connect succeeded, so
  `board_state` could restart an STM32N6 whose AP1 was closed and report it untouched. The
  tool-level tests replace `Probe` with a fake that never resets, which is why none caught it;
  the new tests drive the real `identify()` through a fake programmer.
- RT1's fix refused an image unless a segment covered the RAM vector table. An image with its
  vector table in RAM and a second segment linked for flash passed, and GDB would have
  programmed that segment.
- `STLINK_MCP_CONSOLE` was honoured by one of the two tools that use a console.

Others were new. `read_fault_status` decoded zero as "no fault" on Cortex-M0-class cores,
which have no fault registers, and on a partial read, and its next step located the exception
frame on MSP for faults whose frame is on PSP. `flash_firmware` checked an address that the
programmer uses only for raw binaries. And tool calls, which the MCP SDK runs in parallel
threads, raced: with the port check slowed to widen the window, two probes were given one GDB
port.

None of the fixes has been run on a board. They change what results claim, add refusals before
anything touches the hardware, and run tool calls one at a time. The one change to what is
sent to a probe is that the check of an unmeasured access port always connects with HOTPLUG,
where it could reuse HWRSTPULSE.

