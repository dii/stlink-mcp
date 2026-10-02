# Version 1.0 plan

Decided 2026-10-02. [ROADMAP.md](../ROADMAP.md) holds the scope; this file is how it gets built, and is updated as each step lands. Measurements go to [findings.md](findings.md) as usual.

## What V1 adds, and why

An agent can already load, run, flash and reset a board, read its output, and find where it crashed. Where it is still blind is the program that has gone wrong without crashing. Four capabilities close that, each answering a question an agent asks and cannot cheaply answer with the tools it has:

1. **Names instead of addresses.** `read_memory` also takes a variable's name, with the ELF, or a peripheral register as `PERIPHERAL.REGISTER`, with ST's SVD file for the part, and returns the value decoded: a variable by its type, a register by its fields. An agent thinks in `g_state` and `RCC->CR`. On 2026-10-01 an RCC register address typed by hand was off by 0x400 and cost a measurement, and finding why the H7's USART3 had no clock meant decoding RCC fields by hand.
2. **The call stack.** `core_registers`, given the ELF, returns the call stack with each frame's function, line and local variables, not only the PC. "Stuck in `HAL_Delay`" does not say who called it.
3. **Stop at a condition.** A new tool, `run_until`, runs until a function, a source line or an address is reached, or until an address is written, within a time limit. It answers "does execution get here?" without a console or a rebuild, and "what writes to this?", which prints are poor at.
4. **Resume.** A new tool, `resume`, unless the measurement in M5 shows `release_probe` can carry it.

Not in V1: single-stepping (a later `run_until` to "the next line" covers it if real use asks), memory writes, RTT, and breakpoint lists or conditions. Fourteen tools in all.

## The rules each one keeps

- `verified` only on evidence from the chip. For a stop: the core halted, the Debug Fault Status Register (DFSR, 0xE000ED30) naming the cause, BKPT for a breakpoint and DWTTRAP for a watchpoint, and the PC where it was asked to stop. For a name: the address and type taken from the ELF or the SVD, never guessed, and a name that matches more than one symbol refused with the candidates.
- A time limit that runs out is `unverified`, and says the target is still running.
- Every result that leaves the core halted says so, and that the rest of the chip keeps running: timers, DMA, USB and watchdogs do not stop with the core.
- Nothing resets unless asked, and a reset is reported, as everywhere else.
- The hardware comparators are counted from the part itself, and a request that needs more than it has is refused, never quietly turned into a software breakpoint in flash.
- ELF and SVD data are read at run time from the user's own files. ST's SVD files are Apache-2.0, but nothing parsed from them is committed, by the same rule as the device database.
- Every hardware path is measured on the H7 and the F407, and on the N6 where it differs, before a result claims it. Parser inputs are captured output.

## Measurements first

Each answers a design question, and none needs hardware that is not already attached.

- **M1. Attach without reset.** `run_firmware` starts ST-LINK_gdbserver with `--halt`, which resets. Can a server started without it attach GDB to a running program without resetting it, does attaching halt the core, and does GDB's detach resume it? Decides how `core_registers` gets a call stack from a running or hung program, and whether `run_until` can start from the program as it is.
- **M2. Waiting with a time limit.** A GDB batch blocks on `continue` until the target stops. Compare an interrupt sent to a batch GDB with a GDB/MI process kept open beside the held server (`-exec-continue`, `-exec-interrupt`, and the `*stopped` record with its reason). Decides `run_until`'s control path.
- **M3. Comparators.** Read FP_CTRL (0xE0002000) and DWT_CTRL (0xE0001000) on each part: how many breakpoints and watchpoints there are, and whether GDB's breakpoints in flash use them.
- **M4. Evidence of a stop.** DFSR after a breakpoint and after a watchpoint, and that it is cleared between runs, since it is sticky and an old stop must not verify a new one.
- **M5. A halted core and `release_probe`.** Does shutting the server down resume the core or leave it halted? Decides whether `resume` is a tool or a sentence in `release_probe`.
- **M6. Where a watchpoint stops.** The core is expected to halt after the instruction that made the access, and possibly later. Measure the offset on each part, so the result names the writing instruction, or says it cannot.
- **M7. Reads that change state.** The F407 and H743 SVD files mark no register read as having a side effect; the N657 file marks 54 (counted 2026-10-02). Measure one on the H7: with a byte received on USART3, does a programmer read of RDR clear RXNE? Decides what a register read by name must say (D2).
- **M8. Which SVD.** How a device ID maps to an SVD file name, for the measured families and the seeded ones, and how long parsing takes: the N657 file is 23 MB.
- **M9. Types.** Decode a global's type with pyelftools reading DWARF, and with GDB's `ptype /o` output parsed, on the test images and one project built by STM32CubeIDE. Decides D1.
- **M10. The N6.** Breakpoints, watchpoints and the call stack on the N6's Secure code through AP1, and whether a stop interacts with the port closing.

## Steps

**S1. Names in `read_memory`** (after M7 to M9). `symbol` with `elf_path`, or `register` as `PERIPHERAL.REGISTER` with the SVD found for the part or named by the caller. Still non-halting and one programmer read. Returns the raw words and the decoded value: for variables, base types, enums, pointers, arrays, structs and bitfields, with unions left raw; for registers, each field by name and value, with its enumerated meaning where the SVD gives one. Refuses an unknown or ambiguous name, a variable that does not live in memory (in a register, or optimised out), and a read the SVD marks as changing state; where the SVD marks nothing, the result says so. Tests read the installed SVD files and skip without them; for CI, a short excerpt of one SVD kept with ST's licence header. `tools/hwcheck` reads a global and an RCC register by name on the H7 and the F407 and compares them with the raw read.

**S2. The call stack in `core_registers`** (after M1 and M10). With `elf_path`: GDB attaches as M1 found, takes the backtrace with locals to a bounded depth, and detaches. Frames carry function, file and line, and locals as values, with optimised-out ones marked as such. `core_registers` already halts for its read and says so; this keeps that. `tools/hwcheck` gets an image that hangs three calls deep, and checks the chain.

**S3. `run_until`** (after M2 to M6 and M10). Parameters: `elf_path`; one of `location` (a function, `file:line` or an address) or `watch_address`; `seconds`; and `from_reset` (D3). `verified` on the DFSR cause and the PC, and for a watchpoint, the comparator that matched. The result says where and why it stopped, gives the call stack and locals, and states that the core is held. `unverified` when the time runs out. `failed` when it stopped elsewhere (a fault, pointing at `locate_fault`), when the comparators are used up, when the location is not in the ELF, or when the location is in code that is not loaded. `tools/hwcheck`: reach a function after a few ticks; watch the counter and stop at its store; a location never reached; an image that faults first.

**S4. `resume`** (after M5). Resume and detach, `verified` on evidence the core runs, which M5 settles: the core's halt flag clear, or the counter advancing.

**S5. The 1.0 gates.** The README moves from "deliberately small" to what V1 is, with the tool table and the comparison with related projects updated; the findings ledger gets a row for each new promise, with its measurements; `tools/hwcheck` covers each. Then the gates the README already names: the fourteen tool signatures and result fields declared stable (beta), one report from a user outside the project on a board the README does not list, the Windows question decided, and the owner's read of the README before the repository goes public.

S1 can start as soon as M7 to M9 are done, and lands first. S2 and S3 share M1, M2 and the GDB control path, so they are built together. S4 follows M5.

## Decisions for the owner

- **D1. pyelftools as a third dependency**, pure Python and BSD-licensed, for reading types from DWARF. Recommended: parsing GDB's human-readable `ptype` output is the same fragility this project already pays for with the programmer, and here a structured format exists. The alternative is GDB's text and no new dependency.
- **D2. Register reads the SVD does not mark.** Recommended: read, and say the file does not mark which reads change state. The alternative, refusing data registers by a name rule (DR, RDR, RXDR), rests on no file.
- **D3. Where `run_until` starts.** Recommended: from the program as it runs now by default, and from a reset when asked with `from_reset`, reported as a reset.
