"""ST's stlink-server, which GDB servers started with -t run, can keep a probe
open that nothing holds. On 2026-10-04, after a shared read of an H7 whose GDB
server was held, a plain connect to an STM32F407 whose server had been released
printed only "ST-LINK error (DEV_CONNECT_ERR)", and the tools reported "could
not reach the core" with no error at all; a shared connect worked. The fixtures
are what STM32_Programmer_CLI printed for the F407 then, and the daemon's line
in /proc/net/tcp."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from stlink_mcp import probe as pm
from stlink_mcp.probe import sharing_daemon_running as daemon_running  # before conftest patches it

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
