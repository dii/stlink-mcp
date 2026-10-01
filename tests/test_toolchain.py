"""Toolchain discovery.

No hardware and no real install: these test the ordering logic directly, which
is the part that was wrong and the part that fails silently when it is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stlink_mcp.toolchain import _version_key


def _order(names: list[str]) -> list[str]:
    """Names as find_toolchain would try them, newest first."""
    return [p.name for p in sorted((Path(n) for n in names), key=_version_key, reverse=True)]


class TestNewestInstallWins:
    """find_toolchain's docstring promises "newest first when several are
    installed". It delivered the oldest for eighteen months of ST releases,
    because the sort was a string sort."""

    def test_a_two_digit_minor_beats_a_one_digit_minor(self) -> None:
        """The exact case that was broken, and the reason it went unnoticed:
        it only appears once a version number crosses from 9 to 10."""
        assert _order([
            "stm32cubeclt_1.9.0",
            "stm32cubeclt_1.10.0",
            "stm32cubeclt_1.22.0",
        ])[0] == "stm32cubeclt_1.22.0"

    def test_the_old_string_sort_would_fail_this(self) -> None:
        """Pins the bug rather than the fix.

        A test that only asserts the right answer passes against any
        implementation that happens to be right for the sample. This one states
        what the previous implementation did, so if someone reverts to a plain
        sorted(reverse=True) the failure names the reason.
        """
        names = ["stm32cubeclt_1.9.0", "stm32cubeclt_1.10.0", "stm32cubeclt_1.22.0"]
        string_sorted = sorted(names, reverse=True)
        assert string_sorted[0] == "stm32cubeclt_1.9.0", (
            "the premise of this test has changed: a plain string sort no "
            "longer picks the oldest install"
        )
        assert _order(names)[0] != string_sorted[0]

    def test_patch_and_missing_components_order_sensibly(self) -> None:
        assert _order([
            "stm32cubeclt_1.22.0",
            "stm32cubeclt_1.22.1",
            "stm32cubeclt_1.22",
        ]) == [
            "stm32cubeclt_1.22.1",
            "stm32cubeclt_1.22.0",
            "stm32cubeclt_1.22",
        ]

    def test_a_directory_with_no_version_sorts_last_and_does_not_crash(self) -> None:
        """An unversioned install directory is a real shape: ST's macOS default
        is a bare STM32CubeCLT with no number in it at all."""
        assert _order(["stm32cubeclt", "stm32cubeclt_1.22.0"]) == [
            "stm32cubeclt_1.22.0",
            "stm32cubeclt",
        ]

    def test_the_order_is_stable_when_versions_tie(self) -> None:
        """Two directories parsing to the same version must not order by
        whatever the filesystem returned, or the chosen install changes between
        runs on one machine."""
        names = ["stm32cubeclt_1.22.0", "stm32cubeclt_1.22.0-beta"]
        assert _order(names) == _order(list(reversed(names)))


class TestTheNotFoundMessageNamesWhereItLooked:
    def test_every_searched_place_is_named_when_none_exists(self, monkeypatch) -> None:
        """It fell back to "/opt/st, ~/st" when no base directory existed,
        leaving out two of the places the README says are searched."""
        import pytest

        from stlink_mcp import toolchain

        monkeypatch.delenv("STLINK_MCP_CLT_ROOT", raising=False)
        monkeypatch.setattr(toolchain, "_candidate_roots", lambda: [])
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.find_toolchain()
        message = str(raised.value)
        for place in ("/opt/st", "~/st", "/usr/local/st", "/Applications"):
            assert place in message

    def test_a_wrong_root_setting_says_what_is_missing(self, monkeypatch, tmp_path) -> None:
        import pytest

        from stlink_mcp import toolchain

        monkeypatch.setenv("STLINK_MCP_CLT_ROOT", str(tmp_path))
        with pytest.raises(toolchain.ToolchainError, match="STM32_Programmer_CLI under it"):
            toolchain.find_toolchain()

    def test_a_missing_home_does_not_stop_the_search(self, monkeypatch) -> None:
        from stlink_mcp import toolchain

        def no_home(self):
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.delenv("STLINK_MCP_CLT_ROOT", raising=False)
        monkeypatch.setattr(toolchain.Path, "expanduser", no_home)
        assert isinstance(toolchain._candidate_roots(), list)


class TestTheNewestCompleteInstallIsChosen:
    """Red-team RT9 (2026-09-03): the newest install with a programmer won even
    when it lacked the GDB server, over a complete older one."""

    @staticmethod
    def _install(base, name, *, gdb_server=True):
        root = base / name
        for rel in ("STM32CubeProgrammer/bin/STM32_Programmer_CLI",
                    "GNU-tools-for-STM32/bin/arm-none-eabi-gdb",
                    *(["STLink-gdb-server/bin/ST-LINK_gdbserver"] if gdb_server else [])):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("")
        return root

    @pytest.fixture
    def base(self, tmp_path, monkeypatch):
        from stlink_mcp import toolchain
        monkeypatch.delenv("STLINK_MCP_CLT_ROOT", raising=False)
        monkeypatch.setattr(toolchain, "_SEARCH_BASES", (str(tmp_path),))
        monkeypatch.setattr(toolchain, "_MAC_ROOT", tmp_path / "no-mac")
        return tmp_path

    def test_a_complete_older_install_beats_an_incomplete_newer_one(self, base) -> None:
        from stlink_mcp.toolchain import find_toolchain
        old = self._install(base, "stm32cubeclt_1.18.0")
        new = self._install(base, "stm32cubeclt_1.22.0", gdb_server=False)
        tc = find_toolchain()
        assert tc.root == old and tc.passed_over == (new,)
        tc.check()

    def test_with_none_complete_the_error_names_the_others(self, base) -> None:
        from stlink_mcp.toolchain import ToolchainError, find_toolchain
        self._install(base, "stm32cubeclt_1.18.0", gdb_server=False)
        new = self._install(base, "stm32cubeclt_1.22.0", gdb_server=False)
        tc = find_toolchain()
        assert tc.root == new
        with pytest.raises(ToolchainError, match="other installs found are incomplete too: .*1.18.0"):
            tc.check()

    def test_the_newest_complete_install_wins(self, base) -> None:
        from stlink_mcp.toolchain import find_toolchain
        self._install(base, "stm32cubeclt_1.18.0")
        new = self._install(base, "stm32cubeclt_1.22.0")
        assert find_toolchain().root == new and find_toolchain().passed_over == ()
