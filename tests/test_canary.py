"""Reads that return wrong data with no error, caught by reading the core's CPUID.

On a NUCLEO-H743ZI2 whose core slept on WFI, every address read 0x01010001,
the device ID too, the programmer reported no error, and read_memory called the
result verified. A connect under reset cleared it. The fixtures are what
STM32_Programmer_CLI printed on 2026-10-03 for a read of 0x24070000 and of the
CPUID register at 0xE000ED00: asleep with a hotplug connect, then under reset,
then awake again."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import probe as pm
from stlink_mcp.toolchain import CommandResult

FIXTURES = Path(__file__).parent / "fixtures"
ASLEEP = (FIXTURES / "read_h7_asleep_with_cpuid.txt").read_text()
UNDER_RESET = (FIXTURES / "connect_h7_asleep_under_reset_with_cpuid.txt").read_text()
AWAKE = (FIXTURES / "read_h7_awake_with_cpuid.txt").read_text()
SERIAL = "0054003D3137511139383538"


def _probe(monkeypatch, answer, serial: str = SERIAL) -> tuple[pm.Probe, list[str]]:
    modes: list[str] = []

    def run(argv, timeout=90):
        mode = next(a.split("=", 1)[1] for a in argv if a.startswith("mode="))
        modes.append(mode)
        return CommandResult(argv, 0, answer(mode))

    monkeypatch.setattr(pm, "run_command", run)
    return pm.Probe(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")), serial=serial), modes


def test_the_captured_reads_say_what_they_are() -> None:
    assert pm.parse_memory(ASLEEP) == {0x24070000: 0x01010001, pm.CPUID: 0x01010001}
    assert pm.parse_memory(AWAKE) == {0x24070000: 0xC0FFEE01, pm.CPUID: 0x411FC271}


def test_a_cpuid_that_is_not_arms_condemns_the_call() -> None:
    assert "0x01010001" in pm.canary_problem(ASLEEP)
    assert pm.canary_problem(AWAKE) is None


def test_a_read_from_the_sleeping_core_fails_instead_of_returning_the_stale_word(monkeypatch) -> None:
    probe, _ = _probe(monkeypatch, lambda mode: ASLEEP)
    probe._access_port, probe.connect_mode = 0, "HOTPLUG"
    with pytest.raises(pm.ProbeError, match="CPUID"):
        probe.read_memory(0x24070000, 4)


def test_a_healthy_read_returns_only_what_was_asked_for(monkeypatch) -> None:
    probe, _ = _probe(monkeypatch, lambda mode: AWAKE)
    probe._access_port, probe.connect_mode = 0, "HOTPLUG"
    assert probe.read_memory(0x24070000, 4) == {0x24070000: 0xC0FFEE01}


def test_identify_gets_past_it_with_a_connect_under_reset(monkeypatch) -> None:
    probe, modes = _probe(monkeypatch, lambda mode: UNDER_RESET if mode == "UR" else ASLEEP)
    info = probe.identify(allow_reset=True)
    assert info.connected and probe.connect_mode == "UR" and probe.target_was_reset
    assert info.device_name == "STM32H7xx"


def test_without_leave_to_reset_it_says_why_it_did_not_connect(monkeypatch) -> None:
    probe, modes = _probe(monkeypatch, lambda mode: ASLEEP)
    info = probe.identify(allow_reset=False)
    assert not info.connected and set(modes) == {"HOTPLUG"}
    assert any("CPUID" in error for error in info.errors)
    assert "CPUID" in pm.explain_connect_failure(info), "the hint a tool returns says why"


class TestTheCoreStateIsReadWithoutHaltingIt:
    """DHCSR, DEMCR and five DWT_PCSR reads in one programmer call, as captured
    on 2026-10-03: the H7 running h7_ram, the F407 halted, the N6 locked up in a
    fault handler that faulted, and the H7 after the same fault and after a
    reset, which read alike: no instruction retired, and the sampler off."""

    @pytest.mark.parametrize("fixture, serial, state, sampler_on, samples", [
        ("core_state_h7_running.txt", SERIAL, "running", True, {0x24000130, 0x24000134, 0x24000136}),
        ("core_state_f4_halted.txt", "50FF73064977535531090387", "halted", True, {0xFFFFFFFF}),
        ("core_state_n6_locked_up.txt", "003C002C3335510535383531", "locked up", True, {0x341804F8}),
        ("core_state_h7_locked_up.txt", SERIAL, "not seen running", False, {0}),
        ("core_state_h7_after_reset.txt", SERIAL, "not seen running", False, {0}),
    ])
    def test_each_state(self, monkeypatch, fixture, serial, state, sampler_on, samples) -> None:
        text = (FIXTURES / fixture).read_text()
        probe, _ = _probe(monkeypatch, lambda mode: text, serial)
        probe._access_port = 0
        core = probe.core_state()
        assert core.state == state and core.sampler_on is sampler_on
        assert set(core.pc_samples) == samples and len(core.pc_samples) == 5

    @pytest.mark.parametrize("fixture, state, dfsr, caught", [
        ("core_state_h7_held_after_lockup.txt", "halted", 0x9, True),
        ("core_state_h7_held_running.txt", "running", 0x1, False),
    ])
    def test_dfsr_says_when_a_vector_catch_halted_it(self, monkeypatch, fixture, state, dfsr, caught) -> None:
        """Captured 2026-10-04 in shared mode beside run_firmware's held server:
        the H7 after its program faulted inside its fault handler, which the
        server reset and halted at the reset vector, and the H7 running h7_ram."""
        text = (FIXTURES / fixture).read_text()
        probe, _ = _probe(monkeypatch, lambda mode: text)
        probe._access_port = 0
        core = probe.core_state()
        assert (core.state, core.dfsr, core.vector_catch) == (state, dfsr, caught)

    def test_a_sleeping_h7s_stale_word_is_not_taken_for_a_state(self, monkeypatch) -> None:
        probe, _ = _probe(monkeypatch, lambda mode: ASLEEP)
        probe._access_port = 0
        with pytest.raises(pm.ProbeError):
            probe.core_state()


class TestARegisterReadLeavesTheCoreAsItWasFound:
    """CR2, LF6. In ST's shared mode a -coreReg halt outlived the command, on the
    N6 and the F407 (2026-10-04). A model of that core: a register read halts it,
    the programmer's -run lets it go, and the programmer prints what the fixtures
    hold, captured on the F407 beside a held server, running and then halted."""

    def _board(self, monkeypatch, halted: bool):
        board = {"halted": halted}
        printed = {False: (FIXTURES / "coreReg_f4_running_before.txt").read_text(),
                   True: (FIXTURES / "coreReg_f4_halted_before.txt").read_text()}

        def run(argv, timeout=90):
            if "-run" in argv:
                board["halted"] = False
                return CommandResult(argv, 0, "Core run\n")
            out = printed[board["halted"]]
            if "-coreReg" in argv:
                board["halted"] = True
            return CommandResult(argv, 0, out)

        monkeypatch.setattr(pm, "run_command", run)
        probe = pm.Probe(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")),
                         serial="50FF73064977535531090387", shared=True)
        probe._access_port = 0
        return probe, board

    def test_a_running_core_runs_again(self, monkeypatch) -> None:
        probe, board = self._board(monkeypatch, halted=False)
        assert "PC" in probe.core_registers() and board["halted"] is False

    def test_a_halted_core_stays_halted(self, monkeypatch) -> None:
        probe, board = self._board(monkeypatch, halted=True)
        assert "PC" in probe.core_registers() and board["halted"] is True


def test_beside_a_held_server_a_connect_never_resets(monkeypatch) -> None:
    """BS2. A connect in shared mode, beside a server holding the probe, tries no
    resetting mode even when asked it may: a reset made beside the server broke
    its GDB session (2026-10-03). The core here answers only under reset, as the
    sleeping H7 did. Found untested when the guard was taken out (2026-10-04)."""
    probe, modes = _probe(monkeypatch, lambda mode: UNDER_RESET if mode == "UR" else ASLEEP)
    probe.shared = True
    info = probe.identify(allow_reset=True)
    assert not info.connected and not probe.target_was_reset and set(modes) == {"HOTPLUG"}
