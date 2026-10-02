"""N8: the debug interface is a parameter, not an assumption.

The connect banners are captured from real probes on 2026-10-01: an
ST-LINK/V2 on a NUCLEO-H743ZI's JTAG connector, the NUCLEO-N657X0-Q's own
probe asked for JTAG (it does it), and the NUCLEO-H743ZI's own STLINK-V3E asked
for JTAG (it does not, and says so in the words a missing target uses).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import probe as pm
from stlink_mcp import runner
from stlink_mcp.toolchain import CommandResult

FIXTURES = Path(__file__).parent / "fixtures"


def _banner(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestTheBannerSaysWhichInterfaceConnected:
    def test_jtag_through_an_st_link_v2(self) -> None:
        info = pm.parse_target_info(_banner("connect_v2_jtag_under_reset.txt"))
        assert info.connected and info.device_id == 0x450
        assert (info.interface, info.interface_khz) == ("jtag", 9000)

    def test_a_probe_with_no_board_reports_none_not_dashes(self) -> None:
        assert pm.parse_target_info(_banner("connect_v2_jtag_under_reset.txt")).board is None
        assert pm.parse_target_info(_banner("connect_n6_v3_jtag.txt")).board == "NUCLEO-N657X0-Q"

    def test_jtag_on_the_n6_boards_own_probe(self) -> None:
        info = pm.parse_target_info(_banner("connect_n6_v3_jtag.txt"))
        assert info.connected and info.device_id == 0x486
        assert (info.interface, info.interface_khz) == ("jtag", 21333)

    def test_a_probe_that_cannot_do_jtag_reads_as_no_target(self) -> None:
        info = pm.parse_target_info(_banner("connect_v3e_jtag_refused.txt"))
        assert not info.connected
        assert info.interface is None, "no clock line, so nothing observed"
        assert any("Unable to get core ID" in e for e in info.errors)

    def test_an_swd_banner_says_swd(self) -> None:
        info = pm.parse_target_info("SWD freq    : 8000 KHz\nDevice ID   : 0x450\n")
        assert (info.interface, info.interface_khz) == ("swd", 8000)


class TestTheProbeConnectsOverTheInterfaceItWasGiven:
    def _probe(self, **kw) -> pm.Probe:
        return pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="AAA", **kw)

    def test_swd_is_the_default(self) -> None:
        assert "port=SWD" in self._probe()._connect_args(0)

    def test_jtag_when_asked(self) -> None:
        args = self._probe(interface="jtag")._connect_args(0)
        assert "port=JTAG" in args and "port=SWD" not in args

    def test_anything_else_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not one of swd, jtag"):
            self._probe(interface="uart")

    def test_a_jtag_failure_says_the_probe_may_not_do_jtag(self, monkeypatch) -> None:
        refused = _banner("connect_v3e_jtag_refused.txt")
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 1, refused))
        probe = pm.Probe(toolchain=SimpleNamespace(programmer="x"),
                         serial="0054003D3137511139383538", interface="jtag")
        info = probe.identify(allow_reset=False)
        hint = pm.explain_connect_failure(info, probe.interface)
        assert "may not do JTAG" in hint.split("\n  - ")[1], "it is the first likely cause"
        assert "may not do JTAG" not in pm.explain_connect_failure(info, "swd")


class TestTheGdbServerIsStartedForTheInterface:
    def _argv(self, monkeypatch, interface: str) -> list[str]:
        seen: dict[str, list[str]] = {}

        class _Stop(Exception):
            pass

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            raise _Stop

        monkeypatch.setattr(runner, "port_is_taken", lambda port: False)
        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        tc = SimpleNamespace(gdb_server="srv", programmer_bin_dir="bin")
        with pytest.raises(_Stop):
            runner.GdbServer(tc, 0, 61300, serial="AAA", interface=interface).__enter__()
        return seen["argv"]

    def test_swd_passes_d(self, monkeypatch) -> None:
        assert "-d" in self._argv(monkeypatch, "swd")

    def test_jtag_is_the_absence_of_d(self, monkeypatch) -> None:
        assert "-d" not in self._argv(monkeypatch, "jtag")

    def test_a_held_server_on_the_other_interface_is_not_reused(self, monkeypatch) -> None:
        held = SimpleNamespace(interface="swd", port=61300, is_alive=lambda: True)
        monkeypatch.setitem(runner._sessions, "AAA", held)
        with pytest.raises(runner.RunError, match="uses SWD, and this call asked for JTAG"):
            runner.acquire_server(SimpleNamespace(), 0, 61300, serial="AAA", interface="jtag")
        assert runner.acquire_server(SimpleNamespace(), 0, 61300, serial="AAA", interface="swd") is held


class TestAServerThatNeverListensSaysWhy:
    """Red-team RT8 (2026-09-03): the server was stopped and its output thrown
    away, so the agent got "did not start listening" and nothing else."""

    def test_its_output_is_in_the_error(self, monkeypatch) -> None:
        import io

        class _Proc:
            stdout = io.StringIO("ST-LINK firmware update available.\nWaiting for debugger connection...\n")
            def poll(self):
                return None
            def terminate(self):
                pass
            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(runner, "_LISTEN_TIMEOUT", 0.3)
        monkeypatch.setattr(runner, "port_is_taken", lambda port: False)
        monkeypatch.setattr(runner, "port_is_accepting", lambda port: False)
        monkeypatch.setattr(runner.subprocess, "Popen", lambda argv, **kw: _Proc())
        tc = SimpleNamespace(gdb_server="srv", programmer_bin_dir="bin")
        with pytest.raises(runner.RunError, match="firmware update available"):
            runner.GdbServer(tc, 0, 61300, serial="AAAA0001").__enter__()


class TestAnOldProbeIsNotCalledAbsent:
    """Captured 2026-10-01: an ST-LINK/V2 on V2J16S4 was listed by -l, then a
    connect naming its serial said "Serial number not found", and one by index
    said its firmware was too old. The hint used to say no such probe was
    attached."""

    def test_by_serial_the_hint_names_old_firmware_as_a_cause(self) -> None:
        info = pm.parse_target_info(_banner("connect_v2_old_firmware_by_serial.txt"))
        hint = pm.explain_connect_failure(info)
        assert not info.connected
        assert "too old" in hint and "STLinkUpgrade" in hint and "list_probes" in hint

    def test_by_index_the_hint_says_upgrade(self) -> None:
        info = pm.parse_target_info(_banner("connect_v2_old_firmware_by_index.txt"))
        assert "too old" in pm.explain_connect_failure(info)

    def test_the_f407_through_the_upgraded_v2(self) -> None:
        info = pm.parse_target_info(_banner("connect_v2_f407.txt"))
        assert info.connected and info.device_id == 0x413 and info.cpu == "Cortex-M4"
        assert info.board is None and (info.interface, info.interface_khz) == ("swd", 4000)


class TestAFlashWriteThatCannotEraseSaysWhy:
    """Captured on 2026-10-02 from an STM32F407 through an ST-LINK/V2, with
    sector 11 write-protected in its option bytes, as flash_firmware reported
    it (the programmer's output, stripped). The programmer stops at the erase,
    and its output never says why."""

    def test_the_failure_names_write_protection_first(self, monkeypatch) -> None:
        output = _banner("program_f407_sector_write_protected.txt")
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 1, output))
        probe = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="50FF73064977535531090387")
        with pytest.raises(pm.ProbeError) as raised:
            probe.program_flash("wrp-test.bin", 0x080F0000)
        message = str(raised.value)
        assert message.index("Write protection") < message.index("Programming failed")
        assert "-ob displ" in message and "Error: failed to erase memory" in message

    def test_other_programming_failures_carry_no_such_claim(self, monkeypatch) -> None:
        output = _banner("connect_v2_f407.txt")
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 1, output))
        probe = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="50FF73064977535531090387")
        with pytest.raises(pm.ProbeError) as raised:
            probe.program_flash("image.bin", 0x08000000)
        assert "Write protection" not in str(raised.value)


class TestAClosedPortIsNamedAsACause:
    """Captured on 2026-10-02 from a NUCLEO-N657X0-Q after an image turned the
    SYSCFG clock off, as ST's stock start-up code does: the programmer still
    connects and identifies the part on AP1, and the read itself fails; the GDB
    server will not start, and blames power and cabling. The F407 and the H7
    fail a read across the end of a memory region with the same line."""

    def test_the_one_call_read_names_both_causes(self, monkeypatch) -> None:
        output = _banner("read_n6_ap1_closed.txt")
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 1, output))
        probe = pm.Probe(toolchain=SimpleNamespace(programmer="x"),
                         serial="003C002C3335510535383531", try_first=1)
        with pytest.raises(pm.ProbeError) as raised:
            probe.identify_and_read(0x341B0000, 32)
        message = str(raised.value)
        assert "end of" in message and "power cycle" in message
        assert message.index("power cycle") < message.index("Output was")

    def test_the_two_call_read_says_it_too(self, monkeypatch) -> None:
        output = _banner("read_n6_ap1_closed.txt")
        monkeypatch.setattr(pm, "run_command", lambda argv, timeout=90: CommandResult(argv, 1, output))
        probe = pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial="003C002C3335510535383531")
        with pytest.raises(pm.ProbeError) as raised:
            probe.read_memory(0x341B0000, 32)
        assert "power cycle" in str(raised.value)

    def test_a_server_that_cannot_attach_is_not_blamed_on_cabling(self) -> None:
        message = runner.server_exit_message(_banner("gdbserver_n6_ap1_closed.txt"))
        assert message.index("closed") < message.index("Please check power and cabling")
        assert "board_state" in message and "power cycle" in message

    def test_other_server_exits_carry_no_such_claim(self) -> None:
        message = runner.server_exit_message("Error: port 61234 already in use\n")
        assert "closed" not in message and message.startswith("GDB server exited before listening")
