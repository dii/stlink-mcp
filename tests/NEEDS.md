# What the tests are for: the needs, by ID

Every test should check one of these needs and name it, by ID, in its name or docstring. A need is something the agent or the user relies on, stated in what they observe, not in how the code does it. A test that only checks how the code does it (an argument list, a message's exact wording that no need requires, a value computed the same way the code computes it) is not testing a need, and changing the code without changing what anyone observes should not break it.

The cross-cutting needs N1 to N9 are AGENTS.md's, and are not restated here. The rest are per tool, then per part-specific table. Written 2026-10-04, before the test review that maps the tests onto them; X8 and T1 added by that review, for tests that checked them with no need written.

## Every tool

- **X1.** Every result keeps the contract (`status`, `ok`, and `verified_by`, `unverified_because` or `error`), on success and on every failure: no toolchain, no probe, an exception inside the tool.
- **X2.** With more than one probe attached, a call that names none is refused with the serials to choose from; nothing picks the first.
- **X3.** Calls on different probes run together; calls on one probe wait their turn.
- **X4.** Every call leaves one trace line naming who asked, the tool, the probe and the verdict; a refusal for a held probe says whose call holds it.
- **X5.** Every tool's description is 40 to 80 words.
- **X6.** While ST's stlink-server runs, every programmer connect and listing goes through it, so a probe it keeps open is still reached; while it does not run, connects are direct.
- **X7.** A read whose CPUID check fails is not a read: wrong data is never returned as data, and a connect that read it counts as not made.
- **X8.** A call on a part whose access port was measured connects once: an agent waits for every programmer call, about 0.1 s plain and 0.4 s shared, and the STM32N6 cost one more per call before (2026-10-01).
- **T1.** The newest complete STM32CubeCLT install is used, by version number, and installs passed over are named.

## list_probes (LP)

- **LP1.** Each attached probe is listed once, with its serial, firmware, board and console port.
- **LP2.** A probe's console is the port matched to its serial, never one taken by position.
- **LP3.** It works while probes are held, and marks the held ones.
- **LP4.** A probe it could not read is counted and its errors given, not dropped.

## board_state (BS)

- **BS1.** It identifies the device and the family, on the family's access port; a port nobody measured is named as such, with the other ports that answered.
- **BS2.** It resets only when a hotplug connect cannot reach the core, never beside a held server, and `target_was_reset` says whether it did.
- **BS3.** The core's state comes from DHCSR without halting it, and "running" needs evidence that instructions retired.
- **BS4.** Program-counter samples are placed in the source when an ELF is given.
- **BS5.** Every family value carries its provenance, and only `measured` ones are presented as seen to work.
- **BS6.** A core halted by a vector catch beside a held server is named for what it likely was.

## run_firmware (RF)

- **RF1.** It is verified only by evidence from this image: console output after the load, or program-counter samples inside the image. Output the previous image printed never counts.
- **RF2.** It is failed when the load did not complete, with GDB's own output.
- **RF3.** It refuses, before touching the target, an image that is not linked for the family's RAM.
- **RF4.** It resets before loading and says so.
- **RF5.** It keeps the probe afterwards unless `keep_probe` is false, and the result says which.
- **RF6.** A load address nobody measured is disclosed on success, not only on refusal.

## release_probe (RP)

- **RP1.** It is unverified when nothing was held.
- **RP2.** It resumes a core run_until held, and says from DHCSR whether it runs.
- **RP3.** It says whose call held the probe.

## flash_firmware (FF)

- **FF1.** It takes only a raw binary, and refuses anything else before touching the probe.
- **FF2.** It refuses a flash base nobody measured unless the caller gives the address.
- **FF3.** It is verified only when the range read back matches the image byte for byte.
- **FF4.** It says what its erase took beyond the image.
- **FF5.** It stops a server holding the probe first, and says so.

## read_memory (RM)

- **RM1.** It never halts or resets the target, and refuses rather than reset when the port is closed.
- **RM2.** It refuses an address, length or alignment it cannot read whole.
- **RM3.** It refuses a register known to change state when read, by name or by address, unless `read_effects_ok`; then the read happens and the result says what it did.
- **RM4.** Any read in the peripheral range names the registers it covered.
- **RM5.** A variable is decoded with the ELF's types; an ELF the target does not run makes the read unverified.
- **RM6.** A register is decoded from the SVD file for this part; when the candidate files disagree and nothing chooses, it is refused.
- **RM7.** It reads beside a server holding the probe in shared mode, and refuses, saying why, beside one that keeps the probe to itself.

## run_until (RU)

- **RU1.** It is verified only when the chip's DFSR names a breakpoint or a watchpoint at the stop.
- **RU2.** A time limit that runs out is unverified and says where the core is.
- **RU3.** It refuses an ELF the target does not run, before any breakpoint is set.
- **RU4.** It refuses what would not stop the core: a hardware breakpoint the part cannot place, a watch of the wrong width or alignment, a software watchpoint, a watch on a register whose read changes it.
- **RU5.** A function's inlined copies stop it too.
- **RU6.** It leaves the core halted at the stop and says the rest of the chip keeps running; it never lets go of a held probe to stop.
- **RU7.** It gives the instruction at the stop and the call stack, files named as elsewhere.
- **RU8.** It refuses to use a server held over the other interface.

## reset_target (RT)

- **RT1.** It is verified only when the core halted at the address word 1 of the vector table names.
- **RT2.** It lets the core go afterwards, and a release that failed is not a verified reset.
- **RT3.** On a part whose reset PC is not predictable it is unverified, not guessed.
- **RT4.** It stops a server holding the probe first, and says so.
- **RT5.** It never claims the core is halted now.

## core_registers (CR)

- **CR1.** It gives the same registers whichever way it reads them.
- **CR2.** It leaves the core as it found it, running or halted, and says which from DHCSR.
- **CR3.** With an ELF it gives the call stack; with one the target does not run, no stack.
- **CR4.** It reports whether reaching the core needed a reset.

## read_fault_status (FS)

- **FS1.** It never halts or resets the target.
- **FS2.** It refuses cores without the registers; a clean read on a core it does not know is unverified.
- **FS3.** It decodes the registers as tests/blind/faults/contract.md states (FAULTS-N1 to N13).
- **FS4.** A register the read did not return is a failure, never a clean register.
- **FS5.** MMFAR and BFAR are given only when CFSR marks them valid.
- **FS6.** A clean read beside a held server that caught a vector is unverified.

## locate_fault (LF)

- **LF1.** It reads the frame from the stack EXC_RETURN names, and its stacked PC and LR.
- **LF2.** It refuses in thread mode, or when LR no longer holds EXC_RETURN.
- **LF3.** Words that do not look like a frame make it unverified.
- **LF4.** An imprecise bus error is noted: the stacked PC is late.
- **LF5.** With an ELF, the function, line and instruction at the stacked PC.
- **LF6.** It leaves the core as it found it.

## read_option_bytes (OB)

- **OB1.** Nothing in the package writes option bytes: some writes cannot be undone.
- **OB2.** Every option byte the programmer reports, with its value and ST's meaning, in ST's sections.
- **OB3.** Readout protection comes first.
- **OB4.** A part ST's programmer has no option bytes for fails with ST's reason.
- **OB5.** It never halts or resets the target, and reads beside a held server.

## capture_console (CC)

- **CC1.** The port is the one matched to the probe's serial or the one the setting names, never the first port; with none it refuses.
- **CC2.** What the probe buffered before the port opened comes back apart, and never counts as heard.
- **CC3.** Hearing nothing is unverified.
- **CC4.** It captures at most 60 seconds.
- **CC5.** A capture interrupted partway keeps what it heard.

## capture_swo (CS)

- **CS1.** It is verified only when text arrives on the port; silence is unverified and names a wrong clock among the causes.
- **CS2.** The result says trace output was left on.
- **CS3.** It works over SWD only.

## The part tables and settings

- **Q1.** An entry in quirks.py's FAMILIES carries only measured values; anything else arrives labelled from elsewhere.
- **Q2.** A board file is user-stated, wins over the table, and a bad one stops start-up with the reason.
- **Q3.** A part ST's database describes is seeded labelled `vendor-db`, and one the seeding refused says why.
- **E1.** A read-effects entry labelled measured names the part and the date; an assumed one says what is missing.
- **S1.** Every setting does what it says, and a bad value stops start-up with the reason.
