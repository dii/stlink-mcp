"""The list of reads that change state, checked without ST's files, so CI runs it.

The entries themselves were measured on an STM32H743 and an STM32F407 on
2026-10-03 (docs/findings.md); tests/test_svd.py checks them against the
installed SVD files where STM32CubeCLT is present."""

from __future__ import annotations

import re

import pytest

from stlink_mcp import read_effects
from stlink_mcp.quirks import ASSUMED, MEASURED


class TestEveryEntrySaysHowItIsKnown:
    @pytest.mark.parametrize("entry", read_effects.KNOWN, ids=lambda e: f"{e.block}:{','.join(e.registers)}")
    def test_an_entry(self, entry) -> None:
        re.compile(entry.block)
        assert entry.registers and all(r == r.upper() for r in entry.registers)
        assert entry.provenance in (MEASURED, ASSUMED)
        if entry.provenance == MEASURED:
            # Measured names the part and the date, so a reader can tell what it rests on.
            assert re.search(r"STM32\w+", entry.established) and re.search(r"\d{4}-\d{2}-\d{2}", entry.established)
        else:
            assert entry.established.startswith("not measured:")

    def test_no_two_entries_claim_the_same_register(self) -> None:
        seen = set()
        for entry in read_effects.KNOWN:
            for register in entry.registers:
                assert (entry.block, register) not in seen
                seen.add((entry.block, register))


class TestNamesAreMatchedByBlock:
    @pytest.mark.parametrize("name, peripheral, group, base", [
        ("RDR", "USART3", "USART", "RDR"),          # STM32H743: no prefix
        ("USART_RDR", "USART1", "USART", "RDR"),    # STM32N657: the block's name
        ("RTC_SSR", "RTC", "RTC", "SSR"),           # STM32H743's RTC
        ("FDCAN_ECR", "FDCAN1", "FDCAN", "ECR"),    # the instance number dropped
        ("CR1", "SPI1", "SPI", "CR1"),
    ])
    def test_the_prefix_comes_off(self, name, peripheral, group, base) -> None:
        assert read_effects.base_name(name, peripheral, group) == base

    def test_a_block_matches_whole(self) -> None:
        assert read_effects.known("USART", "RDR") and read_effects.known("LPUART", "RDR")
        assert not read_effects.known("USART_X", "RDR") and not read_effects.known(None, "RDR")
        assert not read_effects.known("GPIO", "IDR")

    def test_another_files_mark_is_used_only_without_one_here(self) -> None:
        vendor = {("FDCAN", "ECR"): ("STM32G0B1.svd", "CEL: clear")}
        [effect] = read_effects.effects_of(peripheral="FDCAN1", name="FDCAN_ECR", group="FDCAN",
                                           own_mark=None, file_name="STM32H743.svd", vendor=vendor)
        assert "another part's file" in effect.established and "STM32G0B1.svd" in effect.established
        [own] = read_effects.effects_of(peripheral="FDCAN1", name="FDCAN_ECR", group="FDCAN",
                                        own_mark="CEL: clear", file_name="STM32G0B1.svd", vendor=vendor)
        assert "another" not in own.established


class TestOnlyPeripheralAddressesAreLookedUp:
    @pytest.mark.parametrize("start, length, device", [
        (0x24000000, 1024, False),   # RAM
        (0x08000000, 4, False),      # flash
        (0xE000ED28, 4, False),      # the core's own fault registers
        (0x40004824, 4, True),       # a UART
        (0x5FFFFFFC, 8, True),       # straddling the end of the region
        (0x3FFFFFFC, 8, True),       # straddling its start
        (0xA0000000, 4, True),       # the external device region
    ])
    def test_the_region(self, start, length, device) -> None:
        assert read_effects.in_device_region(start, length) is device
