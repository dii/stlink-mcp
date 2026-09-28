# Contributing

The rules for changing this project are in [AGENTS.md](AGENTS.md), and they apply
to people as much as to agents. This file says how to run it, what makes a
report useful, and how to contribute a board, without restating those rules.

## Running the tests

```sh
uv sync
uv run pytest
```

No hardware is needed. The tests that read a real STM32CubeCLT install skip when
there is none, and everything else fakes the probe. Anything that talks to a
board is exercised by hand instead, and the commit message records what was run,
on which board, probe firmware and STM32CubeCLT version, and what it showed. If
you have not run a change on hardware, say so in the commit message; that is a
fact about the change, not an apology.

## Reporting a problem

Open an issue with:

- the output of `uv run stlink-mcp --check`
- the tool that was called, its arguments, and the full result it returned
- the board, the probe generation (ST-LINK/V2, V2-1, V3) and its firmware
- the STM32CubeCLT version and the operating system

If the problem is in how probes are listed, paste the output of
`STM32_Programmer_CLI -l` exactly as printed. Every parser here is tested against
captured output, and only ST-LINK V3 probes have been captured so far, so a
listing from any other probe is useful even when nothing is wrong.

## Contributing a board

A value is `measured` when someone ran it on the part and watched it work, and
only measured values go into `FAMILIES` in `src/stlink_mcp/quirks.py`. A test
fails the build otherwise. For a part this project has not measured:

1. Describe it in a board file first (see the README). Everything there is
   `user-stated`, and the tools will use it.
2. Run an image from RAM with `run_firmware` and confirm it started: console
   output from the new image, or a counter read back with `read_memory`
   advancing between two reads.
3. Open an issue or pull request with the device ID, the board, each value and
   how you established it. A value you took from a reference manual and did not
   run is still worth sending, labelled as such.

Never include files from STM32CubeProgrammer's `Data_Base` directory, or a table
made from them. They are under ST's licence (SLA0048), which forbids putting them
under open-source terms. Quoting one address is fine.

## Where text goes

`README.md` is for people using the server and carries conclusions only.
Measurements, dates, and what was got wrong go in
[docs/findings.md](docs/findings.md). Tool docstrings are read by the calling
agent on every session, so they stay between roughly 40 and 80 words.
