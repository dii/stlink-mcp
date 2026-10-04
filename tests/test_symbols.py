"""Variables read by name: their address, size and type from the ELF's DWARF.

The fixtures are tools/hwcheck/images/debug_target.c built for an STM32H7 with
STM32CubeCLT's GCC 14.3 at DWARF 5, 4 and 3; GCC encodes bitfields one way for
5 and the older way for 4 and 3, so both are covered. DUMP is g_state's 36 bytes
read from a NUCLEO-H743ZI2 running that program, without halting it, on
2026-10-02.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stlink_mcp import symbols

ELF = Path(__file__).parent / "fixtures" / "elf"
DUMP = bytes.fromhex("2efbffff0000c03f03008d00efbe3d013f014001410100004433221120010024fd070000")
EXPECTED = {
    "temperature_mc": -1234,
    "gain": 1.5,
    "mode": "MODE_RUN (3)",
    "flags": {"a": 5, "b": 17, "c": 0xBEEF},
    "samples": [317, 319, 320, 321],
    "raw": {"word": 0x11223344, "bytes": [0x44, 0x33, 0x22, 0x11]},
    "name": "0x24000120",
    "pos": {"x": -3, "y": 7},
}


@pytest.fixture(params=[5, 4, 3])
def elf(request) -> str:
    return str(ELF / f"h7_vars_dwarf{request.param}.elf")


class TestAVariableIsFoundAndDecoded:
    def test_its_address_size_and_type_come_from_the_dwarf(self, elf) -> None:
        var = symbols.find_variable(elf, "g_state")
        assert (var.address, var.size, var.type_name) == (0x24000130, 36, "state_t")
        assert var.scope == "debug_target.c"

    def test_every_kind_of_member_decodes(self, elf) -> None:
        var = symbols.find_variable(elf, "g_state")
        assert symbols.decode(var.type_die, DUMP) == EXPECTED

    def test_a_volatile_scalar_names_its_qualifier(self, elf) -> None:
        var = symbols.find_variable(elf, "g_ticks")
        assert (var.size, var.type_name) == (4, "volatile uint32_t")
        assert symbols.decode(var.type_die, (1234).to_bytes(4, "little")) == 1234

    def test_the_code_to_compare_with_the_target_is_the_image_start(self, elf) -> None:
        address, data = symbols.first_code(elf, 64)
        assert address == 0x24000000 and len(data) == 64
        assert data[:4] == (0x24080000).to_bytes(4, "little"), "the initial stack pointer"


class TestANameThatIsNotOneVariableIsRefused:
    def test_two_variables_with_one_name_are_both_listed(self, elf) -> None:
        with pytest.raises(symbols.SymbolError) as raised:
            symbols.find_variable(elf, "s_count")
        message = str(raised.value)
        assert "names 2 variables" in message and "in writer()" in message

    def test_an_unknown_name_suggests_close_ones(self, elf) -> None:
        with pytest.raises(symbols.SymbolError, match="Close names: g_state"):
            symbols.find_variable(elf, "g_stat")

    def test_a_local_has_no_fixed_address(self, elf) -> None:
        with pytest.raises(symbols.SymbolError, match="no fixed address"):
            symbols.find_variable(elf, "local2")

    def test_an_elf_without_debug_information_says_how_to_get_it(self, tmp_path) -> None:
        from elfgen import minimal_elf32
        bare = tmp_path / "bare.elf"
        bare.write_bytes(minimal_elf32(0x24000000, 0x100))
        with pytest.raises(symbols.SymbolError, match="-g"):
            symbols.find_variable(str(bare), "g_state")

    def test_a_missing_file_is_named(self) -> None:
        with pytest.raises(symbols.SymbolError, match="Cannot read"):
            symbols.find_variable("/nonexistent.elf", "g_state")


class TestTextAndNumbers:
    def test_a_char_array_is_text_and_a_uint8_array_is_numbers(self, elf) -> None:
        var = symbols.find_variable(elf, "g_state")
        raw = symbols.decode(var.type_die, DUMP)["raw"]
        assert raw["bytes"] == [0x44, 0x33, 0x22, 0x11]
        label = symbols.find_variable(elf, "g_label")
        assert (label.size, label.type_name) == (12, "char[12]")
        assert symbols.decode(label.type_die, b"debug\0\0\0\0\0\0\0") == "debug"
        assert symbols.decode(label.type_die, b"tab\there\0\0\0\0") == "tab\\there"


def test_a_functions_inlined_copies_are_found(elf) -> None:
    """delay is inlined into reset_handler at 0x240000D6 in every DWARF version;
    writer, called normally, has none. GDB's breakpoint on a function name goes
    on its out-of-line copy only (2026-10-04)."""
    assert symbols.inlined_at(elf, "delay") == (0x240000D6,)
    assert symbols.inlined_at(elf, "writer") == ()
    assert symbols.inlined_at(elf, "no_such_function") == ()
