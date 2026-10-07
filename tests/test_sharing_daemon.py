"""ST's stlink-server, which GDB servers started with -t run, can keep a probe
open that nothing holds. On 2026-10-04, after a shared read of an H7 whose GDB
server was held, a plain connect to an STM32F407 whose server had been released
printed only "ST-LINK error (DEV_CONNECT_ERR)", and the tools reported "could
not reach the core" with no error at all; a shared connect worked. The fixtures
are what STM32_Programmer_CLI printed for the F407 then, and the daemon's line
in /proc/net/tcp."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest
from types import SimpleNamespace

from stlink_mcp import probe as pm
from stlink_mcp.probe import sharing_daemon_running as daemon_running  # before conftest patches it
from stlink_mcp.probe import stop_idle_daemon as real_stop_idle_daemon

FIXTURES = Path(__file__).parent / "fixtures"
PLAIN = (FIXTURES / "connect_f4_daemon_keeps_probe_plain.txt").read_text()
SHARED = (FIXTURES / "connect_f4_daemon_keeps_probe_shared.txt").read_text()
TCP = FIXTURES / "proc_net_tcp_stlink_server_listening.txt"


def test_the_refused_connect_says_why() -> None:
    info = pm.parse_target_info(PLAIN)
    assert not info.connected and info.errors == ["ST-LINK error (DEV_CONNECT_ERR)"]
    assert "another program has it open" in pm.explain_connect_failure(info)


def test_the_shared_connect_reached_the_same_probe() -> None:
    info = pm.parse_target_info(SHARED)
    assert info.connected and info.device_id == 0x413 and pm.canary_problem(SHARED) is None


def test_the_daemon_is_found_in_the_kernels_listening_sockets(tmp_path) -> None:
    assert daemon_running((str(TCP),))
    header_only = tmp_path / "tcp"
    header_only.write_text(TCP.read_text().splitlines()[0] + "\n")
    assert not daemon_running((str(header_only),))


def _probe(shared: bool = False) -> pm.Probe:
    return pm.Probe(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")), serial="50FF73064977535531090387",
                    shared=shared)


def test_while_it_runs_every_connect_and_listing_is_shared(monkeypatch) -> None:
    monkeypatch.setattr(pm, "sharing_daemon_running", lambda *a, **k: True)
    assert _probe()._connect_args(0)[-1] == "shared"
    assert pm._list_argv(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")))[-1] == "shared"


def test_without_it_only_a_probe_beside_a_held_server_is_shared() -> None:
    assert "shared" not in _probe()._connect_args(0)
    assert _probe(shared=True)._connect_args(0)[-1] == "shared"
    assert "shared" not in pm._list_argv(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")))


def test_a_probe_that_stopped_answering_is_named_as_the_probe() -> None:
    """What STM32_Programmer_CLI printed, blank lines dropped, for a hotplug
    connect through an H7's on-board STLINK-V3E that had stuck after a session
    in shared mode (2026-10-04); a second probe on the same lines reached the
    core, and only cutting the V3E's USB power cleared it: unplugging its cable,
    or on 2026-10-05 uhubctl on a hub that switches port power; with a second
    probe on the same lines, only cutting both at once reset the board."""
    output = (FIXTURES / "connect_h7_v3_stuck_dev_tcp_cmd_err.txt").read_text()
    info = pm.parse_target_info(output)
    assert not info.connected and "ST-LINK error (DEV_TCP_CMD_ERR)" in info.errors
    hint = pm.explain_connect_failure(info)
    assert "probe itself stopped answering" in hint and "cutting its USB power" in hint and "uhubctl" in hint
    assert "other probe wired to the same board at the same time" in hint


# The tests that signal a daemon: Windows has no SIGKILL, and no /proc either, so
# stop_idle_daemon returns there before any signal (test_without_proc_it_does_nothing).
_signals = pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="no SIGKILL on this system")


class TestAnIdleDaemonIsStopped:
    """X11. A model of /proc: processes as directories with comm and cmdline,
    a daemon that exits on SIGTERM unless told not to, and the daemon's socket
    counted as listening while its directory exists."""

    @staticmethod
    def _proc(tmp_path, processes: dict[int, tuple[str, str]]) -> Path:
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "self").mkdir()
        for pid, (comm, argv0) in processes.items():
            (proc / str(pid)).mkdir()
            (proc / str(pid) / "comm").write_text(comm + "\n")
            (proc / str(pid) / "cmdline").write_bytes(argv0.encode() + b"\0-p\0")
        return proc

    @pytest.fixture
    def machine(self, tmp_path, monkeypatch):
        import shutil
        import signal
        state = SimpleNamespace(signals=[], stubborn=set(), proc=None)

        def kill(pid, sig):
            state.signals.append((pid, sig))
            if sig == signal.SIGKILL or pid not in state.stubborn:
                shutil.rmtree(state.proc / str(pid), ignore_errors=True)

        def build(processes, stubborn=()):
            state.proc = self._proc(tmp_path, processes)
            state.stubborn = set(stubborn)
            monkeypatch.setattr(pm.os, "kill", kill)
            monkeypatch.setattr(pm, "stop_idle_daemon",
                                lambda *a, **k: real_stop_idle_daemon(state.proc, grace=0.3))
            monkeypatch.setattr(pm, "sharing_daemon_running",
                                lambda *a, **k: any((state.proc / d / "comm").read_text().strip() == "stlink-server"
                                                    for d in __import__("os").listdir(state.proc) if d.isdigit()))
            return state
        return build

    DAEMON = (412, ("stlink-server", "/usr/bin/stlink-server"))
    GDB_SERVER = (500, ("ST-LINK_gdbserv", "/opt/st/stm32cubeclt_1.22.0/STLink-gdb-server/bin/ST-LINK_gdbserver"))

    @_signals
    def test_with_no_gdb_server_the_daemon_is_stopped_and_the_connect_is_plain(self, machine) -> None:
        import signal
        state = machine(dict([self.DAEMON, (77, ("bash", "/usr/bin/bash"))]))
        argv = _probe()._connect_args(0)
        assert "shared" not in argv and state.signals == [(412, signal.SIGTERM)]

    def test_a_daemon_a_gdb_server_uses_is_left_and_the_connect_is_shared(self, machine) -> None:
        state = machine(dict([self.DAEMON, self.GDB_SERVER]))
        assert _probe()._connect_args(0)[-1] == "shared" and state.signals == []
        assert pm._list_argv(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")))[-1] == "shared"

    @_signals
    def test_a_listing_with_no_gdb_server_stops_it_too(self, machine) -> None:
        state = machine(dict([self.DAEMON]))
        assert "shared" not in pm._list_argv(SimpleNamespace(programmer=Path("STM32_Programmer_CLI")))
        assert state.signals

    @_signals
    def test_one_that_ignores_sigterm_is_killed(self, machine) -> None:
        import signal
        state = machine(dict([self.DAEMON]), stubborn={412})
        _probe()._connect_args(0)
        assert state.signals == [(412, signal.SIGTERM), (412, signal.SIGKILL)]

    def test_without_proc_it_does_nothing(self, tmp_path) -> None:
        assert real_stop_idle_daemon(tmp_path / "no-proc") == []
