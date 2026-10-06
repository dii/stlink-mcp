# tools

Scripts for working on stlink-mcp itself. None of them is part of the server.

- `hwcheck/` runs the README's promises against attached boards, through the server over stdio, and prints PASS or FAIL for each. Run it before a release on whatever boards you have; see [hwcheck/README.md](hwcheck/README.md).
- `survey_device_db.py` surveys STM32CubeProgrammer's device databases in your own install. The figures quoted about them in docs/findings.md come from it, so they can be re-run against any install rather than trusted.
- `dwctl.py` is a development harness that holds debug sessions open across many load-and-run cycles, driven by lines written to a FIFO. It was built to find why the STM32N6's debug access port closes (docs/findings.md, 2026-08-14 to 08-16), which needed many consecutive runs through one session. It reaches into the server's modules directly and writes to fixed paths under `/tmp`, so it is for experiments, not for use against a board you care about.
