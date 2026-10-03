"""Peripheral registers by name, from ST's SVD files.

These read the SVD files of the STM32CubeCLT install on the machine running
them, and skip without one. The files are not copied here: each says
Apache-2.0, but the folder they ship in carries ST's SLA0048, the agreement
that keeps the device database out of this repository, and which governs is
the owner's call. What they rely on was measured on 2026-10-02 against
STM32CubeCLT 1.22.0.
"""

from __future__ import annotations

import pytest

from stlink_mcp import svd
from stlink_mcp.toolchain import ToolchainError, find_toolchain


@pytest.fixture(scope="module")
def svd_dir():
    try:
        directory = svd.svd_dir(find_toolchain().root)
    except ToolchainError:
        pytest.skip("no STM32CubeCLT install")
    if not (directory / "STM32H743.svd").is_file():
        pytest.skip("no SVD files in the STM32CubeCLT install")
    return directory


class TestTheFileIsChosenFromWhatTheProbeReports:
    @pytest.mark.parametrize("board, device, cpu, stem", [
        ("NUCLEO-H743ZI", "STM32H7xx", "Cortex-M7", "STM32H743"),
        ("STM32H753I-EVAL2", "STM32H7xx", "Cortex-M7", "STM32H753"),
        ("NUCLEO-N657X0-Q", "STM32N6xx", "Cortex-M55", "STM32N657"),
    ])
    def test_a_board_name_names_its_part(self, svd_dir, board, device, cpu, stem) -> None:
        files, how = svd.candidates(svd_dir, device_name=device, board=board, cpu=cpu)
        assert [f.stem for f in files] == [stem] and board in how

    def test_without_a_board_the_device_name_lists_the_parts(self, svd_dir) -> None:
        files, how = svd.candidates(svd_dir, device_name="STM32F405xx/F407xx/F415xx/F417xx",
                                    board=None, cpu="Cortex-M4")
        assert [f.stem for f in files] == ["STM32F405", "STM32F407", "STM32F415", "STM32F417"]
        assert "device name" in how

    def test_files_that_agree_are_used_and_say_so(self, svd_dir) -> None:
        register, name, how = svd.resolve(svd_dir, "RCC.CR", svd=None,
                                          device_name="STM32F405xx/F407xx/F415xx/F417xx",
                                          board=None, cpu="Cortex-M4")
        assert (register.address, register.size_bits) == (0x40023800, 32)
        assert "all 4 files" in how and name == "STM32F405.svd"

    def test_files_that_disagree_are_refused(self, svd_dir) -> None:
        """RCC.CR has three layouts among the STM32H7 files (H743 and H7A3 among
        them), so an H7 with no board name to narrow it cannot be read by name."""
        with pytest.raises(svd.SvdError, match="not described the same way"):
            svd.resolve(svd_dir, "RCC.CR", svd=None, device_name="STM32H7xx", board=None, cpu="Cortex-M7")

    def test_the_caller_can_name_the_file(self, svd_dir) -> None:
        register, name, how = svd.resolve(svd_dir, "RCC.CR", svd="STM32H743", device_name="STM32H7xx",
                                          board=None, cpu="Cortex-M7")
        assert (register.address, name, how) == (0x58024400, "STM32H743.svd", "named by the caller")

    def test_nothing_to_choose_from_says_how_to_name_one(self, svd_dir) -> None:
        with pytest.raises(svd.SvdError, match="Name one with svd"):
            svd.resolve(svd_dir, "RCC.CR", svd=None, device_name=None, board=None, cpu=None)


class TestNames:
    def test_an_n6_register_is_found_without_its_prefix(self, svd_dir) -> None:
        register = svd.find(svd_dir / "STM32N657.svd", "RCC.CR")
        assert register.name == "RCC_CR"

    def test_a_register_named_after_the_peripherals_type_is_found(self, svd_dir) -> None:
        """STM32N657.svd names USART1's registers USART_BRR, USART_TDR."""
        assert svd.find(svd_dir / "STM32N657.svd", "USART1.BRR").name == "USART_BRR"

    def test_a_register_in_two_forms_lists_both(self, svd_dir) -> None:
        """Asked for USART1.ISR on 2026-10-03, it said there was no such register."""
        with pytest.raises(svd.SvdError, match="USART_ISR_FIFO_DISABLED, USART_ISR_FIFO_ENABLED"):
            svd.find(svd_dir / "STM32N657.svd", "USART1.ISR")
        chosen = svd.find(svd_dir / "STM32N657.svd", "USART1.USART_ISR_FIFO_DISABLED")
        assert chosen.address == 0x4200101C

    def test_a_derived_peripheral_has_its_own_address(self, svd_dir) -> None:
        one = svd.find(svd_dir / "STM32F407.svd", "USART1.SR")
        two = svd.find(svd_dir / "STM32F407.svd", "USART2.SR")
        assert (one.address, two.address) == (0x40011000, 0x40004400)
        assert [f.name for f in one.fields] == [f.name for f in two.fields]

    def test_a_wrong_name_suggests_close_ones(self, svd_dir) -> None:
        with pytest.raises(svd.SvdError, match="Close names: .*RCC"):
            svd.find(svd_dir / "STM32H743.svd", "RCX.CR")
        with pytest.raises(svd.SvdError, match="Close names: CR"):
            svd.find(svd_dir / "STM32H743.svd", "RCC.CRR")

    def test_a_core_register_points_at_the_core_files(self, svd_dir) -> None:
        with pytest.raises(svd.SvdError, match="Core/Cortex-M7"):
            svd.find(svd_dir / "STM32H743.svd", "SCB.CFSR")

    @pytest.mark.parametrize("query", ["RCC", "RCC.", ".CR", "RCC.CR.HSION"])
    def test_a_query_that_is_not_peripheral_dot_register_is_refused(self, svd_dir, query) -> None:
        with pytest.raises(svd.SvdError, match="PERIPHERAL.REGISTER"):
            svd.find(svd_dir / "STM32H743.svd", query)


class TestDecoding:
    def test_fields_carry_their_values(self, svd_dir) -> None:
        """0x00000083, RCC.CR's reset value in the H743 file: bits 0, 1 and 7, which
        that file names HSION, HSIKERON and CSION."""
        register = svd.find(svd_dir / "STM32H743.svd", "RCC.CR")
        fields = svd.decode(register, 0x00000083)
        assert (fields["HSION"], fields["HSIKERON"], fields["CSION"]) == (1, 1, 1)
        assert sum(v for v in fields.values() if isinstance(v, int)) == 3

    def test_a_value_is_explained_by_its_description_not_its_b_name(self, svd_dir) -> None:
        """ST names values B_0x0 and so on; the description is what says it."""
        fields = svd.decode(svd.find(svd_dir / "STM32N657.svd", "RCC.CFGR1"), 0)
        assert fields["CPUSW"]["value"] == 0
        assert fields["CPUSW"]["meaning"].startswith("hsi_ck selected")


class TestReadsThatChangeState:
    """What a read is known to do, from read_effects.py's list and ST's own
    readAction, on the files of the three boards measured."""

    def test_the_files_own_mark_is_used(self, svd_dir) -> None:
        register = svd.find(svd_dir / "STM32N657.svd", "ETH.MACISR")
        assert svd.own_mark(register)
        [effect] = svd.effects(register, "STM32N657.svd", {})
        assert effect.provenance == "vendor-db" and "STM32N657.svd" in effect.established

    def test_an_unmarked_register_is_caught_by_its_block(self, svd_dir) -> None:
        """USART3's RDR on the H743 changes state when read (measured), and the
        file does not mark it."""
        register = svd.find(svd_dir / "STM32H743.svd", "USART3.RDR")
        assert register.group == "USART" and svd.own_mark(register) is None
        [effect] = svd.effects(register, "STM32H743.svd", {})
        assert effect.provenance == "measured" and "RXNE" in effect.effect

    def test_the_same_names_are_matched_in_every_familys_file(self, svd_dir) -> None:
        """The N657 file prefixes names with the peripheral's type, USART_RDR."""
        for file_name, query in (("STM32N657.svd", "USART1.RDR"), ("STM32F407.svd", "UART5.DR"),
                                 ("STM32F407.svd", "SPI1.DR"), ("STM32F407.svd", "ADC1.DR"),
                                 ("STM32H743.svd", "ADC3.DR"), ("STM32H743.svd", "RTC.SSR")):
            register = svd.find(svd_dir / file_name, query)
            assert [e.provenance for e in svd.effects(register, file_name, {})] == ["measured"], query

    def test_a_register_with_nothing_known_has_no_effect(self, svd_dir) -> None:
        assert svd.effects(svd.find(svd_dir / "STM32H743.svd", "RCC.CR"), "STM32H743.svd", {}) == []

    def test_another_files_mark_reaches_the_same_block(self, svd_dir) -> None:
        """The H743 file does not mark FDCAN_ECR; STM32G0B1's marks the same
        block's register (2026-10-03)."""
        register = svd.find(svd_dir / "STM32H743.svd", "FDCAN1.FDCAN_ECR")
        [effect] = svd.effects(register, "STM32H743.svd", svd.vendor_marks(svd_dir))
        assert effect.provenance == "vendor-db" and "another part's file" in effect.established

    def test_a_range_names_the_registers_it_covers(self, svd_dir) -> None:
        coverage = svd.covering(svd_dir / "STM32H743.svd", 0x40004800, 0x30)
        names = [f"{r.peripheral}.{r.name}" for r in coverage.registers]
        assert "USART3.ISR" in names and "USART3.RDR" in names and coverage.unmapped == 0
