"""Probe enumeration tests.

The fixture is real `STM32_Programmer_CLI -l` output captured with two
NUCLEO-N657X0-Q boards attached, trimmed only in the tail of the serial-port
list. It is kept verbatim in structure because the first version of this parser
reported four probes for two boards, and an invented fixture would not have
caught that: the duplication comes from the tool printing each serial once in
the probe list and again in the UART listing.
"""

from __future__ import annotations

from pathlib import Path

from stlink_mcp.probe import parse_probe_list

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    """Real captured tool output, kept in a file once it is too long to inline."""
    return (_FIXTURES / name).read_text()


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


    def test_ports_are_spaced_for_the_swo_port(self) -> None:
        """Each server opens -p and -p+1, so consecutive probes must not be
        one apart. Measured against a running server; a stride of 1 puts board
        two's GDB port on board one's SWO port."""
        from stlink_mcp import server

        server._gdb_ports.clear()
        ports = sorted(server._gdb_port_for(f"SN{i}") for i in range(4))
        gaps = [b - a for a, b in zip(ports, ports[1:])]
        assert all(gap >= 2 for gap in gaps), ports


class TestEmptyFieldsDoNotCaptureTheNextLine:
    """Captured with four NUCLEO-N657X0-Q and one NUCLEO-H743ZI attached.

    The N6 boards were running firmware, so their debug ports were closed and
    the probe reported blank `ST-LINK FW` and `Board Name` for all four, with
    four `ST-LINK error (DEV_CONNECT_ERR)` lines above the list. The H743 was
    the only board with those fields populated.

    That mix is the point. Every earlier fixture had populated fields, so none
    could reach this: a `\\s*` after the colon crosses the newline, and an empty
    field then captures the first token of whatever follows. The four N6 boards
    came back with firmware "Access", taken from `Access Port Number`, and the
    last probe in the list came back with the section's closing rule as its
    board name.
    """

    FIXTURE = _fixture("probe_list_n6_closed_plus_h743.txt")

    def test_all_five_probes_are_found(self) -> None:
        assert len(parse_probe_list(self.FIXTURE)) == 5

    def test_an_empty_firmware_field_is_absent_not_invented(self) -> None:
        by_serial = {p.serial: p for p in parse_probe_list(self.FIXTURE)}
        n6 = by_serial["0034004F3335510735383531"]
        assert n6.firmware != "Access"
        assert not n6.firmware

    def test_the_populated_board_still_reads_correctly(self) -> None:
        """The fix must not cost the case that already worked."""
        by_serial = {p.serial: p for p in parse_probe_list(self.FIXTURE)}
        h743 = by_serial["0054003D3137511139383538"]
        assert h743.firmware == "V3J7M2"
        assert h743.board == "NUCLEO-H743ZI"

    def test_the_last_probe_does_not_absorb_the_closing_rule(self) -> None:
        """The final block runs to the end of the section, so it is the one that
        can swallow the '------' separator as a board name."""
        last = parse_probe_list(self.FIXTURE)[-1]
        assert last.serial == "003C002C3335510535383531"
        assert not last.board
        assert "---" not in (last.board or "")

    def test_console_mapping_survives_five_probes_out_of_order(self) -> None:
        """Probe-list order and UART order disagree completely here, which is
        the case that positional pairing gets wrong."""
        by_serial = {p.serial: p for p in parse_probe_list(self.FIXTURE)}
        assert by_serial["0054003D3137511139383538"].console_port == "/dev/ttyACM4"
        assert by_serial["003C002C3335510535383531"].console_port == "/dev/ttyACM1"
        assert by_serial["0034004F3335510735383531"].console_port == "/dev/ttyACM2"
