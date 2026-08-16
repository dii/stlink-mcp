# stlink-mcp — working rules

> This file is the single source of rules for this repo. `CLAUDE.md` is a one-line
> `@AGENTS.md` import, and that line is load-bearing: Claude Code does not discover a bare
> `AGENTS.md`. `GEMINI.md` is a symlink to this file, so never write to it.

An MCP server that lets an agent program, run and inspect an STM32 target over ST-LINK,
driving the STM32CubeCLT command line tools. Runs locally, on the machine the board is
plugged into. Intended to be published, so it must make sense to someone whose board is
not ours.

## Intent

**stlink-mcp exists so that what an AI coding agent believes about an STM32 target is what
is actually true of that target.**

The vendor command line tools report success for operations they did not perform.
`STM32_Programmer_CLI -s` prints "Start operation achieved successfully" for a start that
left the program counter untouched, and a GDB load against an already-running target
completes cleanly while changing nothing.

A human developer usually catches that, because they hold information the tool never gave
them: the LED on the board, the console text that looks like the previous build, the board
in their hand. **An agent has none of that.** The text a tool returns is its entire model of
the hardware. So a tool that overstates does not merely fail an operation. It makes the
agent confidently wrong, and the agent then writes that wrongness into code, commit messages
and documentation, where it costs far more than the failed operation did.

The primary user is therefore an agent, with a human supervising. Where the two want
different things, the agent's need to not be misled wins.

### The needs, each checkable by reading one result

- **N1. Every result carries its verdict and its evidence.** `verified` names what was
  observed, `unverified` names what was not checked and is not success, `failed` names the
  cause. Never return `ok: True` on the strength of an exit code.
- **N2. A missing fact is reported, never inferred.** An uncharacterised part refuses the
  operation rather than assuming an address. Prefer a loud gap to a quiet guess.
- **N3. Anything the agent cannot observe for itself is stated in the result:** whether the
  target was reset, whether the answer came from a cache, whether the access port in use is
  not the family's, whether another process holds the probe.
- **N4. Nothing perturbs the target silently.** Any operation that halts, resets or restarts
  the target either refuses or reports that it did so.
- **N5. It runs on hardware that is not ours.** Part-specific facts live in data rather than
  code, and an unknown part still identifies itself rather than failing completely.
- **N6. Every part-specific fact records how it was established.** Measured on hardware, and
  taken from a vendor source but not yet confirmed, are different states and are never
  presented as the same one.

N1 to N4 are what the code earns today. N5 is half done. **N6 does not exist yet**, and it
is the one that makes broad device coverage possible without any of it claiming to be
measured. See `ROADMAP.md`.

### Non-goals

It does not build firmware, does not sign images, is not a human-facing debugger interface,
does not abstract over non-ST probes, and does not replace STM32CubeProgrammer. It drives it.

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
