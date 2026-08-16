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
["0x413"]
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
        assert set(boards) == {0x413}
        assert boards[0x413].ram_load_base == 0x20000000

    def test_everything_it_contains_is_user_stated(self, tmp_path) -> None:
        board = quirks.load_user_boards(write(tmp_path, GOOD))[0x413]
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
        board = quirks.load_user_boards(write(tmp_path, GOOD))[0x413]
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
        bad = '["0x413"]\nname = "x"\naccess_port = 0\nram_load_base = 0x20000000\n'
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
