"""The ELF reader behind run_firmware's refusal of images not linked for RAM."""

from pathlib import Path

import pytest

from elfgen import elf32_segments, minimal_elf32
from stlink_mcp import image
from stlink_mcp.image import check_flash_image, flash_runs


class TestLoadSegments:
    def test_one_load_segment_at_the_chosen_address(self) -> None:
        segs = image.load_segments(minimal_elf32(0x24000000, 0x200))
        assert [(s.paddr, s.filesz) for s in segs] == [(0x24000000, 0x200)]

    def test_a_segment_that_writes_nothing_covers_nothing(self) -> None:
        """A .bss-only segment used to count as covering its first address."""
        seg = image.load_segments(elf32_segments((0x24000000, 0, 0x400)))[0]
        assert not seg.covers(0x24000000)

    def test_covers_is_half_open(self) -> None:
        seg = image.load_segments(minimal_elf32(0x24000000, 0x200))[0]
        assert seg.covers(0x24000000) and seg.covers(0x240001FF)
        assert not seg.covers(0x24000200) and not seg.covers(0x23FFFFFF)

    @pytest.mark.parametrize("data,why", [
        (b"\x7fELF not really", "magic"), (b"", "magic"),
        (minimal_elf32(0x24000000, ei_class=2), "32-bit"),
        (minimal_elf32(0x24000000, ei_data=2), "little-endian"),
        (minimal_elf32(0x24000000)[:60], "fit"),
    ])
    def test_things_that_are_not_an_elf32_image_are_refused_with_a_reason(self, data, why) -> None:
        with pytest.raises(image.ImageError, match=why):
            image.load_segments(data)


class TestCheckRamImage:
    def test_an_image_covering_the_vector_table_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "ram.elf"; p.write_bytes(minimal_elf32(0x34180000, 0x1000))
        assert image.check_ram_image(p, 0x34180400) is None

    def test_a_flash_linked_image_is_refused_and_names_both_addresses(self, tmp_path: Path) -> None:
        p = tmp_path / "flash.elf"; p.write_bytes(minimal_elf32(0x08000000, 0x1000))
        msg = image.check_ram_image(p, 0x24000000)
        assert msg and "not linked" in msg and "0x24000000" in msg and "0x08000000" in msg

    def test_a_missing_file_is_a_reason_not_a_traceback(self, tmp_path: Path) -> None:
        msg = image.check_ram_image(tmp_path / "absent.elf", 0x24000000)
        assert msg and "Cannot read" in msg

    def test_a_ram_vector_table_beside_a_flash_segment_is_refused(self, tmp_path: Path) -> None:
        """The image shape the first check let through: vector table in AXI SRAM,
        so it "covers" the family's address, and a second segment linked for
        flash, which GDB's load would then program."""
        p = tmp_path / "mixed.elf"
        p.write_bytes(elf32_segments((0x24000000, 0x400), (0x08000000, 0x800)))
        msg = image.check_ram_image(p, 0x24000000)
        assert msg and "outside the SRAM region" in msg and "0x08000000" in msg

    def test_segments_anywhere_in_the_sram_region_pass(self, tmp_path: Path) -> None:
        """H7 DTCM, AXI SRAM and SRAM1-4 are all in the window."""
        p = tmp_path / "ram.elf"
        p.write_bytes(elf32_segments(
            (0x24000000, 0x400), (0x20000000, 0x100), (0x30000000, 0x100)))
        assert image.check_ram_image(p, 0x24000000) is None

    def test_a_segment_may_end_exactly_at_the_top_of_the_region(self, tmp_path: Path) -> None:
        p = tmp_path / "top.elf"
        p.write_bytes(elf32_segments((0x24000000, 0x400), (0x3FFFFF00, 0x100)))
        assert image.check_ram_image(p, 0x24000000) is None

    def test_a_segment_crossing_the_top_of_the_region_is_refused(self, tmp_path: Path) -> None:
        p = tmp_path / "cross.elf"
        p.write_bytes(elf32_segments((0x24000000, 0x400), (0x3FFFFF00, 0x200)))
        assert "outside the SRAM region" in image.check_ram_image(p, 0x24000000)

    def test_a_segment_that_writes_nothing_is_not_refused(self, tmp_path: Path) -> None:
        """GDB loads file contents only, so a zero-filesz segment writes nothing
        wherever it sits."""
        p = tmp_path / "bss.elf"
        p.write_bytes(elf32_segments((0x24000000, 0x400), (0x60000000, 0, 0x1000)))
        assert image.check_ram_image(p, 0x24000000) is None

    def test_itcm_is_refused_and_the_reason_says_why(self, tmp_path: Path) -> None:
        """RAM at 0x00000000 on an H7, and indistinguishable from an alias of
        flash on parts that boot from it. A loud gap, not a quiet guess."""
        p = tmp_path / "itcm.elf"
        p.write_bytes(elf32_segments((0x24000000, 0x400), (0x00000000, 0x100)))
        assert "ITCM" in image.check_ram_image(p, 0x24000000)


class TestFlashRuns:
    """FF1, FF6. The bytes an ELF or Intel HEX file writes, checked against
    objcopy's own conversions of one ELF (tests/fixtures/elf/flash_runs.c):
    its HEX must give the same runs, and every run must be what objcopy's
    binary holds at that address."""

    FIX = Path(__file__).parent / "fixtures" / "elf"

    def test_the_elf_and_its_hex_write_the_same_bytes(self) -> None:
        assert flash_runs(self.FIX / "flash_runs.elf") == flash_runs(self.FIX / "flash_runs.hex")

    @pytest.mark.parametrize("name", ["flash_runs.elf", "flash_runs.hex"])
    def test_each_run_is_what_the_binary_holds_there_and_the_gap_is_in_none(self, name) -> None:
        runs = flash_runs(self.FIX / name)
        binary = (self.FIX / "flash_runs.bin").read_bytes()
        for run in runs:
            assert run.data == binary[run.address - 0x08000000:run.end - 0x08000000]
        # Code and the flash copy of .data are adjacent, one run; the config
        # word 16 KB on is another, with nothing written between.
        assert [r.address for r in runs] == [0x08000000, 0x08004000]
        assert runs[0].end < 0x08004000

    def test_a_record_with_a_wrong_checksum_is_refused(self, tmp_path) -> None:
        text = (self.FIX / "flash_runs.hex").read_text().replace(":0840000001005A5A78563412EF", ":0840000001005A5A78563412EE")
        bad = tmp_path / "bad.hex"
        bad.write_text(text)
        assert "checksum" in check_flash_image(bad)

    def test_a_hex_file_cut_short_is_refused(self, tmp_path) -> None:
        cut = tmp_path / "cut.hex"
        cut.write_text("".join((self.FIX / "flash_runs.hex").read_text().splitlines(keepends=True)[:5]))
        assert "truncated" in check_flash_image(cut)

    def test_an_elf_with_nothing_to_write_is_refused(self, tmp_path) -> None:
        empty = tmp_path / "empty.elf"
        empty.write_bytes(minimal_elf32(0x08000000, 0))
        assert "no bytes to write" in check_flash_image(empty)
