# hwcheck

Checks the README's promises against real boards, through the server over stdio, as an MCP client calls it. docs/findings.md has a ledger, "What each README promise rests on"; this is how its hardware column is re-run.

```sh
uv run python tools/hwcheck/hwcheck.py
uv run python tools/hwcheck/hwcheck.py --h7 <serial> --n6 <serial> --jtag <serial> --flash
uv run python tools/hwcheck/hwcheck.py --f4 <serial> --f4-console /dev/serial/by-id/<adapter>
```

It knows the NUCLEO-H743ZI2, whose probe reports the board name NUCLEO-H743ZI, and the NUCLEO-N657X0-Q, and finds them by that name in `list_probes` when no serial is given. An STM32F407 board has no board name to find it by, so `--f4` names its probe. Each check prints PASS or FAIL and what it saw, and when a check fails, every call result since the previous check, whole. A board whose first call fails gets one FAIL and SKIP for the rest, since each of its other checks would fail too, at up to a minute each. The exit status is 1 when any check failed or none ran.

Other options: `--skip h7` or `--skip n6` leaves out a board found by name, such as one another run is using; `--keep-images` keeps the built test images and prints where; `--cycle-port HUB:PORTS` cuts power to those ports of a hub with `uhubctl` for 10 s before the H7's checks, all of them together, such as the H7's own probe and a second probe on its debug lines.

## What it changes on the boards

- It loads images into RAM on both boards. The H7 is reset at the end, so it runs its own firmware again.
- It never resets the N6, because ST's stock start-up code can close the N6's debug port until a power cycle. The N6 is left running a RAM image, which a power cycle clears.
- `--n6-closed` ends the N6's checks by closing its debug port on purpose, with the one write ST's start-up code makes, and checks what the tools say to a closed port. The N6 then needs a power cycle.
- The SWO checks on the H7 and the F407 leave the target's trace output on, including the setting that gives the SWO pin to the trace unit. On the F407, where the pin is PB3, that survives a reset; a power cycle clears it.
- It loads images into RAM on an F407 board named with `--f4` and resets it at the end.
- `--flash` writes the flash of the H7, the start of the N6's external flash, and the F407's when `--f4` is given. It saves the whole flash with STM32_Programmer_CLI first, writes a small test image and checks it, then writes the saved copy back and verifies it, even when a check failed. The saved copy is kept in a temporary directory, whose path it prints. It holds whatever the board's own firmware holds, so delete it once the board is back as it was. On the N6 it saves the first 128 KB, fills the first 64 KB sector with data so the erase shows, writes a 16 KB pattern through `flash_firmware`, and writes the saved 128 KB back; the saved copy is deleted once that readback matches.
- The read-effect checks run `h7_read_effects` and `f4_read_effects`, which leave the ADC holding a conversion and read it once on purpose. On the H7 the image also starts the RTC from the LSI if no clock was chosen for it, which lasts until power is removed. On the F407 it sets PD2 as UART5's receive pin, an input, and drives nothing.
- On the H7 it runs an image that makes the SWD pins inputs, one whose fault handler faults, and one that sleeps on WFI with nothing to wake it. Each leaves the debug port unusable until a connect under reset, which `board_state` then makes, restarting the board's own firmware.
- `--jtag <serial>` names an ST-LINK wired to the H7's JTAG connector, such as an ST-LINK/V2 through a 20-to-10-pin adapter into CN5, and adds checks over JTAG. The H7's console is assigned to that probe for those checks with `STLINK_MCP_CONSOLE`.

## Booting the N6 from its flash

`n6_flash_boot.py` measures, in steps, whether an STM32N6 booted from its external flash stays debuggable when its first-stage boot loader opens debug, and whether it can then be rewritten without moving the BOOT1 switch. RM0486 says the boot ROM leaves debug closed in flash boot, and that code running after it can open it by writing `BSEC_DBGCR` and `BSEC_AP_UNLOCK`; this checks that on a board. Each step is run with BOOT1 where its help says, and ends by saying what to do next: `write` (development boot), `boot`, `rewrite` and `closed` (flash boot, after a power cycle), and `recover` (development boot, after a power cycle). It saves the first 128 KB of the external flash before writing and `recover` puts them back. Not yet run on a board.

## The images

`images/beacon.c` is one small program, built in several variants by `build_images.py` with the Arm compiler STM32CubeCLT includes. Every variant writes 0xC0FFEE00 plus a tick count to a fixed RAM word, so a debugger can see it run; the H7 ones, and `n6_print`, also print numbered lines on the board's virtual COM port at 115200 baud: USART3 on the H7, USART1 on the N6, which runs it from its 64 MHz internal oscillator. The `_itm` variants also write each line to ITM stimulus port 0, once a debugger has enabled it, for `capture_swo`; the H7 runs at 64 MHz and the F407 at 16 MHz, their reset clocks. `n6_close` makes the write that closes the N6's debug port. `n6_boot_open`, `n6_boot_open_b` and `n6_boot_closed` are for `n6_flash_boot.py`: the first two open debug as their first act and the third does not, all three print the boot and debug registers they find, and each also comes as `<name>-boot.bin`, with ST's boot header added by `STM32_SigningTool_CLI` with no keys. `images/read_effects.c` leaves peripherals holding data and never reads them again, so that a debugger's read is the only thing that could clear their flags: SPI and ADC on both boards, UART5 receiving on the F407, and the RTC on the H7. `images/debug_target.c` is a second program, with typed globals to read by name, a variable written by one function, and a call chain that hangs when built with `-DHANG`; it is built as `h7_vars`, `f4_vars` and `n6_vars`, with `h7_vars_hang` and `f4_vars_hang` for the call stack of a hang, and the tests' ELF fixtures are built from it too. The fault variants run four ticks and then execute an undefined instruction, or for `h7_busfault` store to 0x2C000000, where nothing answers. They are built fresh into a temporary directory on every run, so nothing built is tracked or packaged.

The F407 images print on UART5, TX on PC12, at 115200 baud from the 16 MHz reset clock. That is where the board they were written on has its console; on another F407 board they still run, and the counter at 0x2001F000 shows it. `--f4-console` names a USB serial adapter wired to that pin, through `STLINK_MCP_CONSOLE`, and then the RAM run is checked by its console; without it the run is checked to be `unverified`. An adapter whose I/O level is set by a VCC-IO pin needs that pin on the target's supply.

The H7 images start PLL3 before using USART3. Without that, a reset taken while the board's own firmware clocked USART3 from PLL3 leaves the UART without a clock, and an image that sets it up from the reset defaults never prints (docs/findings.md, 2026-10-01).

## Adding a board

A board needs its family's RAM address, an image variant for it in `build_images.py`, a linker script in `images/`, and a `check_<board>` function in `hwcheck.py` with the checks it can support. A board with a virtual COM port can verify runs by console; one without is checked by its counter.
