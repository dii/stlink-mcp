"""Seeding from STMicroelectronics' device databases.

The pure resolution logic is tested with no filesystem at all. The parse of the
real databases is tested too, and skipped rather than faked when CubeCLT is not
installed: an invented XML sample would test the parser against a belief about
ST's format, which is the thing most likely to be wrong. That rule is why this
project's parser survived a board whose fields came back blank and another whose
serial came back as uninitialised memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stlink_mcp import quirks
from stlink_mcp.vendordb import (
    VendorDbError,
    parse_hex,
    resolve_sram_base,
    seed_from_directory,
)

DB_DIR = Path("/opt/st/stm32cubeclt_1.22.0/STM32CubeProgrammer/Data_Base")
needs_db = pytest.mark.skipif(not DB_DIR.is_dir(), reason="CubeCLT not installed here")


@pytest.fixture(scope="module")
def result():
    """Parsed once for the whole module: it reads 104 XML files."""
    return seed_from_directory(DB_DIR)


class TestParsingAddressesTheWayStWritesThem:
    """ST's own formatting is inconsistent, and being strict about it already
    cost this project one published wrong number."""

    def test_a_plain_hex_address(self) -> None:
        assert parse_hex("0x20000000") == 0x20000000

    def test_a_trailing_c_suffix(self) -> None:
        """0x45A writes its address as "0x20004000U". A strict int(x, 16)
        rejects it, which is how a survey reported 103 of 104 SRAM regions when
        the answer was 104."""
        assert parse_hex("0x20004000U") == 0x20004000

    def test_bare_hex_with_no_prefix(self) -> None:
        """At least two files write a size this way."""
        assert parse_hex("3C000") == 0x3C000

    def test_nonsense_is_none_rather_than_zero(self) -> None:
        assert parse_hex("banana") is None
        assert parse_hex(None) is None
        assert parse_hex("") is None


class TestChoosingOneSramBase:
    def test_a_single_region_is_used(self) -> None:
        base, why = resolve_sram_base({0x20000000})
        assert (base, why) == (0x20000000, None)

    def test_a_trustzone_alias_pair_resolves_to_the_non_secure_view(self) -> None:
        """Ten of the fourteen ambiguous device IDs are this: the same physical
        memory at 0x2xxxxxxx and 0x3xxxxxxx, exactly 0x10000000 apart."""
        base, why = resolve_sram_base({0x20000000, 0x30000000})
        assert (base, why) == (0x20000000, None)
        base, why = resolve_sram_base({0x20004000, 0x30004000})
        assert (base, why) == (0x20004000, None)

    def test_separate_banks_are_refused_with_the_addresses_named(self) -> None:
        """The other four are a real choice, and picking one would be a guess."""
        base, why = resolve_sram_base({0x20000000, 0x20018000, 0x20030000})
        assert base is None
        assert "0x20000000" in why and "separate banks" in why

    def test_a_pair_that_is_not_an_alias_is_refused(self) -> None:
        """0x42B offers 0x20010000 and 0x30004000: two addresses, and NOT the
        same memory twice, since they are not 0x10000000 apart. The rule has to
        test the offset rather than just the count."""
        base, why = resolve_sram_base({0x20010000, 0x30004000})
        assert base is None

    def test_no_region_at_all_says_so(self) -> None:
        base, why = resolve_sram_base(set())
        assert base is None and "no SRAM region" in why


class TestSeedingIsNeverConfusedWithMeasuring:
    def test_a_seeded_entry_is_vendor_db_with_an_assumed_port(self) -> None:
        entry = quirks.FamilyQuirks(
            name="x", access_port=0, ram_load_base=0x20000000,
            vector_table_addr=0x20000000, flash_base=0x08000000, notes="seeded",
            default_provenance=quirks.VENDOR_DB,
            provenance={"access_port": quirks.ASSUMED},
        )
        assert entry.provenance_of("ram_load_base") == quirks.VENDOR_DB
        assert entry.provenance_of("access_port") == quirks.ASSUMED
        assert not entry.is_measured("flash_base")

    def test_a_missing_database_directory_is_an_error_not_an_empty_seed(self, tmp_path) -> None:
        with pytest.raises(VendorDbError, match="No device databases"):
            seed_from_directory(tmp_path)


class TestPrecedence:
    """A measured entry must outrank a seeded one, and the STM32N6 is the reason
    rather than a principle.

    Its database entry gives SRAM at 0x34180400, which is that family's VECTOR
    TABLE address and not its RAM load base of 0x34180000, because the boot ROM
    prepends a 0x400 signing header. Nothing in the database says which of the
    two it is reporting. Seeding it would put a load address 0x400 too high into
    a field that has already been measured correctly.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        before_v = dict(quirks._VENDOR_BOARDS)
        before_u = dict(quirks._USER_BOARDS)
        yield
        quirks.set_vendor_boards(before_v)
        quirks.set_user_boards(before_u)

    def _seed(self, device_id: int, ram: int) -> None:
        quirks.set_vendor_boards({
            device_id: quirks.FamilyQuirks(
                name="seeded", access_port=0, ram_load_base=ram,
                vector_table_addr=ram, notes="seeded",
                default_provenance=quirks.VENDOR_DB,
            )
        })

    def test_a_measured_family_beats_a_seeded_entry(self) -> None:
        self._seed(0x486, 0x34180400)   # what the database would have given
        entry = quirks.for_device_id(0x486)
        assert entry.ram_load_base == 0x34180000, "the measured value must win"
        assert entry.is_measured("ram_load_base")

    def test_a_seeded_entry_is_used_where_nothing_was_measured(self) -> None:
        self._seed(0x413, 0x20000000)
        entry = quirks.for_device_id(0x413)
        assert entry.ram_load_base == 0x20000000
        assert entry.provenance_of("ram_load_base") == quirks.VENDOR_DB

    def test_a_user_board_beats_a_seeded_entry(self) -> None:
        self._seed(0x413, 0x20000000)
        quirks.set_user_boards({
            0x413: quirks.FamilyQuirks(
                name="mine", access_port=0, ram_load_base=0x10000000,
                vector_table_addr=0x10000000, notes="mine",
                default_provenance=quirks.USER_STATED,
            )
        })
        assert quirks.for_device_id(0x413).ram_load_base == 0x10000000

    def test_an_unseeded_unknown_still_offers_nothing_to_guess_with(self) -> None:
        self._seed(0x413, 0x20000000)
        assert quirks.for_device_id(0xDEAD) is quirks.DEFAULT


@needs_db
class TestAgainstTheRealDatabases:
    """Run against whatever CubeCLT is installed, so a version whose format
    changed shows up here rather than as a wrong address on a board."""

    def test_most_device_ids_are_seedable(self, result) -> None:
        seeded, skipped = result
        assert len(seeded) >= 95, (
            f"only {len(seeded)} device IDs seeded, which is far below the 99 "
            "measured on CubeCLT 1.22.0. Either the format changed or the parser "
            "silently stopped matching."
        )

    def test_the_ambiguous_ones_are_refused_rather_than_guessed(self, result) -> None:
        seeded, skipped = result
        for device_id in (0x42B, 0x42C, 0x472, 0x49A):
            assert device_id in skipped, f"{device_id:#x} should be refused"
            assert device_id not in seeded

    def test_it_agrees_with_what_was_measured_by_hand(self, result) -> None:
        """The strongest check available without a board: the two families that
        WERE measured must come out of the database with the same addresses.

        0x486 is excluded on purpose. It is the one part where the database and
        the measurement disagree, and that disagreement is why measured outranks
        seeded.
        """
        seeded, _ = result
        h7 = seeded[0x450]
        assert h7.ram_load_base == quirks.FAMILIES["STM32H7"].ram_load_base
        assert h7.flash_base == quirks.FAMILIES["STM32H7"].flash_base

    def test_the_n6_is_the_known_disagreement(self, result) -> None:
        """Pins the reason for the precedence rule. If this ever stops being
        true, the rule needs re-deriving rather than trusting."""
        seeded, _ = result
        assert seeded[0x486].ram_load_base == 0x34180400
        assert quirks.FAMILIES["STM32N6"].ram_load_base == 0x34180000

    def test_nothing_seeded_claims_to_be_measured(self, result) -> None:
        seeded, _ = result
        for device_id, entry in seeded.items():
            origins = set(entry.provenance_summary().values())
            assert quirks.MEASURED not in origins, f"{device_id:#x} claims a measurement"

    def test_every_seeded_entry_is_internally_consistent(self, result) -> None:
        seeded, _ = result
        for device_id, entry in seeded.items():
            assert quirks.check_consistency(entry) == [], f"{device_id:#x}"
