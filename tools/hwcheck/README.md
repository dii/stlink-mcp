# hwcheck

Checks the README's promises against real boards, through the server over stdio, as an MCP client calls it. docs/findings.md ends with a ledger of what each promise rests on; this is how the hardware column of that ledger is re-run.

```sh
uv run python tools/hwcheck/hwcheck.py
uv run python tools/hwcheck/hwcheck.py --h7 <serial> --n6 <serial> --jtag <serial> --flash
uv run python tools/hwcheck/hwcheck.py --f4 <serial> --f4-console /dev/serial/by-id/<adapter>
```

It knows the NUCLEO-H743ZI and the NUCLEO-N657X0-Q, and finds them by board name in `list_probes` when no serial is given. An STM32F407 board has no board name to find it by, so `--f4` names its probe. Each check prints PASS or FAIL and what it saw, and when a check fails, every call result since the previous check, whole. A board whose first call fails gets one FAIL and SKIP for the rest, since each of its other checks would fail too, at up to a minute each. The exit status is 1 when any failed.

## What it changes on the boards

- It loads images into RAM on both boards. The H7 is reset at the end, so it runs its own firmware again.
- It never resets the N6, because ST's stock start-up code can close the N6's debug port until a power cycle. The N6 is left running a RAM image, which a power cycle clears.
- `--n6-closed` ends the N6's checks by closing its debug port on purpose, with the one write ST's start-up code makes, and checks what the tools say to a closed port. The N6 then needs a power cycle.
- The SWO checks on the H7 and the F407 leave the target's trace output on, including the setting that gives the SWO pin to the trace unit. On the F407, where the pin is PB3, that survives a reset; a power cycle clears it.
- It loads images into RAM on an F407 board named with `--f4` and resets it at the end.
- `--flash` writes the flash of the H7, and of the F407 when `--f4` is given. It saves the whole flash with STM32_Programmer_CLI first, writes a small test image and checks it, then writes the saved copy back and verifies it, even when a check failed. The saved copy is kept in a temporary directory, whose path it prints. It holds whatever the board's own firmware holds, so delete it once the board is back as it was.
- `--jtag <serial>` names an ST-LINK wired to the H7's JTAG connector, such as an ST-LINK/V2 through a 20-to-10-pin adapter into CN5, and adds checks over JTAG. The H7's console is assigned to that probe for those checks with `STLINK_MCP_CONSOLE`.

## The images

`images/beacon.c` is one small program, built in several variants by `build_images.py` with the Arm compiler STM32CubeCLT includes. Every variant writes 0xC0FFEE00 plus a tick count to a fixed RAM word, so a debugger can see it run; the H7 ones also print numbered lines on the board's virtual COM port, USART3 at 115200 baud. The `_itm` variants also write each line to ITM stimulus port 0, once a debugger has enabled it, for `capture_swo`; the H7 runs at 64 MHz and the F407 at 16 MHz, their reset clocks. `n6_close` makes the write that closes the N6's debug port. The fault variants run four ticks and then execute an undefined instruction, or for `h7_busfault` store to 0x2C000000, where nothing answers. They are built fresh into a temporary directory on every run, so nothing built is tracked or packaged.

The F407 images print on UART5, TX on PC12, at 115200 baud from the 16 MHz reset clock. That is where the board they were written on has its console; on another F407 board they still run, and the counter at 0x2001F000 shows it. `--f4-console` names a USB serial adapter wired to that pin, through `STLINK_MCP_CONSOLE`, and then the RAM run is checked by its console; without it the run is checked to be `unverified`. An adapter whose I/O level is set by a VCC-IO pin needs that pin on the target's supply.

The H7 images start PLL3 before using USART3. Without that, a reset taken while the board's own firmware clocked USART3 from PLL3 leaves the UART without a clock, and an image that sets it up from the reset defaults never prints (docs/findings.md, 2026-10-01).

## Adding a board

A board needs its family's RAM address, an image variant for it in `build_images.py`, a linker script in `images/`, and a `check_<board>` function in `hwcheck.py` with the checks it can support. A board with a virtual COM port can verify runs by console; one without is checked by its counter.
