"""The `stlink-mcp` command line, which the README counts as public API.

It used to ignore every argument: `--help` and `--version` started the server,
which then waited on stdin for an MCP client that was never coming.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from stlink_mcp import __main__ as entry
from stlink_mcp import __version__, server
from stlink_mcp.result import failed, verified
from stlink_mcp.toolchain import ToolchainError


def test_version_prints_the_version_and_exits(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        entry.main(["--version"])
    assert stopped.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_says_it_is_started_by_an_mcp_client(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        entry.main(["--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    assert "MCP client" in out and "--check" in out


def test_an_unknown_argument_is_refused_rather_than_ignored(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        entry.main(["--serial", "AAA1"])
    assert stopped.value.code == 2


class TestCheck:
    @pytest.fixture
    def machine(self, monkeypatch):
        """A toolchain, no vendor database, no board file, one probe."""
        monkeypatch.delenv("STLINK_MCP_GDB_PORT", raising=False)
        monkeypatch.setattr(
            server, "_toolchain", lambda: SimpleNamespace(root=Path("/opt/st/clt"))
        )
        monkeypatch.setattr(server, "load_vendor_seed", lambda: "vendor seeding is OFF")
        monkeypatch.setattr(server, "load_board_config", lambda: (0, None))
        monkeypatch.setattr(server, "list_probes", lambda: verified(
            "programmer enumeration", count=1, probes=[{
                "serial": "0038004C3335510735383531", "board": "NUCLEO-N657X0-Q",
                "firmware": "V3J17M11", "console_port": "/dev/ttyACM0",
                "console_port_source": "probe list",
            }],
        ))

    def test_a_ready_machine_exits_0_and_lists_its_probes(self, machine, capsys) -> None:
        with pytest.raises(SystemExit) as stopped:
            entry.main(["--check"])
        assert stopped.value.code == 0
        out = capsys.readouterr().out
        assert str(Path("/opt/st/clt")) in out
        assert "0038004C3335510735383531" in out and "/dev/ttyACM0 (probe list)" in out

    def test_no_toolchain_exits_1_and_says_so(self, machine, monkeypatch, capsys) -> None:
        def missing():
            raise ToolchainError("No STM32CubeCLT install found.")

        monkeypatch.setattr(server, "_toolchain", missing)
        with pytest.raises(SystemExit) as stopped:
            entry.main(["--check"])
        assert stopped.value.code == 1
        assert "No STM32CubeCLT install found" in capsys.readouterr().out

    def test_a_failed_listing_exits_1(self, machine, monkeypatch, capsys) -> None:
        monkeypatch.setattr(server, "list_probes", lambda: failed("enumeration timed out"))
        with pytest.raises(SystemExit) as stopped:
            entry.main(["--check"])
        assert stopped.value.code == 1
        assert "enumeration timed out" in capsys.readouterr().out

    def test_an_unusable_setting_exits_2(self, machine, monkeypatch, capsys) -> None:
        monkeypatch.setenv("STLINK_MCP_GDB_PORT", "6123x")
        with pytest.raises(SystemExit) as stopped:
            entry.main(["--check"])
        assert stopped.value.code == 2
        assert "STLINK_MCP_GDB_PORT" in capsys.readouterr().out


def test_the_version_is_not_a_second_copy_of_the_tag() -> None:
    """The git tag is the only copy of the version. It was a literal "0.1.0" in
    the package beside a pyproject saying so; no version number may be written
    in the package's source but the fallback for a tree never installed."""
    import re
    import stlink_mcp

    package = Path(stlink_mcp.__file__).parent
    assigned = {m for f in package.glob("*.py")
                for m in re.findall(r"__version__\s*=\s*[\"']([^\"']+)[\"']", f.read_text())}
    assert assigned <= {"0.0.0+unknown"}, assigned
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", __version__)
