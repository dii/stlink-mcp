# Contributing

This file says how to run the tests and the hardware check, what makes a report useful, and how to contribute a board. The full rules for changing the code are in [AGENTS.md](AGENTS.md), written for the agents that work in this repository; the ones a contributor needs are below.

## Running the tests

```sh
uv sync
uv run pytest
```

No hardware is needed. The tests that read a real STM32CubeCLT install skip when there is none, and everything else fakes the probe. Each test names the need it checks, from [tests/NEEDS.md](tests/NEEDS.md). Parser tests run on output captured from the real tools, never on output written by hand.

## Running the hardware check

`tools/hwcheck` runs the README's promises through the server against a NUCLEO-H743ZI2, a NUCLEO-N657X0-Q or an STM32F407 board (`--f4 <serial>`), and prints what held; see [tools/hwcheck/README.md](tools/hwcheck/README.md). It changes the boards: it resets them and runs test images on them, which on the H7 make the SWD pins inputs and start the RTC from the LSI, and its SWO checks leave trace output on until a power cycle. `--flash` rewrites flash, saving it first and writing it back. Read that README's "What it changes on the boards" before you run it.

If you change something that talks to a board, say in the pull request what you ran, on which board, probe firmware and STM32CubeCLT version, and what it showed. If you have not run it on hardware, say so; that is a fact about the change, not an apology.

## Reporting a problem

Open an issue with:

- the output of `uv run stlink-mcp --check`
- the tool that was called, its arguments, and the full result it returned
- the board, the probe generation (ST-LINK/V2, V2-1, V3) and its firmware
- the STM32CubeCLT version and the operating system

If the problem is in how probes are listed, paste the output of `STM32_Programmer_CLI -l` exactly as printed. Every parser here is tested against captured output, and only on-board ST-LINK V3s and one ST-LINK/V2's listing have been captured so far, so a listing from any other probe, a V2-1 especially, is useful even when nothing is wrong.

## Contributing a board

A value is `measured` when someone ran it on the part and watched it work, and only measured values go into `FAMILIES` in `src/stlink_mcp/quirks.py`; a test fails the build otherwise. The table is keyed on the device ID the probe reports, which is coarser than a part number, so put in only values that hold for everything behind the ID, and keep anything that varies by board or by image out of it. For a part this project has not measured:

1. Describe it in a board file first (see the README). Everything there is `user-stated`, and the tools will use it.
2. `access_port`: a connect that answers is not enough, since a port can answer identity and still not halt the core. `core_registers`, which halts the core, working on that port is.
3. `ram_load_base` and `vector_table_addr`: run an image linked to load at `ram_load_base`, with its vector table at `vector_table_addr`, with `run_firmware`, and confirm it started, by console output from the new image, or with `sample_values` and `until "<counter> changes"` on a counter it increments.
4. `flash_base`: write a .bin there with `flash_firmware` and `address`, see it `verified`, then `reset_target` and see the core halt at that image's reset handler.
5. Open an issue or pull request with the device ID, the board, each value and how you established it. A value you took from a reference manual and did not run is still worth sending, labelled as such.

Never include files from STM32CubeProgrammer's `Data_Base` directory, or a table made from them. They are under ST's licence (SLA0048), which forbids putting them under open-source terms. Quoting one address is fine.

## Where text goes

`README.md` is for people using the server and carries conclusions only. Measurements, dates, and what was got wrong go in [docs/findings.md](docs/findings.md). Tool descriptions are read by the calling agent on every session, so each stays between 40 and 80 words.
