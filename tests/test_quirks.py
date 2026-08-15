"""Quirks-table tests, written as the invariants the table has to hold.

These are the properties that make the difference between a tool that is useful
and one that is dangerous, so they are stated as behaviour rather than as
assertions about particular constants.
"""

from __future__ import annotations

import pytest

from stlink_mcp import quirks


class TestUnknownFamiliesDegradeSafely:
    """An unrecognised part must produce a gap, never a guess.

    A wrong load address is worse than an absent one: the operation appears to
    work and the failure surfaces much later, somewhere unrelated.
    """

    def test_unknown_device_id_gives_the_generic_entry(self) -> None:
        assert quirks.for_device_id(0xDEAD) is quirks.DEFAULT

    def test_absent_device_id_gives_the_generic_entry(self) -> None:
        assert quirks.for_device_id(None) is quirks.DEFAULT

    def test_generic_offers_no_addresses_to_guess_with(self) -> None:
        assert quirks.DEFAULT.ram_load_base is None
        assert quirks.DEFAULT.vector_table_addr is None
        assert quirks.DEFAULT.flash_base is None
        assert quirks.DEFAULT.external_loader is None

    def test_generic_assumes_the_common_access_port(self) -> None:
        assert quirks.DEFAULT.access_port == 0

    def test_generic_does_not_claim_signing_is_needed(self) -> None:
        assert quirks.DEFAULT.requires_signing is False


class TestEveryCharacterisedFamilyIsSelfConsistent:
    """Applied to every entry, so a future family cannot be added half-done."""

    @pytest.mark.parametrize("family", quirks.FAMILIES.values(), ids=lambda f: f.name)
    def test_ram_execution_requires_a_vector_table(self, family: quirks.FamilyQuirks) -> None:
        if family.ram_load_base is not None:
            assert family.vector_table_addr is not None, (
                "A RAM load address without a vector table address cannot be run: "
                "SP and PC are read from the vector table."
            )

    @pytest.mark.parametrize("family", quirks.FAMILIES.values(), ids=lambda f: f.name)
    def test_external_loader_implies_a_flash_base(self, family: quirks.FamilyQuirks) -> None:
        if family.external_loader is not None:
            assert family.flash_base is not None

    @pytest.mark.parametrize("family", quirks.FAMILIES.values(), ids=lambda f: f.name)
    def test_every_family_explains_itself(self, family: quirks.FamilyQuirks) -> None:
        """The notes are the point of the table, not decoration: they are what
        stops the next person rediscovering the quirk the hard way."""
        assert family.notes.strip(), f"{family.name} has no notes"

    @pytest.mark.parametrize("family", quirks.FAMILIES.values(), ids=lambda f: f.name)
    def test_access_port_is_plausible(self, family: quirks.FamilyQuirks) -> None:
        assert 0 <= family.access_port <= 255


class TestSTM32N6:
    """The family this project was built against, so its values are pinned.

    Each of these was verified on a NUCLEO-N657X0-Q, and each one was wrong at
    some point during that work.
    """

    @pytest.fixture
    def n6(self) -> quirks.FamilyQuirks:
        return quirks.for_device_id(0x486)

    def test_debug_is_on_access_port_one(self, n6: quirks.FamilyQuirks) -> None:
        assert n6.access_port == 1

    def test_the_vector_table_is_offset_into_the_ram_image(
        self, n6: quirks.FamilyQuirks
    ) -> None:
        """The 0x400 gap is the signing header the boot ROM prepends."""
        assert n6.vector_table_addr == n6.ram_load_base + 0x400

    def test_flash_boot_requires_signing(self, n6: quirks.FamilyQuirks) -> None:
        assert n6.requires_signing is True
