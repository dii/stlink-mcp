"""Boards the user describes, rather than ones this project characterised.

N5: it has to run on hardware that is not ours, and the person with the board
plugged in must be able to say what it is without editing an installed package.

The whole risk of this channel is that it is the one place unvalidated data
enters the quirks table at runtime, so most of what is tested here is refusal.
"""

from __future__ import annotations

import pytest

from stlink_mcp import quirks


def write(tmp_path, text: str):
    path = tmp_path / "boards.toml"
    path.write_text(text)
    return path


GOOD = '''
["0x419"]
name = "STM32F4 on a NUCLEO-F429ZI"
access_port = 0
ram_load_base = 0x20000000
vector_table_addr = 0x20000000
flash_base = 0x08000000
notes = "addresses from the reference manual, not yet run on the board"
'''


class TestWhatLoadsAndHowItIsLabelled:
    def test_a_board_file_is_read_and_keyed_by_device_id(self, tmp_path) -> None:
        boards = quirks.load_user_boards(write(tmp_path, GOOD))
        assert set(boards) == {0x419}
        assert boards[0x419].ram_load_base == 0x20000000

    def test_everything_it_contains_is_user_stated(self, tmp_path) -> None:
        board = quirks.load_user_boards(write(tmp_path, GOOD))[0x419]
        assert set(board.provenance_summary().values()) == {quirks.USER_STATED}

    def test_nothing_in_it_can_be_called_measured(self, tmp_path) -> None:
        """The load-bearing refusal.

        `measured` is the label that unlocks the write path in flash_firmware.
        This server cannot tell a value someone measured from a value someone
        typed, so if a config file could assert it, a typo would unlock an
        irreversible operation. Someone who really has measured their board can
        still flash it by passing the address explicitly, which records the
        claim as theirs rather than as the table's.
        """
        for line in ('default_provenance = "measured"', 'provenance = {}'):
            with pytest.raises(quirks.BoardConfigError, match="cannot label"):
                quirks.load_user_boards(write(tmp_path, GOOD + "\n" + line))

    def test_hex_addresses_survive_as_written(self, tmp_path) -> None:
        """TOML has hex integer literals and JSON does not, which is why this is
        TOML: a load address written in decimal is a transcription error waiting
        to happen."""
        board = quirks.load_user_boards(write(tmp_path, GOOD))[0x419]
        assert board.flash_base == 0x08000000


class TestItRefusesRatherThanQuietlyDoingSomethingElse:
    def test_an_unknown_field_is_an_error_not_an_ignored_line(self, tmp_path) -> None:
        """Ignoring it is the same failure as the provenance typo: the setting
        the user believes they made is not in effect and nothing says so."""
        bad = GOOD + '\nram_load_bass = 0x20000000\n'
        with pytest.raises(quirks.BoardConfigError, match="ram_load_bass"):
            quirks.load_user_boards(write(tmp_path, bad))

    def test_a_missing_access_port_is_refused(self, tmp_path) -> None:
        """There is no safe default. Most parts use 0 and the STM32N6 uses 1,
        and guessing wrong reports a healthy board as broken hardware."""
        without = "\n".join(
            line for line in GOOD.splitlines() if "access_port" not in line
        )
        with pytest.raises(quirks.BoardConfigError, match="access_port"):
            quirks.load_user_boards(write(tmp_path, without))

    def test_a_ram_base_with_no_vector_table_is_refused(self, tmp_path) -> None:
        """The consistency invariants used to live only in this test suite, over
        the compile-time table, so a runtime entry got none of them. Now the
        loader and the tests call the same function."""
        bad = '["0x419"]\nname = "x"\naccess_port = 0\nram_load_base = 0x20000000\n'
        with pytest.raises(quirks.BoardConfigError, match="vector_table_addr"):
            quirks.load_user_boards(write(tmp_path, bad))

    def test_a_key_that_is_not_a_device_id_is_refused(self, tmp_path) -> None:
        bad = '["STM32F4"]\nname = "x"\naccess_port = 0\n'
        with pytest.raises(quirks.BoardConfigError, match="not a device ID"):
            quirks.load_user_boards(write(tmp_path, bad))

    def test_broken_toml_says_so(self, tmp_path) -> None:
        with pytest.raises(quirks.BoardConfigError, match="not valid TOML"):
            quirks.load_user_boards(write(tmp_path, "[unclosed\n"))

    def test_a_missing_file_is_an_error_not_an_empty_set(self, tmp_path) -> None:
        """Asking for a file and getting a different set of boards is exactly the
        shape of failure this project exists to remove."""
        with pytest.raises(quirks.BoardConfigError, match="Cannot read"):
            quirks.load_user_boards(tmp_path / "nope.toml")


class TestPrecedenceOverTheBuiltInTable:
    """A user entry wins. The built-in entry describes a device ID, which is
    coarser than a part and much coarser than a board, and the person with the
    thing plugged in knows more about it than this file does.

    The override is not silent: the entry carries user-stated provenance and
    board_state reports it.
    """

    def test_a_user_board_overrides_a_characterised_family(self, tmp_path) -> None:
        override = '["0x450"]\nname = "my H7"\naccess_port = 0\nflash_base = 0x08000000\n'
        boards = quirks.load_user_boards(write(tmp_path, override))
        assert quirks.for_device_id(0x450, overrides=boards).name == "my H7"
        # and without the overrides argument, the built-in table is untouched
        assert quirks.for_device_id(0x450).name == "STM32H7"

    def test_overriding_does_not_smuggle_in_a_measured_label(self, tmp_path) -> None:
        """The dangerous case: replacing a measured entry with a typed one must
        not inherit the measured entry's standing."""
        override = '["0x450"]\nname = "my H7"\naccess_port = 0\nflash_base = 0x08000000\n'
        boards = quirks.load_user_boards(write(tmp_path, override))
        assert not quirks.for_device_id(0x450, overrides=boards).is_measured("flash_base")
        assert quirks.for_device_id(0x450).is_measured("flash_base")

    def test_an_unlisted_device_still_falls_back(self, tmp_path) -> None:
        boards = quirks.load_user_boards(write(tmp_path, GOOD))
        assert quirks.for_device_id(0xDEAD, overrides=boards) is quirks.DEFAULT


class TestTypesAreCheckedAtTheBoundary:
    """`FamilyQuirks` is a frozen dataclass and annotations are not enforcement,
    so the loader is the only thing between a TOML file and a value of the wrong
    type reaching code that assumes otherwise.

    The case that motivated this: `access_port = "0"`, quoted, an easy slip in a
    format with no schema. It constructed happily, then raised TypeError inside
    check_consistency's range test, which nothing catches, so the whole server
    died with a traceback and exit 1 instead of the one-line message and exit 2
    it was built to give. Every correctly described board in the same file died
    with it.
    """

    @pytest.mark.parametrize(
        "line, expect",
        [
            ('access_port = "0"', "must be an integer"),
            ("access_port = true", "must be an integer"),
            ("ram_load_base = 1.5", "must be an integer"),
            ("flash_base = -1", "must not be negative"),
            ('notes = 5', "must be a string"),
            ('requires_signing = "yes"', "must be a boolean"),
        ],
    )
    def test_a_wrong_type_is_refused_with_the_field_named(
        self, tmp_path, line: str, expect: str
    ) -> None:
        body = "\n".join(
            l for l in GOOD.splitlines() if not l.startswith(line.split(" =")[0] + " ")
        )
        with pytest.raises(quirks.BoardConfigError, match=expect):
            quirks.load_user_boards(write(tmp_path, body + "\n" + line))

    def test_a_zero_address_is_refused(self, tmp_path) -> None:
        """Not a plausible address on any STM32, and the likeliest way to write
        one by accident is to leave a value out and have TOML default nothing."""
        body = GOOD.replace("flash_base = 0x08000000", "flash_base = 0")
        with pytest.raises(quirks.BoardConfigError, match="not a plausible address"):
            quirks.load_user_boards(write(tmp_path, body))

    def test_the_error_is_the_designed_one_not_a_TypeError(self, tmp_path) -> None:
        """The whole point: it must be a BoardConfigError, because that is the
        only exception __main__ catches to produce exit 2 with a message."""
        body = GOOD.replace("access_port = 0", 'access_port = "0"')
        with pytest.raises(quirks.BoardConfigError):
            quirks.load_user_boards(write(tmp_path, body))


class TestTheRuntimeWiring:
    """The path every production tool actually uses.

    `test_user_boards.py` previously tested `load_user_boards` in isolation and
    called `for_device_id(id, overrides=...)` with the dict passed explicitly. It
    never called the no-argument form the tools use, and never touched the module
    global they read, so the glue that connects a board file to a live server was
    verified once by hand and by nothing that runs again.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        before = dict(quirks._USER_BOARDS)
        yield
        quirks.set_user_boards(before)

    def test_installed_boards_are_seen_by_the_no_argument_lookup(self, tmp_path) -> None:
        quirks.set_user_boards(quirks.load_user_boards(write(tmp_path, GOOD)))
        assert quirks.for_device_id(0x419).name == "STM32F4 on a NUCLEO-F429ZI"

    def test_installing_replaces_rather_than_accumulates(self, tmp_path) -> None:
        """A stale entry surviving a reload would describe a board that is no
        longer described, which is worse than not loading at all."""
        quirks.set_user_boards(quirks.load_user_boards(write(tmp_path, GOOD)))
        other = '["0x999"]\nname = "other"\naccess_port = 0\n'
        quirks.set_user_boards(quirks.load_user_boards(write(tmp_path, other)))
        assert quirks.for_device_id(0x999).name == "other"
        assert quirks.for_device_id(0x419) is quirks.DEFAULT

    def test_an_explicit_env_path_is_used_even_when_it_does_not_exist(
        self, tmp_path, monkeypatch
    ) -> None:
        """Asking for a specific file and quietly getting a different set of
        boards is the shape this project exists to remove, so a typo in the
        variable must be an error rather than a silent fallback."""
        from stlink_mcp import server

        missing = tmp_path / "nope.toml"
        monkeypatch.setenv("STLINK_MCP_BOARDS", str(missing))
        assert server.board_config_path() == missing
        with pytest.raises(quirks.BoardConfigError, match="Cannot read"):
            server.load_board_config()

    def test_no_config_anywhere_loads_nothing_and_says_so(self, monkeypatch) -> None:
        from stlink_mcp import server

        monkeypatch.delenv("STLINK_MCP_BOARDS", raising=False)
        monkeypatch.setattr(server, "_default_boards_path", lambda: None)
        assert server.board_config_path() is None
        assert server.load_board_config() == (0, None)

    def test_a_missing_home_does_not_crash_the_lookup(self, monkeypatch) -> None:
        """Path.home() raises when HOME is unset and the UID has no passwd entry.
        As a module-level constant that fired at IMPORT time, before main()
        installed the handler built to turn start-up problems into exit 2."""
        from stlink_mcp import server

        monkeypatch.delenv("STLINK_MCP_BOARDS", raising=False)
        monkeypatch.setattr(
            server.Path, "home", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError))
        )
        assert server._default_boards_path() is None
        assert server.board_config_path() is None

    def test_the_end_to_end_load_installs_what_the_file_described(
        self, tmp_path, monkeypatch
    ) -> None:
        from stlink_mcp import server

        path = write(tmp_path, GOOD)
        monkeypatch.setenv("STLINK_MCP_BOARDS", str(path))
        count, used = server.load_board_config()
        assert (count, used) == (1, path)
        assert quirks.for_device_id(0x419).provenance_of("flash_base") == quirks.USER_STATED
