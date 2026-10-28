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
    """T1. find_toolchain's docstring promises "newest first when several are
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
        monkeypatch.setattr(toolchain, "_cubeide_installs", lambda: [])
        with pytest.raises(toolchain.ToolchainError) as raised:
            toolchain.find_toolchain()
        message = str(raised.value)
        # Each place prints as a Path, with the system's separator; Windows
        # searches C:\ST alone and names no macOS folder.
        places = [str(Path(b)) for b in toolchain._SEARCH_BASES]
        places += [str(toolchain._MAC_ROOT)] if toolchain._MAC_ROOT is not None else []
        assert places and all(place in message for place in places)
        if toolchain.os.name != "nt":
            assert "/opt/st" in message and "/Applications" in message

    def test_a_wrong_root_setting_says_what_is_missing(self, monkeypatch, tmp_path) -> None:
        import pytest

        from stlink_mcp import toolchain

        monkeypatch.setenv("STLINK_MCP_CLT_ROOT", str(tmp_path))
        with pytest.raises(toolchain.ToolchainError, match=r"STM32_Programmer_CLI(\.exe)? under it"):
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
        from stlink_mcp.toolchain import _EXE  # ".exe" on Windows, as the installer names them
        root = base / name
        for rel in ("STM32CubeProgrammer/bin/STM32_Programmer_CLI",
                    "GNU-tools-for-STM32/bin/arm-none-eabi-gdb",
                    *(["STLink-gdb-server/bin/ST-LINK_gdbserver"] if gdb_server else [])):
            (root / f"{rel}{_EXE}").parent.mkdir(parents=True, exist_ok=True)
            (root / f"{rel}{_EXE}").write_text("")
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


class TestCubeIDEsBundledToolsAreUsedWithoutCubeCLT:
    """T2. From a user's report (2026-10-27): with STM32CubeIDE 2.0.0 and no
    STM32CubeCLT, the server ran once a folder of links made the IDE's plugin
    folders look like CubeCLT. The plugin names here are the ones in that
    report; the platform and the version stamp change with every release."""

    PROGRAMMER = "com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_2.2.300.202508131133"
    SERVER = "com.st.stm32cube.ide.mcu.externaltools.stlink-gdb-server.linux64_2.2.300.202509021040"
    GNU = "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.13.3.rel1.linux64_1.0.100.202509120712"

    @staticmethod
    def _plugin(plugins, folder, rel):
        from stlink_mcp.toolchain import _EXE
        path = plugins / folder / "tools" / f"{rel}{_EXE}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def _ide(self, base, name="stm32cubeide_2.0.0", *, nested=False, server=True):
        install = base / name
        plugins = install / "STM32CubeIDE" / "plugins" if nested else install / "plugins"
        self._plugin(plugins, self.PROGRAMMER, "bin/STM32_Programmer_CLI")
        self._plugin(plugins, self.GNU, "bin/arm-none-eabi-gdb")
        if server:
            self._plugin(plugins, self.SERVER, "bin/ST-LINK_gdbserver")
        return install

    @pytest.fixture
    def base(self, tmp_path, monkeypatch):
        from stlink_mcp import toolchain
        monkeypatch.delenv("STLINK_MCP_CLT_ROOT", raising=False)
        monkeypatch.setattr(toolchain, "_SEARCH_BASES", (str(tmp_path),))
        monkeypatch.setattr(toolchain, "_MAC_ROOT", tmp_path / "no-mac")
        return tmp_path

    def test_an_ide_alone_is_found_and_says_so(self, base) -> None:
        from stlink_mcp.toolchain import CUBEIDE, find_toolchain
        install = self._ide(base)
        tc = find_toolchain()
        tc.check()
        assert tc.source == CUBEIDE and tc.root == install
        assert tc.programmer.parent.parent.parent.name == self.PROGRAMMER
        assert tc.gdb_server.parent.parent.parent.name == self.SERVER
        assert tc.gdb.parent.parent.parent.name == self.GNU
        assert (tc.programmer_bin_dir.parent / "Data_Base").parent.parent.name == self.PROGRAMMER

    def test_the_layout_with_an_extra_level_is_found_too(self, base) -> None:
        from stlink_mcp.toolchain import CUBEIDE, find_toolchain
        self._ide(base, nested=True)
        assert find_toolchain().source == CUBEIDE

    def test_a_complete_cubeclt_is_still_preferred(self, base) -> None:
        from stlink_mcp.toolchain import find_toolchain
        self._ide(base)
        clt = TestTheNewestCompleteInstallIsChosen._install(base, "stm32cubeclt_1.18.0")
        tc = find_toolchain()
        assert tc.root == clt and tc.source == "STM32CubeCLT"

    def test_an_ide_beats_an_incomplete_cubeclt_and_names_it(self, base) -> None:
        from stlink_mcp.toolchain import CUBEIDE, find_toolchain
        install = self._ide(base)
        clt = TestTheNewestCompleteInstallIsChosen._install(base, "stm32cubeclt_1.22.0", gdb_server=False)
        tc = find_toolchain()
        assert tc.source == CUBEIDE and tc.root == install and tc.passed_over == (clt,)

    def test_an_ide_missing_a_tool_is_not_used(self, base) -> None:
        from stlink_mcp.toolchain import ToolchainError, find_toolchain
        self._ide(base, server=False)
        with pytest.raises(ToolchainError, match="STM32CubeIDE"):
            find_toolchain()

    def test_the_newest_plugin_folder_with_the_tool_wins(self, base) -> None:
        """An IDE updated in place can keep the old plugin beside the new one."""
        from stlink_mcp.toolchain import find_toolchain
        install = self._ide(base)
        older = self.PROGRAMMER.replace("2.2.300.202508131133", "2.2.0.202403010000")
        self._plugin(install / "plugins", older, "bin/STM32_Programmer_CLI")
        assert find_toolchain().programmer.parent.parent.parent.name == self.PROGRAMMER

    def test_its_svd_files_come_from_the_product_database(self, base) -> None:
        """Where STM32CubeIDE 2.2.0 keeps them on the Linux bench (2026-10-28)."""
        from stlink_mcp.toolchain import find_toolchain
        install = self._ide(base)
        svds = (install / "plugins" / "com.st.stm32cube.ide.mcu.productdb.debug_2.2.500.202605201745"
                / "resources" / "cmsis" / "STMicroelectronics_CMSIS_SVD")
        svds.mkdir(parents=True)
        assert find_toolchain().svd_dir == svds

    def test_the_setting_may_name_an_ide(self, tmp_path, monkeypatch) -> None:
        from stlink_mcp.toolchain import CUBEIDE, find_toolchain
        install = self._ide(tmp_path, "somewhere-else")
        monkeypatch.setenv("STLINK_MCP_CLT_ROOT", str(install))
        assert find_toolchain().source == CUBEIDE
