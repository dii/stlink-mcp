# Findings

The README states conclusions. This file holds the measurements behind them and the things this project got wrong on the way to them, because a claim without its provenance is the failure this project exists to remove. Dates are when the measurement was made. Boards: NUCLEO-N657X0-Q, NUCLEO-H743ZI, STM32H753I-EVAL2, all with on-board ST-LINK V3, driven by STM32CubeCLT 1.22.0 (STM32CubeProgrammer 2.23.0) on Fedora 44.

## The result contract came out of a defect in this tool (2026-08-15)

An early version returned `ok: true` alongside `verified_by: "nothing"`. A review pass found the rule "a tool reports success only after checking" broken in four tools in four different ways. The fix was to stop letting tools set `ok` at all: three constructors, `verified`, `unverified` and `failed`, one of which refuses to build without the evidence that justifies it. The test pinning that rule was itself mutation-checked and failed on the first try, because a non-empty string such as `"nothing"` passes a truthiness check.

## STM32N6: the debug access port closes when a typical image runs (2026-08-14 to 08-16)

**Observation (08-14).** Running an FSBL built from ST's template closes debug access port 1 the moment the image starts. A reset does not reopen it; only removing power does. This was first recorded as a family quirk of the part, which was wrong: it is a property of the image.

**Cause (08-16).** One line at the end of ST's `system_stm32n6xx_fsbl.c`:

```c
RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;   /* deactivate SYSCFG clock */
```

`SystemInit` turns the SYSCFG clock on early so it can write `INITSVTORCR`, then turns it off again on the way out. On the N6 that clock is what keeps access port 1 alive. Remove that single write and the port survives: four consecutive load-and-run cycles with no power cycle, against the stock image closing it on the very next run of the same board.

**Method.** Bisecting `SystemInit`. Eliminated on the way: the boot ROM handoff (a bare image with no startup chain leaves the port open), the startup assembly, TrustZone and the SAU, `INITSVTORCR`, the PWR supply block, and every peripheral reset in there.

**Two wrong confirmations.** The closure was "confirmed" twice by `pgrep ST-LINK_gdbserver`, which matches nothing on Linux, ever: the kernel truncates process names to fifteen characters, so the check reads exactly like a clean control while a process is holding the probe. Confirm a probe is free by port, with `ss -ltnp`, never by process name. Connect-under-reset (`mode=UR`) is not a way out either; it fails on a healthy board too.

**Consequence.** `run_firmware` holds its GDB server across runs, so with a stock image the cost is one power cycle per session rather than one per run.

## Reset before every load

An established GDB connection keeps working across repeated load-and-run cycles, provided the target is reset before every load. The server's own `--halt` resets once, when the server starts. Without an explicit reset per load, each image inherits the previous run's live peripherals and interrupt state and faults during start-up in a way that reads as a firmware bug. Found by running many consecutive loads through one session; the GDB batch now issues `monitor reset` before each load.

## The reset has to be the connect (2026-08-16)

NUCLEO-H743ZI, reset handler at `0x0807FA98`:

| sequence | PC afterwards |
| --- | --- |
| hotplug connect, then `-rst -halt` | `0x0807FC2C` |
| hotplug connect, then `-halt -rst` | `0x0807FC3A` |
| `mode=HWRSTPULSE` connect, then `-halt` | `0x0807FA98` |
| `mode=UR` connect | `0x0807FA98` |

Both non-resetting orders reset the part correctly and then lose the race to halt it, landing hundreds of bytes into start-up where the PC cannot be compared against anything. Only a resetting connect lands on the vector, which is what makes `reset_target`'s check (PC against word 1 of the vector table) possible. Verified on two H7 boards with different reset handlers. On the N6 the core halts in its boot ROM at an address the table cannot predict, and the tool says so rather than inventing a comparison.

## A halt does not survive the invocation that made it (2026-08-17)

`reset_target` had a `leave_halted` option for one day. Sampling the PC across separate reads after a reset-and-halt on a NUCLEO-H743ZI gave `0x0807FA98`, then `0x0807FE96`, `0x08060AFC`, `0x08080482`; the STM32H753I-EVAL2 behaved the same. The option reported `core_halted: true` while the core was demonstrably running, so it was removed. Nothing in this project claims whether the core is halted now: a static program counter is what a halted core looks like and also what a tight loop looks like, and on an N6 sitting in its boot ROM the PC read identically three times while proving nothing either way. To hold a target stopped, use the GDB server that `run_firmware` keeps alive.

## Identity reads correctly on a port that cannot halt the core (2026-08-14)

On the N6, access port 0 reports device ID, name, CPU and revision perfectly well while being unable to halt the core. `identify()` originally stopped at the first port that answered. Now the family's recorded port wins, verified before adopting, and any fallback is reported as a degradation. A wrong access port and an absent target produce the same error from the vendor tools, which is why `board_state` tries every port.

## The vendor database reports the N6 vector table, not its RAM base

ST's device database gives the N6's SRAM at `0x34180400`. That is the vector table address, not the RAM load base of `0x34180000`, because the boot ROM prepends a signing header. Nothing in the database says which of the two it is reporting, which is why a measured entry always beats a seeded one. Four device IDs (`0x42B`, `0x42C`, `0x472`, `0x49A`) name several separate SRAM banks in their databases and are refused rather than guessed.

## A field named for the silicon held a fact about a startup file

The N6 port closure was first stored as `debug_closes_on_run`, a family flag. It described what ST's startup file does, in a field named for what the part does, and it took two days and the bisection above to get back out. The rule since: the quirks table holds what is true of everything behind a device ID, and anything that varies by board or by image stays out of it.

## Enumeration works while a server holds a probe

`list_probes` used to refuse whenever any GDB server was held, on an untested assumption. Measured: `STM32_Programmer_CLI -l` listed all five attached probes in 1.5 seconds with one held, the same as with none. Held probes are marked, not hidden. What fails is connecting to a held probe, not listing it.

## The first ttyACM is the wrong board about half the time

`capture_console` once computed the ambiguity error and then fell through to the first matching device path anyway. With two boards attached that is the wrong board about half the time; with five it is a one-in-five pick. Silence from the wrong board reads as "firmware not running", and output from the wrong board reads as "it works". The port is now resolved from the probe serial via the programmer's own listing, and the tool refuses when it cannot.

## The packaging tool publishes what git hides (2026-08-16)

Hatchling builds an sdist from the filesystem, honouring the repo's `.gitignore` but not the user's global ignore file. A local review report carrying an absolute path from the author's machine was in the built sdist while correctly absent from every commit, and `git status` said nothing, because from git's point of view nothing was wrong. The sdist now has an explicit include list rather than an exclude rule, so the next locally-ignored working file cannot be published the same silent way.

## Newest install, not alphabetically last

`find_toolchain` promised "newest first when several are installed" and delivered the oldest: a plain string sort puts `stm32cubeclt_1.9.0` ahead of `stm32cubeclt_1.22.0`. Fixed with a version sort before anything else was built on it, because from the vendor-database seeding onward it would have meant reading a stale database with no signal that a newer one was skipped.

## The MemManage bit map was wrong, and 52 tests agreed with it (2026-09-03)

`_MMFSR_BITS` placed MUNSTKERR at CFSR bit 5 and had nothing at bit 3. Arm's CMSIS headers put MUNSTKERR at bit 3 and MLSPERR (MemManage during lazy floating-point state preservation) at bit 5. A real unstacking fault therefore decoded to no faults at all, `any_fault` false, "no fault is active": the confidently wrong answer this project exists to prevent. The suite did not catch it because its tests were written from the table, one of them asserting `1 << 5` decodes to MUNSTKERR. Found by a contract author working from Arm's header with the implementation withheld (the blind-tests protocol, first run on this repo), before any test was written. The same run found the diagnosis attached the fault address to three specific bits rather than to the VALID flag, so a stacking fault with a valid MMFAR got no address in its text while IACCVIOL, which never records one, did. Both fixed the same day; the address is now reported for the register whenever its VALID flag is set.

## The run path could write flash (red-team RT1, 2026-09-03)

`run_firmware` handed GDB an ELF and said `load`, and GDB writes each loadable segment to the address the image carries. With a default flash-linked build (0x08000000, what STM32CubeIDE and CMake produce), ST-LINK_gdbserver programs flash (that is what its `-cp` path to CubeProgrammer is for; UM2576 says so), and the tool then reads a stack pointer and program counter from the family's RAM vector table, which holds unrelated bytes, and starts the core there. It reported `unverified`, or `verified` on a stale RAM image's output. The project's own written policy treated the run path as reversible, which it was only for an image linked for RAM, and nothing said the image had to be. Found by a red-team reviewer reading the GDB script and the gdbserver's own help text; confirmed by an independent skeptic against GDB's documentation and the gdbserver's binary. Fix: the image's loadable segments are read before any server is started, and an image that does not cover the family's vector table address is refused by name (`image.py`). A newcomer persona reading the README had asked exactly this question, unprompted: whether a normal Debug build works.

## The field was not searched before building (2026-09-02)

This project was built on 2026-08-14 and went through four review passes without anyone asking whether it already existed. The search happened on 2026-09-02, while checking a proposed rename, and found shieldyguy/stm32-mcp (six months older), Helistana's CubeIDE server, and, a day later, Adancurusul/embedded-debugger-mcp, the closest neighbour on this project's own axis. A separate project would likely still have been the outcome, because the difference is the result contract rather than a feature, but that should have been a decision. The lesson went into the review checklist: search before the roadmap, and name the neighbours in the README.

## What the neighbours report (read 2026-09-04)

The README's comparison with embedded-debugger-mcp rests on its source at commit 7113ae44 (2026-07-16), files `src/tools/debugger_tools/target_control.rs` and `src/tools/debugger_tools/flash.rs`:

- `run` returns "Target resumed execution successfully! ... Status: Running" once `backend.run()` returns Ok. No read of the core follows.
- `reset` calls `backend.snapshot().await.ok()` and maps a failed snapshot to `(0, 0)`, so the message "Target reset completed successfully." can carry `PC: 0x00000000` when the read failed. That is the same shape as the "Start operation achieved successfully" that opened this project.
- `run_firmware` emits "Firmware is now running on target." after `core.reset()` and `core.run()` return Ok. The RTT attach in its step 4 is behind `args.attach_rtt`, and the closing line is emitted either way.
- `flash_program` takes `verify` (or the config's `verify_after_program`), and a separate `flash_verify` compares raw bytes against target flash. This project reads nothing back after `flash_firmware`, and its result says so.

The comparison is fair only while that is what their code does. Re-read before repeating it.

## Fixes that stopped at one tool (review, 2026-09-28)

A review before sharing read the code and reproduced its findings against a faked programmer, with no board attached. Several were a defect already fixed once, surviving in a sibling:

- `reset_target` stopped claiming the core was halted on 2026-08-17, and `core_registers` kept returning `"halted": true`.
- `reset_target` relies on a resetting connect pulsing nRST even when the port then does not answer. `identify()` set `target_was_reset` only when that connect succeeded, so `board_state` could restart an STM32N6 whose AP1 was closed and report it untouched. The tool-level tests replace `Probe` with a fake that never resets, which is why none caught it; the new tests drive the real `identify()` through a fake programmer.
- RT1's fix refused an image unless a segment covered the RAM vector table. An image with its vector table in RAM and a second segment linked for flash passed, and GDB would have programmed that segment.
- `STLINK_MCP_CONSOLE` was honoured by one of the two tools that use a console.

Others were new. `read_fault_status` decoded zero as "no fault" on Cortex-M0-class cores, which have no fault registers, and on a partial read, and its next step located the exception frame on MSP for faults whose frame is on PSP. `flash_firmware` checked an address that the programmer uses only for raw binaries. And tool calls, which the MCP SDK runs in parallel threads, raced: with the port check slowed to widen the window, two probes were given one GDB port.

None of the fixes has been run on a board. They change what results claim, add refusals before anything touches the hardware, and run tool calls one at a time. The one change to what is sent to a probe is that the check of an unmeasured access port always connects with HOTPLUG, where it could reuse HWRSTPULSE.

## First hardware run of the 2026-09-28 changes (2026-10-01)

Run on the NUCLEO-H743ZI through its on-board STLINK-V3E (firmware V3J7M2) and through an ST-LINK/V2 (V2J38S7) wired to its CN5 connector with an Olimex ARM-JTAG-20-10 adapter, and on a NUCLEO-N657X0-Q (STLINK-V3, V3J15M6), with STM32CubeCLT 1.22.0 (STM32CubeProgrammer 2.23.0). Every call went to the server over stdio, as a client makes it. The images were written for the run: an H7 RAM image that prints numbered lines on USART3 and counts in AXI SRAM, a variant that executes an undefined instruction after four lines, the same counter for the N6 with no console, and a flash-linked `.bin`.

What held:

- `list_probes` and `--check` read the first ST-LINK/V2 listing captured for this project: no board name, no console, firmware and serial parsed.
- `board_state` identified all three probes with a hotplug connect and no reset. With three attached and no serial given, it refused and named them.
- `run_firmware` verified the H7 image by its console through both probes. Through the V2, which has no serial port, the console came from `STLINK_MCP_CONSOLE` as a `SERIAL=path` pair, and the result named that source. A bare path with three probes attached was applied to none, and every result said why. The N6 image, which prints nothing, was `unverified`, and its counter advanced between two reads.
- `core_registers` reported `halted_to_read_registers` and made no claim about the core afterwards.
- `read_fault_status` decoded an undefined instruction escalated to a hard fault on both parts, CFSR 0x00010000 and HFSR 0x40000000. Following its next step, LR read 0xFFFFFFF9 and word 6 of the frame at MSP was the address of the `udf`, on the H7 and on the N6. The N6 result carried the TrustZone note. A first read on the N6 said no fault was active; it had simply come before the image reached the fault, and a later read decoded it.
- The refusals came back before anything was sent to a probe: an ELF to `flash_firmware`, a misaligned address, a hex address without its prefix, a 2048-byte read, a zero-second capture. A flash-linked ELF to `run_firmware` costs one hotplug connect first, to learn the family's vector table address.
- `flash_firmware` ran on hardware for the first time: a 371-byte `.bin` to the H7 at its measured flash base. Its result said the write was accepted and not read back. A readback of all 93 words with `read_memory` matched the file, and `reset_target` then halted at the new image's reset handler. The board's own flash was saved first and written back afterwards.
- A hotplug JTAG connect through the V2 reported `JTAG freq : 9000 KHz`, device 0x450, with no reset. This is the observation the planned `interface` parameter will check against.
- Where an image stalled before printing (the first H7 image, whose USART3 did not start on two runs and did on a third, after the board's own firmware had run; the cause is in the image and was not found), `run_firmware` returned `unverified` and the counter read showed it stuck.

What did not hold:

- **`run_firmware` can be verified by the previous image's output.** The STLINK-V3E keeps what the target prints while no program has its serial port open, a few KB of it, and delivers it the next time the port is opened. (This entry first said it keeps the oldest bytes; it keeps both ends, see the next entry.) An H7 image that prints nothing, loaded after one that printed, came back `verified: console output from this board's own port` on nine lines of the previous image; its own counter showed it running. `capture_console` returns the same backlog as if it arrived during the window: a 1.5-second capture returned about 3.5 seconds of output, and a capture after flashing returned 6,793 bytes of the old image before the new image's third line, with its first two lines lost. None of this shows on the N6 runs, whose images print nothing.
- The fault next step finds the frame at the stack pointer the handler reports, which holds only while the handler has pushed nothing. With the H7 flash firmware's own HardFault handler running (VTOR still at flash, which `run_firmware` does not set), the frame was not at MSP.

Observed and not explained: calls that list probes while a GDB server held one took 11 to 14 seconds, against 1.3 to 1.5 seconds otherwise, and a few calls just after a release did too. Calls on the N6 took about 4.6 seconds against 1.45 on the H7. `board_state` reports the V2's board as `--`, the connect banner's text, where `list_probes` reports none.

## The console backlog, measured and set apart (2026-10-01)

How the STLINK-V3E (V3J7M2) fills its buffer, measured with an H7 image printing about 3 KB a second so that it overflows within seconds of the port closing: after 5 seconds closed, the open delivered 8,188 bytes holding ticks 58 to 236 and 665 to 720, and live output went on from 721. So it keeps about the first 6 KB printed after the port closed and the last 2 KB before it opened, and drops everything between. The backlog arrives at once: 320 bytes came 0.1 ms after an open, and the target's next line 650 ms later. Clearing the host's input buffer at open does nothing about it, since it has not arrived yet when that runs.

`ConsoleCapture` now sets aside whatever arrives until the port has been quiet for 100 ms, at most one second, and `run_firmware` and `capture_console` report it apart as `console_buffered`, with its size and a note, never as evidence. On the H7: the silent image loaded after a printing one came back `unverified`, with the previous image's ticks 3 to 10 (255 bytes) set aside; the printing image verified on its own ticks 0 to 2, with 7,981 bytes set aside; a capture split ticks 3 to 8 (set aside) from 11 and 12 (output). Nothing from the previous image arrived after the drain on the run path, so the GDB server had already stopped it by then. In one earlier run the silent image reported no backlog at all where about four seconds of output was expected; the next run did not repeat it, and it is not explained. It cannot verify a run either way, since nothing that arrives before the drain ends is counted.

## A reset can leave USART3 on the H7 without its clock (2026-10-01)

The NUCLEO-H743ZI's own firmware clocks USART3 from PLL3. After a reset taken while it was running, a RAM image that set up USART3 from the reset defaults (kernel clock from the APB1 bus clock) wrote every register as intended and got no transmitter acknowledge: ISR stayed 0 and the first byte never left. Selecting the internal oscillator as the kernel clock, and forcing it on for peripherals, changed nothing. Turning PLL3 on made ISR read 0x006000D0 at once. The likely reading is that the kernel clock selector, which switches only on edges of both clocks, was left on PLL3 when the reset stopped it; that part is inference. A run after the image's own earlier runs was unaffected, which is why it looked intermittent. `run_firmware` reported every one of these runs `unverified`, which was correct. A RAM image that follows firmware like this should turn PLL3 on before using a peripheral that firmware clocked from it.

## Flash is read back before it is verified (2026-10-01)

`flash_firmware` verified on the programmer's own "File download complete", the kind of banner this project exists not to trust, on the one tool whose write persists. It now reads the written range back with the programmer's upload (`-u`) into a temporary file and compares it with the image byte for byte. `-v` would have done a comparison inside the programmer, but its result is another banner to parse; the bytes themselves are the evidence. On the H7: a 395-byte `.bin` at 0x08000000, "read back 395 bytes ... identical", the whole call 2.7 seconds against 2.4 without the readback, and `reset_target` then halted at the image's reset handler. A mismatch and a failed readback are tested against a fake programmer only; no board was made to produce one. The N6 path, through its external loader, has not been run. The refusal to write to an unmeasured base stays, because a readback confirms what was written and cannot tell a wrong address.

## JTAG, measured and then made a parameter (2026-10-01)

By hand first, with STM32_Programmer_CLI and the GDB server, then through the tool. The ST-LINK/V2 (V2J38S7) through an Olimex ARM-JTAG-20-10 into the NUCLEO-H743ZI's CN5: a hotplug connect, a connect under reset and a reset-pulse connect followed by a halt all worked at `JTAG freq : 9000 KHz`, the last halting at the reset handler, 0x0807FA98. The GDB server started without `-d`, which is how it selects JTAG (it has no flag for it), loaded the RAM image, and the console verified it. With `-d` the server's option list includes `SWD Debug : Enabled`; without it the line is absent. The NUCLEO-N657X0-Q's own probe (V3J15M6) does JTAG too, at 21333 kHz by hand and 8000 kHz through the tool, and ran the N6 RAM image over it, its counter advancing. The NUCLEO-H743ZI's own STLINK-V3E (V3J7M2) does not: `Error: Unable to get core ID` and `No STM32 target found`, the words a missing target produces, which is why the hint for a failed JTAG connect now names the probe first. Through the tool that failure took 72 seconds, since identification tries two access ports and a resetting connect and the programmer retries each, and a resetting connect was issued along the way. A run over SWD on the V2 while its GDB server was held over JTAG was refused rather than reusing the server.

## The fault location in one call (2026-10-01)

`read_fault_status` says what happened and its next step says how to find where: read LR, choose MSP or PSP from EXC_RETURN, read 32 bytes there, take word 6. That was followed by hand on both boards earlier the same day and was right on both, and it costs an agent three calls and some arithmetic, so `locate_fault` does it. It also handles the floating-point frame (EXC_RETURN bit 4 clear, 104 bytes, the PC still word 6) and the Armv8-M additional state (bit 5 clear, the frame 40 bytes up), and checks the frame before trusting it: exception entry always stacks an XPSR with the Thumb bit set and a halfword-aligned PC. On the boards, against images that fault with `udf`: the H7 with its own handler on MSP, a thread on PSP, and an extended FP frame, and the N6 on its Secure MSP, each returned the `udf`'s address, and with the ELF `reset_handler` and the source line. The H7 image without its own vector table, so that the board's flash firmware's handler ran and pushed registers first, came back `unverified`, naming the missing Thumb bit and a PC outside the image. The extra-state offset and the imprecise bus error note are tested against a fake only; no board produced either.

## Calls on different probes, and what listing costs (2026-10-01)

Every slow call seen on 2026-10-01, 11 to 14 seconds, was the probe listing (`STM32_Programmer_CLI -l`) that every tool ran first: it took 11.3 seconds while a GDB server held one probe, against 1.36 seconds otherwise. (On 2026-08-15, with five probes and a server held, it took 1.5 seconds; what differs between the two days is not established.) Run beside programmer commands on other probes it also broke some of them: 2 of 148 reads on the H7 failed while listings ran, one with the identity read back as nonsense (boot loader 0x16, 2.875 MB of flash), and 1 of 8 on the N6. Without listings, 80 H7 reads and 9 N6 reads at the same moment all came back. A connect costs 0.12 seconds on the H7 and 1.1 seconds on the N6, which made three connects per N6 call: AP0, which answers identity, then AP1, then the operation.

So a call that names its probe no longer lists the probes. One that needs the console finds the port by the probe's serial in `/dev/serial/by-id`, where udev names each port after its USB serial number, which for these ST-LINKs is the serial the programmer reports. Calls on different probes run together, calls on one probe queue, and a call that lists runs alone. Each probe's last settled access port is tried first, which takes the N6 to two connects. Through the server: an H7 read 0.26 seconds against 1.57, an N6 read 2.3 against 4.8, an N6 read while the H7 was held 3.4 against 14.7, and an H7 capture ran beside two N6 calls, which queued behind each other.

Skipping the listing exposed something it had been hiding. The programmer ignores an `sn=` it cannot parse as hex and connects to the first probe it finds: `sn=NONE` and `sn=X` answered from the ST-LINK/V2, while `sn=123` got "Serial number not found". The project's own stdio test, which passes `NONE`, came back verified from the wrong board before this was caught. A serial that is not hex digits is now refused, and every programmer answer for a named probe is checked against the serial in its own banner.

## What each README promise rests on (2026-10-01)

1.0.0 is cut when every promise in the README has a measurement behind it here. This is the ledger, from the README as it stood on 2026-10-01. Its hardware column on the H7 and the N6 is re-run by `tools/hwcheck/hwcheck.py`, which passed 33 of 33 checks on 2026-10-01 with `--jtag` and `--flash`. "Fake" means a test against a faked programmer or port and nothing on a board. "Not measured" means neither.

| Promise | Measured on hardware | Fake only, or not measured |
| --- | --- | --- |
| No tool reports success it did not observe; `ok` is never set by hand | Every tool's verified path, on the H7 and N6, 2026-10-01 | The no-literal-`ok` rule is a test |
| ST's tools overstate (the three examples) | N6, 2026-08-14 to 08-16 | |
| `board_state` scans ports 0 and 1, and retries with a resetting connect | Three probes by hotplug, 2026-10-01; the reset retry on the N6 with AP1 closed, 2026-08-15 | `target_was_reset` after a resetting connect that then fails: fake |
| `board_state` is `unverified` while a server holds the probe | H7, 2026-10-01 | |
| `run_firmware` refuses an image not linked for RAM, or with a segment outside SRAM | H7, a flash-linked ELF and one with a flash segment beside a RAM vector table, 2026-10-01 | |
| `run_firmware` is verified by the new image's console, and output from before the run never counts | H7, 2026-10-01; the N6's silent image `unverified` | |
| `run_firmware` is `failed` when the GDB batch does not reach its end markers | H7, an image with a segment at 0x2C000000, where nothing answers, 2026-10-01; GDB's own output in the result | |
| `flash_firmware` reads back and compares, and takes only a `.bin` | H7 at 0x08000000, 2026-10-01 | A mismatch, and a failed readback: fake. The N6 path through its external loader: not measured |
| `read_memory` connects hotplug-only and refuses rather than reset at a closed port | Hotplug reads throughout, 2026-10-01 | The refusal at a closed port: fake; the closing itself measured 2026-08-14 |
| A short read is `failed`, naming what is missing | | Fake. On the H7 a read across the end of RAM failed whole instead (2026-10-01), so no board has produced a short read |
| `core_registers` halts for the read and claims nothing after | H7, 2026-08-17 and 2026-10-01; N6, 2026-10-01 | |
| `read_fault_status` decodes the registers | An undefined instruction on the H7 and the N6, 2026-10-01 | Refusing Cortex-M0, M0+, M1 and M23: fake, no such board here. A partial read: fake |
| `locate_fault` finds the faulting instruction, and says when the frame does not look like one | H7 on MSP, on PSP and with an FP frame; N6 on its Secure MSP; a handler that pushed first; thread mode refused; 2026-10-01. An overwritten LR refused, and an imprecise bus error noted, 2026-10-01 | The Armv8-M extra state: fake |
| `capture_console` reads the port mapped to the probe | Probe list and udev names, 2026-10-01; a setting pair through the V2 | |
| `reset_target` halts at the reset handler and compares it with the vector table | H7 through the V3E, 2026-08-16 and 2026-10-01; through the V2 over JTAG, 2026-10-01 | The N6 was not reset today |
| `release_probe` is `unverified` when nothing was held | 2026-10-01 | |
| `list_probes` lists each probe once and works while others are held | 2026-08-15; 2026-10-01, at 11 s while held | |
| The server is held because the N6's stock start-up closes AP1 | 2026-08-14 to 08-16 | |
| The target is reset before every load | 2026-08 (the reset-before-every-load entry) | |
| A held server blocks only its own probe | N6 calls while the H7 was held, 2026-10-01 | |
| The tool does not set VTOR | A fault went to the flash firmware's handler, 2026-10-01 | |
| A serial is required with several boards; consoles map by serial; each board gets its own GDB port | 2026-10-01: refused with three attached, two servers on 61234 and 61238 | |
| Calls on different probes run together; on one probe they queue | 2026-10-01 | |
| SWD or JTAG, and which probes do JTAG | 2026-10-01 | |
| Every setting does what the table says, and a bad one stops start-up | All six, 2026-10-01, including 57600 baud and a GDB port base of 62000 | |
| Provenance changes behaviour; an unmeasured flash base is refused | Through a board file for the H7: user-stated values, a run with the note, flash refused without an address, 2026-10-01 | A `vendor-db` part: not measured, no seeded board here |
| A seeded part can run firmware; an unrecognised one can be identified and read | | Not measured: needs a board that is neither an H7 nor an N6 |
| A board file is user-stated only and wins over the table | H7, 2026-10-01 | |
| 97 seeded device IDs, 4 refused | The installed databases, 2026-10-01 | |
| Tested on the listed boards and probes | Each, on the dates above | macOS and an ST-LINK/V2-1: not measured. Windows: not supported |

So the hardware gaps before 1.0.0 are a board outside the two measured families (an STM32G0, C0, L0 or F0 would also settle the Cortex-M0 refusal) and the N6 flash path. A GDB load that fails partway was added on 2026-10-01. The fake-only rows need a way to make a board produce the case, and some of them may never get one.

## Four ledger rows moved onto the H7 (2026-10-01)

Images made to produce each case. An image with its vector table in AXI SRAM and a second segment at 0x2C000000, which is inside the range the tool accepts as RAM and holds nothing on an H7, passed the image check, and GDB's load stopped at that section: `run_firmware` was `failed`, its `gdb_output` naming `.flashblob` at 0x2c000000 and the error. A store to the same address from a running image raised an imprecise bus error: `read_fault_status` decoded CFSR 0x00000400 (IMPRECISERR) and HFSR 0x40000000, and `locate_fault` returned a stacked PC of 0x24000166, 12 bytes past the store at 0x2400015A, with its note that the PC is late. A HardFault handler that calls a function before looping left LR at 0x2400018F, and `locate_fault` refused, naming it. A 32-byte read from 0x2407FFF0, across the end of the 512 KB of AXI SRAM, did not come back short: the programmer failed the whole read, so the short-read refusal still has only a fake behind it.

## A JTAG failure cannot be cut short by the missing clock line (2026-10-01, rejected)

A JTAG connect through a probe that does not do JTAG, the NUCLEO-H743ZI's own STLINK-V3E, took 72 seconds to fail through the tool: two access ports, then a resetting connect, each retried by the programmer. Its output has no `JTAG freq` line, while every successful connect has a clock line, so stopping after the first attempt when no clock line appears looked safe. It is not. The captured connect to an STM32N6 whose running image had closed the debug port (the `NO_CORE` sample in tests/test_parsing.py, over SWD) has no clock line either, and that is the case the resetting connect exists to recover. The two failures print the same thing, so the tool keeps trying, and the README says a JTAG failure on a probe without it takes over a minute.

## Who asked: the first step on N9 (2026-10-01)

N9 asks that every operation be traceable to who asked for it, even while the answer is always the local user. The MCP SDK hands a tool a `Context` when it declares one, which was tested with a toy server over stdio before anything was built on it: a synchronous tool behind a `functools.wraps` decorator receives it in its worker thread, the parameter stays out of the tool's schema, and an optional annotation (`Context | None = None`) works too. The client's declared name and version are on `ctx.session.client_params`; stdio has no session id. The first version numbered connections by `ctx.session`, and on the board one client's three calls came out as connections 1, 2 and 3: the SDK builds a new session object for every request. A second toy showed the initialize parameters and the SDK's private connection object staying the same across one connection's requests, so the public one, the parameters, is the key now, and one client's calls read as connection 1 throughout. Each call is traced on stderr, and each held GDB server records the caller that acquired it.

## One programmer call per read (2026-10-01)

A read was two programmer calls, a connect to identify the target and then a connect with the read, and on the N6 every connect costs about 1.1 seconds. STM32_Programmer_CLI prints the identity banner and the data in one call, so `read_memory` and `read_fault_status` now make one when the probe's remembered port is its family's measured port, the case where identification would scan nothing else, settle nowhere else and warn of nothing; anything else takes the two-call path as before. Only reads take it, because a read discarded after the banner shows something unexpected has changed nothing. On the boards through the server, after the first call: an N6 read 1.15 seconds against 2.3, the N6 fault status the same, an H7 read 0.13 against 0.25. `tools/hwcheck` passed 30 of 30 afterwards.

## A third family on a custom board: an STM32F407 (2026-10-01)

A custom board built around an STM32F407IGT6 (device 0x413, Cortex-M4, 1 MB flash), not a NUCLEO, through a second ST-LINK/V2 wired to its JTAG header. The probe arrived on firmware V2J16S4. `-l` listed it, a connect naming its serial said "Serial number not found", and one by index said "Old ST-LINK firmware version": STM32CubeProgrammer 2.23.0 will not use it. STLinkUpgrade's command line could not do the upgrade, because every action expects a probe already in DFU mode and none of its options sends the switch, so it was done in its window; both V2s are on V2J48S7 now, and the connect by serial works. The tool's hint for "Serial number not found" had said no such probe was attached, and now names old firmware too.

The option bytes read Level 0, no write protection. The flash was saved before anything else. Then, through the tool, the path every part outside the two measured families takes, which had never run on a board: `board_state` reported the family from ST's database with its addresses `vendor-db` and access port 0 `assumed`, and the warning that the port is a guess rode on every tool that used it. A RAM image loaded at the database's 0x20000000 and ran, its counter advancing between reads; with no serial adapter on the board's UART, the run was `unverified`, which is right. Its result carried a note saying "The run itself IS verified, by this board's own console output", written before the verdict was known on the assumption of a verified run, and no seeded run had reached it before. The note now claims nothing about the run. A fault image's undefined instruction decoded as UNDEFINSTR escalated to HardFault, and `locate_fault` returned its address and source line. `core_registers` halted the core on AP0, `reset_target` halted at the resident firmware's reset handler, and a JTAG connect through the V2 ran at 9000 kHz. AP0 is therefore the working port, and 0x20000000 a working RAM vector table address, on this part; the flash path has not been run.
