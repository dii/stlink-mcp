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


class TestProvenanceCannotQuietlyClaimAMeasurement:
    """N6: measured, vendor-db, user-stated and assumed are different states and
    are never presented as the same one.

    The first version of this model failed its own test in the first five
    minutes: `DEFAULT` carries access port 0 and no signing, both assumptions
    about the STM32 range rather than facts about any part, and they were
    reported as `measured`. The lie N6 exists to stop, produced by the mechanism
    built to stop it. Hence the first test here.
    """

    def test_the_generic_entry_does_not_call_its_assumptions_measured(self) -> None:
        summary = quirks.DEFAULT.provenance_summary()
        assert summary["access_port"] == quirks.ASSUMED
        assert not quirks.DEFAULT.is_measured("access_port")

    def test_a_provenance_key_naming_no_field_is_rejected(self) -> None:
        """The silent-failure case, and the reason __post_init__ exists.

        A misspelled key looks harmless. The real field then falls back to
        `default_provenance`, so on a hand-written entry a value nobody measured
        is reported as measured, and nothing anywhere says otherwise.
        """
        with pytest.raises(quirks.ProvenanceError, match="not a field"):
            quirks.FamilyQuirks(
                name="typo", access_port=0, provenance={"ram_load_bases": quirks.MEASURED}
            )

    def test_an_invented_provenance_label_is_rejected(self) -> None:
        with pytest.raises(quirks.ProvenanceError):
            quirks.FamilyQuirks(name="bad", access_port=0, default_provenance="probably")

    def test_a_field_with_no_value_has_no_provenance(self) -> None:
        """An absent fact has no origin, and listing one would suggest the table
        knows something it does not."""
        summary = quirks.DEFAULT.provenance_summary()
        assert quirks.DEFAULT.ram_load_base is None
        assert "ram_load_base" not in summary

    def test_a_seeded_entry_reports_where_its_addresses_came_from(self) -> None:
        seeded = quirks.FamilyQuirks(
            name="STM32F4",
            access_port=0,
            ram_load_base=0x20000000,
            vector_table_addr=0x20000000,
            flash_base=0x08000000,
            notes="seeded from the vendor device database",
            default_provenance=quirks.VENDOR_DB,
            provenance={"access_port": quirks.ASSUMED},
        )
        assert seeded.provenance_of("ram_load_base") == quirks.VENDOR_DB
        assert not seeded.is_measured("ram_load_base")
        assert seeded.provenance_of("access_port") == quirks.ASSUMED
        # The database says nothing about signing, so the entry must not either.
        assert seeded.requires_signing is None
        assert "requires_signing" not in seeded.provenance_summary()

    def test_signing_distinguishes_checked_from_unknown(self) -> None:
        """A bool defaulting to False cannot tell "we checked and it does not"
        from "nobody has said", and the provenance model then reports an origin
        for a claim the entry never made."""
        assert quirks.FamilyQuirks(name="x", access_port=0).requires_signing is None
        assert quirks.for_device_id(0x486).requires_signing is True
        assert quirks.DEFAULT.requires_signing is False


class TestTheContributionRuleIsCheckedNotJustStated:
    """AGENTS.md says to fill in only what you verified on hardware. Until now
    that was prose in three places and nothing enforced it.

    It matters more once seeding exists, because then the table legitimately
    contains unmeasured values and the rule can no longer be "everything here is
    measured" by inspection. This is the line between the two: entries written
    into FAMILIES by hand are measured, and anything else must arrive labelled.
    """

    @pytest.mark.parametrize("family", quirks.FAMILIES.values(), ids=lambda f: f.name)
    def test_every_hand_written_entry_is_fully_measured(
        self, family: quirks.FamilyQuirks
    ) -> None:
        unmeasured = {
            name: origin
            for name, origin in family.provenance_summary().items()
            if origin != quirks.MEASURED
        }
        assert not unmeasured, (
            f"{family.name} is in FAMILIES, which is the hand-written table, but "
            f"carries values that are not measured: {unmeasured}. Either measure "
            "them on a board or let the part fall back to DEFAULT."
        )
