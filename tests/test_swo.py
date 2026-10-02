"""SWO: what a target writes to an ITM stimulus port, captured by the
programmer's -startswv.

The logs and the standard output are captured from an STM32F407 through an
ST-LINK/V2 on 2026-10-02, running a test image that writes each line to ITM
port 0 with its core at 16 MHz: captured at 16 MHz, at 15.1 MHz (6% low) and
at 32 MHz.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import probe as pm

FIXTURES = Path(__file__).parent / "fixtures"
SERIAL = "50FF73064977535531090387"


def _fixture(name: str) -> str:
    with open(FIXTURES / name, encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


class TestTheLogIsReadBetweenItsMarkers:
    def test_the_text_that_arrived_and_nothing_the_programmer_added(self) -> None:
        body = pm.parse_swv_log(_fixture("swo_f407_itm.log"))
        assert body == "".join(f"stlink-mcp hw check f407 itm tick {n}\r\n" for n in (2, 3, 4, 5))

    def test_a_clock_far_off_gives_nothing(self) -> None:
        assert pm.parse_swv_log(_fixture("swo_f407_itm_clock_wrong.log")) == ""

    def test_a_clock_a_little_off_gives_replacement_characters(self) -> None:
        body = pm.parse_swv_log(_fixture("swo_f407_itm_clock_6pc_low.log"))
        assert body and body.count("�") > len(body) / 4

    def test_a_log_that_never_started_is_none(self) -> None:
        assert pm.parse_swv_log("") is None
        assert pm.parse_swv_log("****\n\tDevice Name: STM32F4\n****\n") is None


class _FakeProgrammer:
    """Stands in for STM32_Programmer_CLI -startswv: prints the captured
    standard output, writes the captured log to the path it was given, and
    exits when it reads E."""

    log = "swo_f407_itm.log"
    stdout_text = "swo_f407_stdout.txt"
    argv: list[str] = []

    def __init__(self, argv, **kw) -> None:
        type(self).argv = argv
        Path(argv[-1]).write_bytes((FIXTURES / self.log).read_bytes())
        self.stdout = io.StringIO(_fixture(self.stdout_text))
        self.stdin = io.StringIO()
        self.returncode: int | None = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        assert "E\n" in self.stdin.getvalue(), "stopped by its own E, not killed"
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = -9


class TestTheProbeCapturesAndStops:
    def _probe(self, monkeypatch, **kw) -> pm.Probe:
        monkeypatch.setattr(pm.subprocess, "Popen", _FakeProgrammer)
        return pm.Probe(toolchain=SimpleNamespace(programmer="x"), serial=SERIAL, **kw)

    def test_the_clock_goes_in_megahertz_and_the_port_by_number(self, monkeypatch) -> None:
        text = self._probe(monkeypatch).capture_swo(16_000_000, 0, 0)
        assert text.startswith("stlink-mcp hw check f407 itm tick 2")
        argv = _FakeProgrammer.argv
        assert argv[argv.index("-startswv") + 1:-1] == ["freq=16", "portnumber=0"]
        assert "mode=HOTPLUG" in argv and "port=SWD" in argv

    def test_a_fractional_clock_is_passed_exactly(self, monkeypatch) -> None:
        """15.1 and 16.9 MHz both garbled a 16 MHz core's output: the
        programmer uses the fraction rather than rounding it away."""
        self._probe(monkeypatch).capture_swo(12_288_000, 0, 3)
        argv = _FakeProgrammer.argv
        assert argv[argv.index("-startswv") + 1:-1] == ["freq=12.288", "portnumber=3"]

    def test_jtag_is_refused_before_anything_runs(self, monkeypatch) -> None:
        probe = self._probe(monkeypatch, interface="jtag")
        _FakeProgrammer.argv = []
        with pytest.raises(pm.ProbeError, match="SWD"):
            probe.capture_swo(16_000_000, 0, 0)
        assert _FakeProgrammer.argv == []

    def test_an_answer_from_another_probe_is_refused(self, monkeypatch) -> None:
        probe = self._probe(monkeypatch)
        probe.serial = "0054003D3137511139383538"
        with pytest.raises(pm.ProbeError, match="answered from"):
            probe.capture_swo(16_000_000, 0, 0)
