"""Parser tests.

Every sample here is real output captured from STM32_Programmer_CLI against a
NUCLEO-N657X0-Q, not invented. Invented samples test the parser against your
belief about the format, which is the thing most likely to be wrong.
"""

from __future__ import annotations

from stlink_mcp.probe import (
    parse_core_registers,
    parse_memory,
    parse_target_info,
)

CONNECTED = """\
      -------------------------------------------------------------------
                        STM32CubeProgrammer v2.23.0
      -------------------------------------------------------------------
ST-LINK SN  : 0038004C3335510735383531
ST-LINK FW  : V3J17M11
Board       : NUCLEO-N657X0-Q
Voltage     : 3.26V
SWD freq    : 8000 KHz
Connect mode: Hot Plug
Reset mode  : Software reset
Device ID   : 0x486
Revision ID : Rev B
Device name : ST32N657
Device type : MCU
Device CPU  : Cortex-M55
BL Version  : --
"""

# What a closed debug port looks like. Note the CLI retries, so every error
# appears twice, and the banner fields are still present.
NO_CORE = """\
ST-LINK SN  : 0038004C3335510735383531
ST-LINK FW  : V3J17M11
Board       : NUCLEO-N657X0-Q
Voltage     : 3.26V
Error: Unable to get core ID
Error: No STM32 target found! If your product embeds Debug Authentication, please perform a discovery using Debug Authentication
ST-LINK SN  : 0038004C3335510735383531
ST-LINK FW  : V3J17M11
Board       : NUCLEO-N657X0-Q
Voltage     : 3.26V
Error: Unable to get core ID
Error: No STM32 target found! If your product embeds Debug Authentication, please perform a discovery using Debug Authentication
"""

MEMORY = """\
  Address:      : 0x34180400
0x34180400 : 34200000 34180841 3418089D 3418089D
0x34180410 : 3418089D 3418089D 3418089D 3418089D
"""

CORE_REGS = """\
R3       = 0xCAFEDEAD
R11      = 0x56021800
SP       = 0x341037A8
LR       = 0x18003521
PC       = 0x18003514
XPSR     = 0x21000000
PRIMASK  = 0x00
"""


class TestTargetInfo:
    def test_connected_target_is_fully_parsed(self) -> None:
        info = parse_target_info(CONNECTED)
        assert info.connected
        assert info.device_id == 0x486
        assert info.device_name == "ST32N657"
        assert info.cpu == "Cortex-M55"
        assert info.stlink_firmware == "V3J17M11"
        assert info.board == "NUCLEO-N657X0-Q"
        assert info.errors == []

    def test_unreachable_core_is_not_reported_as_connected(self) -> None:
        """The banner still parses, so presence of fields must not imply a core."""
        info = parse_target_info(NO_CORE)
        assert not info.connected
        assert info.stlink_firmware == "V3J17M11"
        assert info.device_id is None

    def test_repeated_errors_are_collapsed(self) -> None:
        """The CLI retries and repeats itself; callers should see each cause once."""
        info = parse_target_info(NO_CORE)
        assert len(info.errors) == 2

    def test_device_id_selects_the_family(self) -> None:
        assert parse_target_info(CONNECTED).quirks.name == "STM32N6"

    def test_unknown_device_falls_back_to_generic(self) -> None:
        info = parse_target_info(CONNECTED.replace("0x486", "0x999"))
        assert info.quirks.name == "generic"


class TestMemory:
    def test_words_are_addressed_individually(self) -> None:
        words = parse_memory(MEMORY)
        assert words[0x34180400] == 0x34200000
        assert words[0x34180404] == 0x34180841
        assert words[0x34180410] == 0x3418089D
        assert len(words) == 8

    def test_no_data_yields_no_words(self) -> None:
        assert parse_memory("Error: cannot read memory") == {}


class TestCoreRegisters:
    def test_registers_are_parsed(self) -> None:
        regs = parse_core_registers(CORE_REGS)
        assert regs["PC"] == 0x18003514
        assert regs["SP"] == 0x341037A8
        assert regs["PRIMASK"] == 0x00

    def test_absent_registers_yield_nothing(self) -> None:
        assert parse_core_registers("Error: cannot halt") == {}
