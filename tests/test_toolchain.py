"""Toolchain discovery.

No hardware and no real install: these test the ordering logic directly, which
is the part that was wrong and the part that fails silently when it is wrong.
"""

from __future__ import annotations

from pathlib import Path

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
