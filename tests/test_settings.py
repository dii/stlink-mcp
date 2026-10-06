"""Environment settings: parsed where used, refused at start-up when unusable."""

from __future__ import annotations

import pytest

from stlink_mcp import settings


class TestNumbersAreCheckedNotTrusted:
    def test_defaults_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(settings.GDB_PORT, raising=False)
        monkeypatch.delenv(settings.CONSOLE_BAUD, raising=False)
        assert settings.gdb_port_base() == 61234
        assert settings.console_baud() == 115200

    def test_a_valid_value_is_used(self, monkeypatch) -> None:
        monkeypatch.setenv(settings.GDB_PORT, "50000")
        assert settings.gdb_port_base() == 50000

    @pytest.mark.parametrize("name,value", [
        (settings.GDB_PORT, "6123x"), (settings.GDB_PORT, "80"),
        (settings.GDB_PORT, "65534"), (settings.CONSOLE_BAUD, "115k"),
        (settings.CONSOLE_BAUD, "0"),
    ])
    def test_an_unusable_value_is_an_error_naming_the_setting(
        self, monkeypatch, name: str, value: str
    ) -> None:
        monkeypatch.setenv(name, value)
        with pytest.raises(settings.SettingError, match=name):
            settings.check()


class TestTheInstallDirectoryIsChecked:
    """S1."""

    def test_a_directory_without_the_programmer_is_refused(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("STLINK_MCP_CLT_ROOT", str(tmp_path))
        with pytest.raises(settings.SettingError, match="STLINK_MCP_CLT_ROOT"):
            settings.check()

    def test_unset_is_not_checked_at_start_up(self, monkeypatch) -> None:
        """A machine without the tools still starts, and each tool says why."""
        monkeypatch.delenv("STLINK_MCP_CLT_ROOT", raising=False)
        settings.check()


class TestConsoleBelongsToAProbe:
    """A bare path applied to several boards is the wrong-board failure the
    console mapping exists to prevent, so it applies only to a lone probe."""

    def test_unset_assigns_nothing(self, monkeypatch) -> None:
        monkeypatch.delenv(settings.CONSOLE, raising=False)
        assert settings.console_for("AAA1", 1) == (None, None)

    def test_a_bare_path_applies_to_a_single_probe(self, monkeypatch) -> None:
        monkeypatch.setenv(settings.CONSOLE, "/dev/ttyUSB0")
        assert settings.console_for("AAA1", 1) == ("/dev/ttyUSB0", None)

    def test_a_bare_path_with_several_probes_applies_to_none_and_says_so(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(settings.CONSOLE, "/dev/ttyUSB0")
        device, note = settings.console_for("AAA1", 2)
        assert device is None
        assert "applied to none" in note and "<probe serial>=" in note

    def test_pairs_name_their_probe_whatever_the_case(self, monkeypatch) -> None:
        monkeypatch.setenv(settings.CONSOLE, "aaa1=/dev/ttyUSB0, BBB2=/dev/ttyUSB1")
        assert settings.console_for("AAA1", 2) == ("/dev/ttyUSB0", None)
        assert settings.console_for("BBB2", 2) == ("/dev/ttyUSB1", None)
        assert settings.console_for("CCC3", 3) == (None, None)

    @pytest.mark.parametrize("value", [
        "/dev/ttyUSB0,/dev/ttyUSB1",    # two bare paths, no probe for either
        "AAA1=/dev/ttyUSB0,/dev/ttyUSB1",  # a bare path mixed with pairs
        "AAA1=",                          # a pair with no path
        "=/dev/ttyUSB0",                  # a pair with no serial
        "AAA1=/dev/a,aaa1=/dev/b",        # one probe named twice
    ])
    def test_a_malformed_value_is_refused(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv(settings.CONSOLE, value)
        with pytest.raises(settings.SettingError, match=settings.CONSOLE):
            settings.check()


class TestStartUpRefusesAnUnusableSetting:
    def test_one_line_and_exit_2_rather_than_a_traceback(
        self, monkeypatch, capsys
    ) -> None:
        """It used to be int() at import time, so the traceback came out of
        `from .server import ...` before main() could say anything."""
        from stlink_mcp import __main__ as entry

        monkeypatch.setenv(settings.GDB_PORT, "6123x")
        monkeypatch.setattr(entry, "_install_signal_handlers", lambda: None)
        with pytest.raises(SystemExit) as stopped:
            entry.main([])
        assert stopped.value.code == 2
        err = capsys.readouterr().err.strip()
        assert err.count("\n") == 0 and settings.GDB_PORT in err
