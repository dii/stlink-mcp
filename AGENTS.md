# stlink-mcp — working rules

> This file is the single source of rules for this repo. `CLAUDE.md` is a one-line
> `@AGENTS.md` import, and that line is load-bearing: Claude Code does not discover a bare
> `AGENTS.md`. `GEMINI.md` is a symlink to this file, so never write to it.

An MCP server that lets an agent program, run and inspect an STM32 target over ST-LINK,
driving the STM32CubeCLT command line tools. Runs locally, on the machine the board is
plugged into. Intended to be published, so it must make sense to someone whose board is
not ours.

## Intent

### What it is

An MCP server, on the current MCP 2.x SDK, that a user runs **locally on the machine their
STM32 board is plugged into** and attaches to a local agent session. Once attached, the agent
can detect the attached board or be told what it is, and can then **flash it, reset it and
interrogate it** over whatever debug and communication interfaces that board provides: SWD,
JTAG, the serial console, and later SWO or RTT.

Getting it running and attached is part of the product, not a footnote. A user should be able
to install it, point an agent client at it, and have a working board conversation without
reading the source.

### Deployment model, and one constraint that outlives it

One user, one machine, boards on the desk in front of them. **There is no team access model:
no authentication, no approvals, no multi-tenancy.** That is a deliberate scope decision, not
an oversight, and it is what keeps the tool small.

It may become a feature later, so **the architecture must not prohibit it.** Nothing may
assume a single implicit caller in a way that would have to be unpicked to add identity,
sessions or per-caller policy. Concretely: state keyed to a caller rather than to a module
global, and every operation traceable to who asked for it even while the answer is always
"the local user".

### The quality bar, and the reason the project exists at all

**What an AI coding agent believes about the target must be what is actually true of it.**

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
  code. The board is either detected, or **described by the user without editing the
  installed package**, and an unknown part still identifies itself rather than failing
  completely.
- **N6. Every part-specific fact records how it was established.** Measured on hardware,
  taken from a vendor database, and stated by the user are three different states and are
  never presented as the same one.
- **N7. Flash, reset and interrogate are all first-class operations.** Reset in particular is
  something the user asks for and gets told the truth about, not only a side effect of
  something else.
- **N8. The interface is a parameter, not an assumption.** SWD, JTAG and the serial console
  are choices the user or the board makes. Nothing hardcodes one of them.

And one architectural constraint, which is checked by reading the design rather than a
result:

- **N9. Nothing assumes a single implicit caller.** Identity and per-caller policy are out of
  scope today and must remain addable without a redesign.

Which of these the code meets today is tracked in one place, the "Where it stands" table in
`ROADMAP.md`. This file states the needs and does not restate their status.

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
there plus a device-ID mapping. The tests in `tests/test_quirks.py` enforce internal
consistency (a RAM load address without a vector table address, for instance, is rejected).

**Entries in `FAMILIES` are hand-written and must be fully measured on hardware.** That is no
longer only a request: `TestTheContributionRuleIsCheckedNotJustStated` fails the build if any
entry there carries a value that is not `measured`. If you have a value you have not run on a
board, it does not belong in that dict.

Unmeasured values are allowed in the table, but they arrive from elsewhere and they arrive
labelled. Every field has a provenance, one of `measured`, `vendor-db`, `user-stated` or
`assumed`, and `DEFAULT` is the worked example: its access port 0 is `assumed`, because it is
a fact about the STM32 range and not about any part. What the label changes is behaviour, not
presentation. `flash_firmware` refuses a base it did not get from a measurement or from the
caller, because that write persists and there is no readback to catch a wrong address.

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
- Three audiences, three places. `README.md` is for someone who wants to use it, and
  carries conclusions only. Measurements, dates, and what was got wrong on the way go in
  `docs/findings.md`, which the README links to. Tool docstrings are instructions for the
  agent that calls the tool, roughly 40 to 80 words: an agent loads every one of them on
  every session, and a long description buries the one line that changes what it does.
