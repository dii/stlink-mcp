"""DA1. Instructions from objdump, checked against what they describe: each
instruction's bytes against the ELF's own bytes at its address, its source line
against the source file, an inlined copy against the DWARF.

The fixtures are what STM32CubeCLT 1.22.0's objdump printed with -d -l for the
ranges below, from tests/fixtures/elf: h7_vars_dwarf5.elf, built from
tools/hwcheck/images/debug_target.c, and disasm.elf, built from disasm_a.c and
disasm_b.c with the same GCC for a Cortex-M4.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from stlink_mcp import disasm, symbols

FIXTURES = Path(__file__).parent / "fixtures"
ELF = FIXTURES / "elf"
SOURCE = Path(__file__).parent.parent / "tools" / "hwcheck" / "images" / "debug_target.c"

# fixture, ELF, the range objdump was asked for
CASES = [
    ("objdump_range_h7_writer.txt", "h7_vars_dwarf5.elf", 0x24000042, 0x24000054),
    ("objdump_range_h7_reset_handler.txt", "h7_vars_dwarf5.elf", 0x24000092, 0x24000128),
    ("objdump_range_disasm_scale_b.txt", "disasm.elf", 0x08000034, 0x0800004C),
    ("objdump_range_disasm_long_function.txt", "disasm.elf", 0x08000066, 0x08000316),
]


def _parsed(fixture: str) -> list[disasm.Instruction]:
    return disasm.parse((FIXTURES / fixture).read_text())


def _elf_bytes(name: str) -> dict[int, int]:
    found: dict[int, int] = {}
    with open(ELF / name, "rb") as f:
        for segment in ELFFile(f).iter_segments():
            if segment.header.p_type == "PT_LOAD":
                base = segment.header.p_vaddr
                found.update((base + n, b) for n, b in enumerate(segment.data()))
    return found


def _body(name: str) -> range:
    """The lines of a function's definition in debug_target.c, from its
    signature to its closing brace."""
    lines = SOURCE.read_text().splitlines()
    first = next(n for n, text in enumerate(lines, 1) if re.search(rf"\b{name}\(", text) and "{" in text)
    last = first if lines[first - 1].rstrip().endswith("}") else next(
        n for n, text in enumerate(lines, 1) if n > first and text == "}")
    return range(first, last + 1)


@pytest.mark.parametrize("fixture, elf, start, stop", CASES)
def test_every_instruction_in_the_range_is_there_with_the_elfs_bytes(fixture, elf, start, stop) -> None:
    """The instruction an agent reads at an address is the one stored there:
    none dropped, none run together, the bytes as the ELF holds them, literal
    pools included."""
    instructions = _parsed(fixture)
    stored = _elf_bytes(elf)
    assert instructions[0].address == start
    for i, after in zip(instructions, instructions[1:]):
        assert after.address == i.address + len(i.code), hex(i.address)
    assert instructions[-1].address + len(instructions[-1].code) == stop
    for i in instructions:
        assert i.code == bytes(stored[i.address + n] for n in range(len(i.code))), hex(i.address)


@pytest.mark.parametrize("function", ["writer", "reset_handler"])
def test_each_instruction_names_a_line_of_its_function_or_of_what_was_inlined(function) -> None:
    fixture = f"objdump_range_h7_{function}.txt"
    delay = _body("delay")
    for i in _parsed(fixture):
        assert i.file == "images/debug_target.c", hex(i.address)
        assert i.line in (delay if i.function == "delay" else _body(function)), (hex(i.address), i.line)


def test_the_inlined_copy_is_named_for_the_function_it_came_from() -> None:
    """RU5's inlined copy, as the DWARF places it, is the instruction the parse
    gives to delay, inside reset_handler."""
    sites = symbols.inlined_at(str(ELF / "h7_vars_dwarf5.elf"), "delay")
    by_address = {i.address: i for i in _parsed("objdump_range_h7_reset_handler.txt")}
    assert sites and all(by_address[a].function == "delay" for a in sites)
    assert {i.function for i in by_address.values()} == {"reset_handler", "delay"}


def test_two_functions_of_one_name_are_both_in_the_table() -> None:
    """DA2 rests on this: the symbol table keeps both static functions called
    scale, each with its file, where objdump's --disassemble=scale printed the
    first alone (checked 2026-10-04)."""
    scales = [f for f in symbols.functions(str(ELF / "disasm.elf")) if f.name == "scale"]
    assert sorted((f.start, f.file) for f in scales) == [(0x08000000, "disasm_a.c"), (0x08000034, "disasm_b.c")]
