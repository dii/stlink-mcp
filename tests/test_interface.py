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
