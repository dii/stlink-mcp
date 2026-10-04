"""Instructions from the ELF, by the toolchain's objdump.

Agents decoded Thumb instructions by hand from words read off the target in
four of seven tasks of an evaluation, and one tried to run a disassembler it
did not have (2026-10-04). STM32CubeCLT ships objdump, which disassembles an
address range and, with -l, names the source line of each instruction and the
function an inlined copy comes from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .toolchain import Toolchain, run_command

#: The most instructions one call returns; a function longer than this is cut.
LIMIT = 256

_INSTRUCTION = re.compile(r"^\s*([0-9a-f]+):\t((?:[0-9a-f]+ ?)+?)\s*\t(.+?)\s*$")
_SOURCE = re.compile(r"^(\S.*?):(\d+)(?: \(discriminator \d+\))?\s*$")
_FUNCTION = re.compile(r"^([A-Za-z_][\w.$]*)\(\):\s*$")


@dataclass(frozen=True)
class Instruction:
    address: int
    code: bytes
    """As stored in memory, little-endian: a 16-bit Thumb halfword per group of
    four hex digits, a 32-bit word for a literal pool's `.word`."""
    text: str
    function: str | None
    """The function this instruction belongs to, the inlined one where objdump
    names one."""
    file: str | None
    line: int | None


def parse(output: str) -> list[Instruction]:
    """objdump -d -l output, as instructions in address order."""
    found: list[Instruction] = []
    function = file = None
    line: int | None = None
    for raw in output.splitlines():
        if (m := _INSTRUCTION.match(raw)):
            code = b"".join(int(g, 16).to_bytes(len(g) // 2, "little") for g in m.group(2).split())
            found.append(Instruction(int(m.group(1), 16), code, " ".join(m.group(3).split()), function, file, line))
        elif (m := _FUNCTION.match(raw)):
            function = m.group(1)
        elif (m := _SOURCE.match(raw)) and not raw.endswith(":") and "file format" not in raw:
            file, line = m.group(1), int(m.group(2))
    return found


def run(toolchain: Toolchain, elf: Path, start: int, stop: int) -> list[Instruction]:
    """Disassemble [start, stop). By range and never by name: objdump's
    --disassemble=<name> takes the first of two functions that share a name,
    without a word, and prints nothing for an address."""
    argv = [str(toolchain.objdump), "-d", "-l", f"--start-address=0x{start:08X}",
            f"--stop-address=0x{stop:08X}", str(elf)]
    return parse(run_command(argv, timeout=60).stdout)
