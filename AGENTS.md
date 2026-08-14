# stlink-mcp — working rules

> This file is the single source of rules for this repo. `CLAUDE.md` is a one-line
> `@AGENTS.md` import, and that line is load-bearing: Claude Code does not discover a bare
> `AGENTS.md`. `GEMINI.md` is a symlink to this file, so never write to it.

An MCP server that lets an agent program, run and inspect an STM32 target over ST-LINK,
driving the STM32CubeCLT command line tools. Runs locally, on the machine the board is
plugged into. Intended to be published, so it must make sense to someone whose board is
not ours.

## The one rule that matters

**A tool reports success only after checking that the thing it was asked to do actually
happened.** This is the whole reason the project exists. The vendor tools do not do it:
`STM32_Programmer_CLI -s` prints "Start operation achieved successfully" for a start that
left the program counter untouched, and a GDB load against an already-running target
completes cleanly while changing nothing.

Consequences for any change here:

- Never return `ok: True` on the strength of an exit code. Verify the effect.
- Where a claim cannot be verified, say so in the result (`verified_by`, `warning`). An
  unverifiable claim reported as verified is the specific harm this project exists to stop.
- Prefer a loud gap to a quiet guess. An unknown device family must refuse the operation,
  not assume an address.

## Claims about tool behaviour are tested, not assumed

Every wrong turn in this project's history came from believing documentation or a search
result over the artefact in front of us. Three examples worth keeping:

- The MCP SDK's API was assumed to be `mcp.server.fastmcp`. It is `mcp.server.MCPServer`
  in 2.x. One import check would have settled it, and did.
- Core debug on the STM32N6 was concluded to be unavailable, from an error message. It was
  an access-port problem.
- The access port itself was then concluded settled, having changed two variables at once
  (a firmware upgrade and the flag) without separating them.

So: run the probe, read the header, open the archive. Do not reason from the changelog.

## Quirks belong in the table, not the code

`src/stlink_mcp/quirks.py` holds everything family specific. Adding a family is one entry
there plus a device-ID mapping. Fill in only values verified on real hardware; the tests in
`tests/test_quirks.py` enforce internal consistency (a RAM load address without a vector
table address, for instance, is rejected).

Every entry carries `notes`, and that field is the point rather than decoration: it is what
stops the next person rediscovering the quirk the hard way.

## Tests

`uv run pytest`. Parser samples are real captured tool output, never invented, because an
invented sample tests the parser against your belief about the format, which is the thing
most likely to be wrong. Hardware-dependent paths are not unit tested; they are exercised
by hand against a board and the result recorded in the commit message.

## Public repo hygiene

- No estate specifics, no host names, no paths from the author's machine in code or docs.
- No secrets, obviously, and no serial numbers of real probes outside test fixtures.
- Not affiliated with STMicroelectronics, and the name deliberately avoids their
  STM32Cube product brand.
- Prose in `README.md` follows the author's voice: plain descriptive words, no idioms, and
  a plain ASCII `-` rather than an em-dash.
