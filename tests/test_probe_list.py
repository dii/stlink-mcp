"""Probe enumeration tests.

The fixture is real `STM32_Programmer_CLI -l` output captured with two
NUCLEO-N657X0-Q boards attached, trimmed only in the tail of the serial-port
list. It is kept verbatim in structure because the first version of this parser
reported four probes for two boards, and an invented fixture would not have
caught that: the duplication comes from the tool printing each serial once in
the probe list and again in the UART listing.
"""

from __future__ import annotations

from stlink_mcp.probe import parse_probe_list

TWO_BOARDS = """\
=====  DFU Interface   =====
No STM32 device in DFU mode connected
===== J-Link Interface =====
No J-Link/flasher probe detected.
===== STLink Interface =====
-------- Connected ST-LINK Probes List --------
ST-Link Probe 0 :
   ST-LINK SN  : 0038004C3335510735383531
   ST-LINK FW  : V3J17M11
   Access Port Number  : 3
   Board Name  : NUCLEO-N657X0-Q
ST-Link Probe 1 :
   ST-LINK SN  : 003E00463234510337333934
   ST-LINK FW  : V3J15M6
   Access Port Number  : 3
   Board Name  : NUCLEO-N657X0-Q
=====  UART Interface  =====
Total number of serial ports available: 34
Board Name  : NUCLEO-N657X0-Q
ST-LINK SN: 003E00463234510337333934
Port: ttyACM0
Location: /dev/ttyACM0
Description: STLINK-V3
Manufacturer: STMicroelectronics
Board Name  : NUCLEO-N657X0-Q
ST-LINK SN: 0038004C3335510735383531
Port: ttyACM1
Location: /dev/ttyACM1
Description: STLINK-V3
Manufacturer: STMicroelectronics
Port: ttyS0
Location: /dev/ttyS0
Description: N/A
Manufacturer: N/A
"""

NO_PROBES = """\
=====  DFU Interface   =====
No STM32 device in DFU mode connected
===== J-Link Interface =====
No J-Link/flasher probe detected.
===== STLink Interface =====
Error: No ST-LINK detected!
=====  UART Interface  =====
Total number of serial ports available: 32
Port: ttyS0
Location: /dev/ttyS0
Description: N/A
Manufacturer: N/A
"""


class TestProbeCount:
    def test_two_boards_are_two_probes_not_four(self) -> None:
        """The regression that motivated this file. The tool prints each serial
        twice, in two unrelated sections."""
        assert len(parse_probe_list(TWO_BOARDS)) == 2

    def test_serials_are_unique(self) -> None:
        probes = parse_probe_list(TWO_BOARDS)
        assert len({p.serial for p in probes}) == len(probes)

    def test_no_probes_yields_nothing(self) -> None:
        assert parse_probe_list(NO_PROBES) == []

    def test_serial_ports_are_not_mistaken_for_probes(self) -> None:
        """ttyS* entries have no serial number and must not create entries."""
        assert all(p.serial.startswith("00") for p in parse_probe_list(TWO_BOARDS))


class TestProbeFields:
    def test_firmware_is_read_per_probe(self) -> None:
        """The two boards are on different firmware, which is the useful case:
        fields must not leak between blocks."""
        by_serial = {p.serial: p for p in parse_probe_list(TWO_BOARDS)}
        assert by_serial["0038004C3335510735383531"].firmware == "V3J17M11"
        assert by_serial["003E00463234510337333934"].firmware == "V3J15M6"

    def test_board_name_is_read(self) -> None:
        assert all(p.board == "NUCLEO-N657X0-Q" for p in parse_probe_list(TWO_BOARDS))


class TestConsoleMapping:
    """The reason the UART section is parsed at all.

    With two boards attached, taking the first /dev/ttyACM* would verify a run
    against the wrong board's console about half the time, and that failure looks
    exactly like the firmware working.
    """

    def test_each_probe_gets_its_own_console(self) -> None:
        by_serial = {p.serial: p for p in parse_probe_list(TWO_BOARDS)}
        assert by_serial["003E00463234510337333934"].console_port == "/dev/ttyACM0"
        assert by_serial["0038004C3335510735383531"].console_port == "/dev/ttyACM1"

    def test_the_mapping_is_not_positional(self) -> None:
        """Probe 0 in the list maps to ttyACM1, not ttyACM0. Anything that pairs
        them by order rather than by serial gets this backwards."""
        probes = parse_probe_list(TWO_BOARDS)
        assert probes[0].console_port == "/dev/ttyACM1"
        assert probes[1].console_port == "/dev/ttyACM0"


class TestGdbPortAllocation:
    """Ports must be distinct per probe and stable within a process.

    An earlier version derived the port from hash(serial) % 100. Python
    randomises string hashing per process, so ports moved between runs, and
    nothing stopped two serials colliding. It surfaced on the fourth board.
    """

    def test_each_probe_gets_a_distinct_port(self) -> None:
        from stlink_mcp import server

        server._gdb_ports.clear()
        serials = [f"00{i:06X}3335510735383531" for i in range(8)]
        ports = [server._gdb_port_for(s) for s in serials]
        assert len(set(ports)) == len(serials)

    def test_a_probe_keeps_its_port(self) -> None:
        from stlink_mcp import server

        server._gdb_ports.clear()
        first = server._gdb_port_for("AAAA")
        server._gdb_port_for("BBBB")
        assert server._gdb_port_for("AAAA") == first
