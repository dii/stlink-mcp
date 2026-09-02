"""Blind tests for src/stlink_mcp/faults.py, written from contract.md alone.

Author (role B) has not seen faults.py's implementation bodies, only its
stubs (signatures, docstrings, public constants), contract.md's twelve
re-ratified clauses FAULTS-N1..FAULTS-N12 and its reference bit table, and
the two source fixtures the table was built from. Every test below carries
exactly one clause id in its function name, per the naming rule.

Round 2 changes from round 1: FAULTS-N8 was re-worded (distinctness is the
test; no naming form -- bit name, category, or any particular phrase -- is
required in the diagnosis text), and FAULTS-N12 was added (the keys of
FaultStatus.raw are exactly the six CMSIS register names). Every other test
below is unchanged from round 1.
"""
from __future__ import annotations

import itertools

import pytest

from stlink_mcp.faults import (
    AFSR,
    BFAR,
    CFSR,
    DFSR,
    FAULT_REGION_BASE,
    FAULT_REGION_SIZE,
    HFSR,
    MMFAR,
    ActiveBit,
    FaultStatus,
    decode,
)

# ---------------------------------------------------------------------------
# Reference bit table, transcribed from contract.md's "Reference bit table".
# Bit positions are absolute positions within the named 32-bit register.
# ---------------------------------------------------------------------------

MMARVALID_BIT = 7   # CFSR (MMFSR byte) bit 7 -- gates mmfar, not a fault entry (FAULTS-N4)
BFARVALID_BIT = 15  # CFSR (BFSR byte) bit 15 -- gates bfar, not a fault entry (FAULTS-N4)

# (register, bit, canonical name) triples, grouped by the status byte/register
# that defines them -- this grouping is what FAULTS-N9's category property
# is checked against.
MMFSR_BITS = [
    ("CFSR", 0, "IACCVIOL"),
    ("CFSR", 1, "DACCVIOL"),
    ("CFSR", 3, "MUNSTKERR"),
    ("CFSR", 4, "MSTKERR"),
    ("CFSR", 5, "MLSPERR"),
]
BFSR_BITS = [
    ("CFSR", 8, "IBUSERR"),
    ("CFSR", 9, "PRECISERR"),
    ("CFSR", 10, "IMPRECISERR"),
    ("CFSR", 11, "UNSTKERR"),
    ("CFSR", 12, "STKERR"),
    ("CFSR", 13, "LSPERR"),
]
UFSR_BITS = [
    ("CFSR", 16, "UNDEFINSTR"),
    ("CFSR", 17, "INVSTATE"),
    ("CFSR", 18, "INVPC"),
    ("CFSR", 19, "NOCP"),
    ("CFSR", 20, "STKOF"),
    ("CFSR", 24, "UNALIGNED"),
    ("CFSR", 25, "DIVBYZERO"),
]
HFSR_BITS = [
    ("HFSR", 1, "VECTTBL"),
    ("HFSR", 30, "FORCED"),
    ("HFSR", 31, "DEBUGEVT"),
]
DFSR_BITS = [
    (0, "HALTED"),
    (1, "BKPT"),
    (2, "DWTTRAP"),
    (3, "VCATCH"),
    (4, "EXTERNAL"),
]

# Every bit decode() must turn into a FaultStatus.faults entry (FAULTS-N2).
ALL_FAULT_BITS = MMFSR_BITS + BFSR_BITS + UFSR_BITS + HFSR_BITS

# The four status-byte groups FAULTS-N9's category property is about.
GROUPS = {
    "MMFSR": MMFSR_BITS,
    "BFSR": BFSR_BITS,
    "UFSR": UFSR_BITS,
    "HFSR": HFSR_BITS,
}

FAULT_BIT_IDS = [f"{reg}-{bit}-{name}" for reg, bit, name in ALL_FAULT_BITS]
DFSR_BIT_IDS = [f"DFSR-{bit}-{name}" for bit, name in DFSR_BITS]


def make_words(fault_bits=(), dfsr_word=0, mmfar=0, bfar=0, afsr=0, extra_cfsr=0, extra_hfsr=0):
    """Build the dict decode() expects: keyed by the six absolute SCB addresses.

    `fault_bits` is an iterable of (register, bit, name) triples (register is
    "CFSR" or "HFSR"); each contributes one set bit to the relevant word.
    `extra_cfsr`/`extra_hfsr` OR in additional raw bits (e.g. a VALID flag)
    without needing a name.
    """
    cfsr_word = extra_cfsr
    hfsr_word = extra_hfsr
    for reg, bit, _name in fault_bits:
        if reg == "CFSR":
            cfsr_word |= 1 << bit
        elif reg == "HFSR":
            hfsr_word |= 1 << bit
        else:
            raise ValueError(f"unknown register tag {reg!r}")
    return {
        CFSR: cfsr_word,
        HFSR: hfsr_word,
        DFSR: dfsr_word,
        MMFAR: mmfar,
        BFAR: bfar,
        AFSR: afsr,
    }


# ---------------------------------------------------------------------------
# FAULTS-N1 -- the six SCB addresses and the FAULT_REGION_* constants.
# ---------------------------------------------------------------------------


def test_faults_n1_a_scb_addresses_match_documented_offsets():
    assert CFSR == 0xE000ED28, "CFSR must be the documented SCB address 0xE000ED28"
    assert HFSR == 0xE000ED2C, "HFSR must be the documented SCB address 0xE000ED2C"
    assert DFSR == 0xE000ED30, "DFSR must be the documented SCB address 0xE000ED30"
    assert MMFAR == 0xE000ED34, "MMFAR must be the documented SCB address 0xE000ED34"
    assert BFAR == 0xE000ED38, "BFAR must be the documented SCB address 0xE000ED38"
    assert AFSR == 0xE000ED3C, "AFSR must be the documented SCB address 0xE000ED3C"


def test_faults_n1_b_fault_region_base_equals_cfsr():
    assert FAULT_REGION_BASE == CFSR, "FAULT_REGION_BASE must equal CFSR, the first register of the region"


def test_faults_n1_c_fault_region_size_is_24_bytes():
    assert FAULT_REGION_SIZE == 24, "FAULT_REGION_SIZE must be 24: six contiguous 32-bit registers, CFSR through AFSR"


def test_faults_n1_d_region_is_the_six_registers_four_bytes_apart_in_order():
    # Per CALLSITES.md, decode() is called with the dict Probe.read_memory(
    # FAULT_REGION_BASE, FAULT_REGION_SIZE) returns, keyed by
    # FAULT_REGION_BASE, FAULT_REGION_BASE+4, ..., FAULT_REGION_BASE+20.
    expected = [CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR]
    actual = [FAULT_REGION_BASE + 4 * i for i in range(6)]
    assert actual == expected, (
        "the six +4-stepped addresses starting at FAULT_REGION_BASE must be "
        f"CFSR,HFSR,DFSR,MMFAR,BFAR,AFSR in that order; got {actual} vs {expected}"
    )


# ---------------------------------------------------------------------------
# FAULTS-N2 -- every set CFSR(MMFSR+BFSR+UFSR)/HFSR bit becomes one
# FaultStatus.faults entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("register,bit,name", ALL_FAULT_BITS, ids=FAULT_BIT_IDS)
def test_faults_n2_a_single_bit_decodes_to_exactly_one_entry(register, bit, name):
    status = decode(make_words(fault_bits=[(register, bit, name)]))
    got_names = [ab.name for ab in status.faults]
    assert len(status.faults) == 1, (
        f"a word with only {name} (bit {bit} of {register}) set must decode to exactly "
        f"one fault entry, got {got_names}"
    )
    assert got_names[0] == name, f"the single decoded entry for bit {bit} of {register} must be named {name!r}, got {got_names[0]!r}"
    assert status.faults[0].meaning, f"the decoded {name} entry must carry a non-empty meaning"


def test_faults_n2_b_independently_set_bits_each_get_their_own_entry():
    chosen = [
        ("CFSR", 0, "IACCVIOL"),
        ("CFSR", 9, "PRECISERR"),
        ("CFSR", 25, "DIVBYZERO"),
        ("HFSR", 1, "VECTTBL"),
    ]
    status = decode(make_words(fault_bits=chosen))
    decoded_names = sorted(ab.name for ab in status.faults)
    expected = sorted(name for _, _, name in chosen)
    assert decoded_names == expected, (
        f"four independently set bits from different status bytes must decode to four "
        f"independent entries (no collapsing, none missing), expected {expected}, got {decoded_names}"
    )
    assert len(status.faults) == 4, "entry count must equal the number of set bits"


def test_faults_n2_c_every_defined_bit_set_at_once_yields_exactly_those_entries():
    status = decode(make_words(fault_bits=ALL_FAULT_BITS))
    expected = sorted(name for _, _, name in ALL_FAULT_BITS)
    actual = sorted(ab.name for ab in status.faults)
    assert actual == expected, (
        "setting every defined CFSR/HFSR bit at once must decode to exactly those "
        f"entries and no others; expected {expected}, got {actual}"
    )


# ---------------------------------------------------------------------------
# FAULTS-N3 -- DFSR bits decode into FaultStatus.debug; .faults and .debug
# are populated from disjoint registers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bit,name", DFSR_BITS, ids=DFSR_BIT_IDS)
def test_faults_n3_a_single_dfsr_bit_decodes_to_debug_only(bit, name):
    status = decode(make_words(dfsr_word=1 << bit))
    got_names = [ab.name for ab in status.debug]
    assert got_names == [name], f"DFSR bit {bit} ({name}) must decode to exactly one debug entry named {name}, got {got_names}"
    assert status.faults == [], f"DFSR bit {name} must never appear in .faults, only in .debug"
    assert status.debug[0].meaning, f"the decoded {name} debug entry must carry a non-empty meaning"


def test_faults_n3_b_all_dfsr_bits_together_land_only_in_debug():
    dfsr_word = 0
    for bit, _name in DFSR_BITS:
        dfsr_word |= 1 << bit
    status = decode(make_words(dfsr_word=dfsr_word))
    decoded_names = sorted(ab.name for ab in status.debug)
    expected = sorted(name for _bit, name in DFSR_BITS)
    assert decoded_names == expected, f"every set DFSR bit must appear in .debug; expected {expected}, got {decoded_names}"
    assert status.faults == [], "DFSR bits must never leak into .faults"


def test_faults_n3_c_cfsr_and_hfsr_bits_never_land_in_debug():
    status = decode(make_words(fault_bits=[("CFSR", 0, "IACCVIOL"), ("HFSR", 31, "DEBUGEVT")]))
    debug_names = {ab.name for ab in status.debug}
    assert "IACCVIOL" not in debug_names, "a CFSR bit (IACCVIOL) must never appear in .debug"
    assert "DEBUGEVT" not in debug_names, "an HFSR bit (DEBUGEVT) must never appear in .debug"
    assert status.debug == [], f".debug must stay empty when only CFSR/HFSR bits are set, got {[ab.name for ab in status.debug]}"
    fault_names = {ab.name for ab in status.faults}
    assert fault_names == {"IACCVIOL", "DEBUGEVT"}, "both bits must instead appear in .faults"


# ---------------------------------------------------------------------------
# FAULTS-N4 -- mmfar/bfar are gated by MMARVALID/BFARVALID respectively, and
# the two are not swapped.
# ---------------------------------------------------------------------------

_N4_CASES = [
    pytest.param({"valid_bit": MMARVALID_BIT, "other_bit": 4, "other_name": "MSTKERR", "field": "mmfar"}, id="mmfar"),
    pytest.param({"valid_bit": BFARVALID_BIT, "other_bit": 9, "other_name": "PRECISERR", "field": "bfar"}, id="bfar"),
]


@pytest.mark.parametrize("case", _N4_CASES)
def test_faults_n4_a_address_is_none_when_valid_bit_clear(case):
    # The fault bit itself is set, but its VALID bit is clear; both address
    # registers hold nonzero values that must NOT leak through.
    cfsr_word = 1 << case["other_bit"]
    status = decode(make_words(extra_cfsr=cfsr_word, mmfar=0xDEADBEEF, bfar=0xCAFEF00D))
    assert getattr(status, case["field"]) is None, (
        f"{case['field']} must be None when its VALID bit is clear, even though the address "
        f"register holds a nonzero value (this is exactly the 'quiet guess' AGENTS.md N2 forbids)"
    )


@pytest.mark.parametrize("case", _N4_CASES)
def test_faults_n4_b_address_present_when_valid_bit_set(case):
    cfsr_word = (1 << case["valid_bit"]) | (1 << case["other_bit"])
    addr_value = 0x20001000
    other_field = "bfar" if case["field"] == "mmfar" else "mmfar"
    kwargs = {case["field"]: addr_value, other_field: 0x99999999}
    status = decode(make_words(extra_cfsr=cfsr_word, **kwargs))
    assert getattr(status, case["field"]) == addr_value, (
        f"{case['field']} must equal the raw address register word ({addr_value:#x}) when its "
        f"VALID bit is set, regardless of which fault bit ({case['other_name']}) is also set; "
        f"got {getattr(status, case['field'])!r}"
    )


@pytest.mark.parametrize("case", _N4_CASES)
def test_faults_n4_c_valid_bits_are_not_swapped(case):
    # Set ONLY this case's VALID bit; both address registers hold distinct
    # nonzero values. Only the matching field should surface.
    cfsr_word = 1 << case["valid_bit"]
    status = decode(make_words(extra_cfsr=cfsr_word, mmfar=0x30003000, bfar=0x40004000))
    expected = 0x30003000 if case["field"] == "mmfar" else 0x40004000
    other_field = "bfar" if case["field"] == "mmfar" else "mmfar"
    assert getattr(status, case["field"]) == expected, (
        f"setting only bit {case['valid_bit']} ({case['field']}'s VALID flag) must surface "
        f"{case['field']}, got {getattr(status, case['field'])!r}"
    )
    assert getattr(status, other_field) is None, (
        f"bit {case['valid_bit']} must gate only {case['field']}, not {other_field} "
        f"(MMARVALID/BFARVALID must not be swapped); {other_field} was "
        f"{getattr(status, other_field)!r} instead of None"
    )


# ---------------------------------------------------------------------------
# FAULTS-N5 -- afsr is always the raw AFSR word, never decoded, never gated.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("afsr_value", [0, 1, 0x12345678, 0xFFFFFFFF])
def test_faults_n5_a_afsr_is_always_the_raw_register_word(afsr_value):
    status = decode(make_words(afsr=afsr_value))
    assert status.afsr == afsr_value, f"afsr must always carry the raw AFSR word as a plain integer; expected {afsr_value:#x}, got {status.afsr!r}"
    assert isinstance(status.afsr, int), "afsr must be a plain integer, never decoded into ActiveBit entries"


def test_faults_n5_b_afsr_unaffected_by_bits_or_valid_flags_elsewhere():
    cfsr_word = (1 << MMARVALID_BIT) | (1 << BFARVALID_BIT) | (1 << 0) | (1 << 8)
    hfsr_word = 1 << 31
    dfsr_word = 1 << 0
    words = {CFSR: cfsr_word, HFSR: hfsr_word, DFSR: dfsr_word, MMFAR: 1, BFAR: 2, AFSR: 0xABCD1234}
    status = decode(words)
    assert status.afsr == 0xABCD1234, (
        "afsr must equal the raw AFSR word regardless of any validity flag or bits set "
        f"elsewhere in CFSR/HFSR/DFSR; got {status.afsr!r}"
    )


def test_faults_n5_c_afsr_never_none_even_on_clean_read():
    status = decode(make_words())
    assert status.afsr is not None, "afsr must never be None; no source describes a validity gate for it"
    assert status.afsr == 0, "afsr must be 0 on an all-zero read, not None"


# ---------------------------------------------------------------------------
# FAULTS-N6 -- STKOF (CFSR/UFSR bit 20) is a defined UsageFault bit.
# ---------------------------------------------------------------------------


def test_faults_n6_a_stkof_present_when_set_alone():
    status = decode(make_words(fault_bits=[("CFSR", 20, "STKOF")]))
    decoded_names = [ab.name for ab in status.faults]
    assert decoded_names == ["STKOF"], f"CFSR/UFSR bit 20 (STKOF) must decode to its own entry, got {decoded_names}"


def test_faults_n6_b_stkof_present_alongside_other_usagefault_bits():
    status = decode(
        make_words(
            fault_bits=[
                ("CFSR", 16, "UNDEFINSTR"),
                ("CFSR", 20, "STKOF"),
                ("CFSR", 25, "DIVBYZERO"),
            ]
        )
    )
    decoded_names = {ab.name for ab in status.faults}
    assert "STKOF" in decoded_names, "STKOF must not be treated as a reserved/undefined bit when other UsageFault bits are also set"
    assert decoded_names == {"UNDEFINSTR", "STKOF", "DIVBYZERO"}, f"expected exactly the three set UsageFault bits, got {decoded_names}"


# ---------------------------------------------------------------------------
# FAULTS-N7 -- any_fault is True exactly when .faults is non-empty, and is
# unaffected by .debug.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("register,bit,name", ALL_FAULT_BITS, ids=FAULT_BIT_IDS)
def test_faults_n7_a_any_fault_true_for_every_single_fault_bit(register, bit, name):
    status = decode(make_words(fault_bits=[(register, bit, name)]))
    assert status.any_fault is True, f"any_fault must be True when {name} (bit {bit} of {register}) is the only set bit"


def test_faults_n7_b_any_fault_false_on_clean_read():
    status = decode(make_words())
    assert status.faults == []
    assert status.any_fault is False, "any_fault must be False on an all-zero (clean) read"


def test_faults_n7_c_any_fault_false_when_only_debug_bits_are_set():
    dfsr_word = 0
    for bit, _name in DFSR_BITS:
        dfsr_word |= 1 << bit
    status = decode(make_words(dfsr_word=dfsr_word))
    assert status.debug, "sanity check: .debug must be non-empty for this scenario"
    assert status.faults == []
    assert status.any_fault is False, (
        "a target halted purely for debug reasons (DFSR bits set, no CFSR/HFSR bits) must "
        "report any_fault == False"
    )


def test_faults_n7_d_any_fault_unaffected_by_simultaneous_debug_bits():
    dfsr_word = 0
    for bit, _name in DFSR_BITS:
        dfsr_word |= 1 << bit
    status = decode(make_words(fault_bits=[("CFSR", 0, "IACCVIOL")], dfsr_word=dfsr_word))
    assert status.any_fault is True, "any_fault must be True whenever .faults is non-empty, even with .debug also populated"


# ---------------------------------------------------------------------------
# FAULTS-N8 (re-worded round 2) -- diagnosis is always a non-empty string,
# is specific to the active bits (checked by distinctness, not by any
# particular naming form), and includes a known mmfar/bfar address.
# ---------------------------------------------------------------------------


def test_faults_n8_a_diagnosis_nonempty_on_clean_read():
    status = decode(make_words())
    assert isinstance(status.diagnosis, str), "diagnosis must be a string"
    assert status.diagnosis.strip() != "", "diagnosis must be non-empty even on a clean read (any_fault == False)"


def test_faults_n8_b_all_single_bit_diagnoses_are_mutually_distinct():
    # Every single-bit fault state (all 21 defined CFSR/HFSR bits) plus the
    # clean read must each produce distinct diagnosis text. This is the
    # general form of FAULTS-N8's distinctness requirement (violation b):
    # no naming form is required, only that the text actually differs.
    seen: dict[str, str] = {}
    for register, bit, name in ALL_FAULT_BITS:
        status = decode(make_words(fault_bits=[(register, bit, name)]))
        text = status.diagnosis
        if text in seen:
            raise AssertionError(
                f"diagnosis for {name} is identical to the diagnosis already produced for "
                f"{seen[text]} ({text!r}); every single-bit fault state must produce mutually "
                "distinct diagnosis text, regardless of naming form"
            )
        seen[text] = name
    clean_text = decode(make_words()).diagnosis
    assert clean_text not in seen, (
        f"the clean-read diagnosis ({clean_text!r}) must not be identical to any single-bit "
        f"fault state's diagnosis (matched {seen.get(clean_text)!r})"
    )


def test_faults_n8_f_two_bit_diagnosis_differs_from_each_single_bit_member():
    # For every pair of defined fault bits, the two-bit state's diagnosis
    # must differ from each member's own single-bit diagnosis (violation d):
    # a multi-bit state must not read as if only the first fault were
    # reported. No naming form is assumed, only that the text differs.
    for (reg1, bit1, name1), (reg2, bit2, name2) in itertools.combinations(ALL_FAULT_BITS, 2):
        pair_text = decode(make_words(fault_bits=[(reg1, bit1, name1), (reg2, bit2, name2)])).diagnosis
        single1_text = decode(make_words(fault_bits=[(reg1, bit1, name1)])).diagnosis
        single2_text = decode(make_words(fault_bits=[(reg2, bit2, name2)])).diagnosis
        assert pair_text != single1_text, (
            f"{name1}+{name2} together must produce a diagnosis different from {name1} alone "
            f"({single1_text!r}); a multi-bit state must not read as only its first member"
        )
        assert pair_text != single2_text, (
            f"{name1}+{name2} together must produce a diagnosis different from {name2} alone "
            f"({single2_text!r}); a multi-bit state must not read as only its first member"
        )


def test_faults_n8_c_diagnosis_differs_across_distinct_fault_states():
    sample_bits = [
        ("CFSR", 0, "IACCVIOL"),
        ("CFSR", 9, "PRECISERR"),
        ("CFSR", 25, "DIVBYZERO"),
        ("HFSR", 30, "FORCED"),
    ]
    diagnoses = [decode(make_words(fault_bits=[spec])).diagnosis for spec in sample_bits]
    clean = decode(make_words()).diagnosis
    all_texts = diagnoses + [clean]
    assert len(set(all_texts)) == len(all_texts), (
        "diagnosis must not be a hardcoded placeholder: distinct fault states (and the clean "
        f"read) produced identical text among {all_texts}"
    )


def test_faults_n8_d_known_mmfar_address_appears_in_diagnosis():
    addr = 0x2000ABCD
    cfsr_word = (1 << MMARVALID_BIT) | (1 << 4)  # MSTKERR, with MMARVALID set
    status = decode(make_words(extra_cfsr=cfsr_word, mmfar=addr))
    text = status.diagnosis.lower()
    hex_form = format(addr, "x")
    dec_form = str(addr)
    assert hex_form in text or dec_form in text, (
        f"diagnosis must include the known mmfar address ({addr:#x} / {addr}) somewhere in its "
        f"text (hex or decimal, format is not pinned); got {status.diagnosis!r}"
    )


def test_faults_n8_e_known_bfar_address_appears_in_diagnosis():
    addr = 0x300BEEF0
    cfsr_word = (1 << BFARVALID_BIT) | (1 << 9)  # PRECISERR, with BFARVALID set
    status = decode(make_words(extra_cfsr=cfsr_word, bfar=addr))
    text = status.diagnosis.lower()
    hex_form = format(addr, "x")
    dec_form = str(addr)
    assert hex_form in text or dec_form in text, (
        f"diagnosis must include the known bfar address ({addr:#x} / {addr}) somewhere in its "
        f"text (hex or decimal, format is not pinned); got {status.diagnosis!r}"
    )


# ---------------------------------------------------------------------------
# FAULTS-N9 -- ActiveBit.category groups bits by status byte; DEBUGEVT
# groups with the HardFault/HFSR family.
# ---------------------------------------------------------------------------


def test_faults_n9_a_bits_in_same_status_byte_share_category():
    for group_name, entries in GROUPS.items():
        status = decode(make_words(fault_bits=entries))
        names_in_group = {name for _reg, _bit, name in entries}
        cats = {ab.category for ab in status.faults if ab.name in names_in_group}
        assert len(cats) == 1, f"all bits within {group_name} must share one category value, got {cats}"


def test_faults_n9_b_bits_from_different_status_bytes_get_different_category():
    for g1, g2 in itertools.combinations(GROUPS, 2):
        for reg1, bit1, name1 in GROUPS[g1]:
            for reg2, bit2, name2 in GROUPS[g2]:
                status = decode(make_words(fault_bits=[(reg1, bit1, name1), (reg2, bit2, name2)]))
                cat_map = {ab.name: ab.category for ab in status.faults}
                assert cat_map[name1] != cat_map[name2], (
                    f"{name1} ({g1}) and {name2} ({g2}) come from different status bytes and must "
                    f"get different category values, both were {cat_map[name1]!r}"
                )


def test_faults_n9_c_debugevt_categorized_within_hardfault_family():
    status = decode(make_words(fault_bits=[("HFSR", 31, "DEBUGEVT"), ("HFSR", 1, "VECTTBL"), ("HFSR", 30, "FORCED")]))
    cat_map = {ab.name: ab.category for ab in status.faults}
    assert cat_map["DEBUGEVT"] == cat_map["VECTTBL"] == cat_map["FORCED"], (
        "DEBUGEVT must share the HardFault/HFSR category with VECTTBL and FORCED, despite its "
        f"name suggesting a debug concern; got {cat_map}"
    )


# ---------------------------------------------------------------------------
# FAULTS-N10 -- decode() never raises on a missing address key; a missing
# key defaults to register value 0.
# ---------------------------------------------------------------------------


def test_faults_n10_a_empty_dict_does_not_raise():
    status = decode({})
    assert status.faults == [], "an empty input dict must decode as no fault bits set"
    assert status.debug == [], "an empty input dict must decode as no debug bits set"
    assert status.mmfar is None, "an empty input dict means MMARVALID reads as clear, so mmfar must be None"
    assert status.bfar is None, "an empty input dict means BFARVALID reads as clear, so bfar must be None"
    assert status.afsr == 0, "an empty input dict must default afsr to 0"


def test_faults_n10_b_missing_keys_default_to_zero_not_error():
    # Only CFSR is present; HFSR/DFSR/MMFAR/BFAR/AFSR keys are entirely
    # absent from the dict (not merely zero-valued).
    cfsr_word = (1 << MMARVALID_BIT) | (1 << 4)  # MSTKERR, with MMARVALID set
    status = decode({CFSR: cfsr_word})
    decoded_names = [ab.name for ab in status.faults]
    assert decoded_names == ["MSTKERR"], f"a missing HFSR key must default to 0 (no HFSR bits set), got faults={decoded_names}"
    assert status.debug == [], "a missing DFSR key must default to 0 (no debug bits set)"
    assert status.mmfar == 0, (
        "MMARVALID is set but the MMFAR key is missing from the input dict: the defaulted "
        f"register value (0) must surface, not None and not a raised error; got {status.mmfar!r}"
    )
    assert status.bfar is None, "BFARVALID is clear, so bfar must be None regardless of the missing BFAR key"
    assert status.afsr == 0, "a missing AFSR key must default to register value 0"


# ---------------------------------------------------------------------------
# FAULTS-N11 -- raw carries one entry per register, matching the values used
# elsewhere, all within unsigned 32-bit range.
# ---------------------------------------------------------------------------


def test_faults_n11_a_raw_has_one_entry_per_register():
    words = make_words(fault_bits=[("CFSR", 0, "IACCVIOL")], dfsr_word=1, mmfar=1, bfar=2, afsr=3)
    status = decode(words)
    assert len(status.raw) == 6, (
        "raw must have exactly one entry per register (CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR: 6 "
        f"total), got {len(status.raw)}"
    )


def test_faults_n11_b_raw_values_match_the_registers_actually_used():
    cfsr_word = 1 << 4  # MSTKERR alone
    hfsr_word = 1 << 1  # VECTTBL alone
    dfsr_word = 1 << 2  # DWTTRAP alone
    mmfar_word = 0x1000
    bfar_word = 0x2000
    afsr_word = 0x3000
    words = {CFSR: cfsr_word, HFSR: hfsr_word, DFSR: dfsr_word, MMFAR: mmfar_word, BFAR: bfar_word, AFSR: afsr_word}
    status = decode(words)
    expected = sorted([cfsr_word, hfsr_word, dfsr_word, mmfar_word, bfar_word, afsr_word])
    actual = sorted(status.raw.values())
    assert actual == expected, (
        "raw's six values must be exactly the six register words decode() actually used to "
        f"compute faults/debug/mmfar/bfar/afsr; expected {expected}, got {actual}"
    )


def test_faults_n11_c_raw_mmfar_and_bfar_entries_are_unaffected_by_gating():
    # MMARVALID and BFARVALID both clear, so status.mmfar/status.bfar surface
    # as None -- but raw must still carry the literal register words.
    mmfar_word = 0x11112222
    bfar_word = 0x33334444
    status = decode(make_words(mmfar=mmfar_word, bfar=bfar_word))
    assert status.mmfar is None and status.bfar is None, "sanity check: both VALID bits are clear here"
    assert mmfar_word in status.raw.values(), (
        f"raw must still carry the raw MMFAR register word ({mmfar_word:#x}) even when MMARVALID "
        f"is clear and status.mmfar is None; raw values were {list(status.raw.values())}"
    )
    assert bfar_word in status.raw.values(), (
        f"raw must still carry the raw BFAR register word ({bfar_word:#x}) even when BFARVALID "
        f"is clear and status.bfar is None; raw values were {list(status.raw.values())}"
    )


def test_faults_n11_d_raw_values_fit_unsigned_32_bit_range():
    words = {CFSR: 0xFFFFFFFF, HFSR: 0xFFFFFFFF, DFSR: 0xFFFFFFFF, MMFAR: 0xFFFFFFFF, BFAR: 0xFFFFFFFF, AFSR: 0xFFFFFFFF}
    status = decode(words)
    for reg_value in status.raw.values():
        assert 0 <= reg_value <= 0xFFFFFFFF, f"every raw value must fit an unsigned 32-bit range (0..0xFFFFFFFF), got {reg_value}"
        formatted = f"0x{reg_value:08X}"
        assert "-" not in formatted, f"raw value {reg_value} must format as unsigned 32-bit hex (CALLSITES.md's 0x{{val:08X}}) without a sign, got {formatted}"


# ---------------------------------------------------------------------------
# FAULTS-N12 (new in round 2) -- FaultStatus.raw's keys are exactly the six
# CMSIS register names "CFSR", "HFSR", "DFSR", "MMFAR", "BFAR", "AFSR".
# ---------------------------------------------------------------------------


def test_faults_n12_a_raw_has_no_key_outside_the_six_cmsis_names():
    words = make_words(fault_bits=[("CFSR", 0, "IACCVIOL"), ("HFSR", 1, "VECTTBL")], dfsr_word=1, mmfar=1, bfar=2, afsr=3)
    status = decode(words)
    allowed = {"CFSR", "HFSR", "DFSR", "MMFAR", "BFAR", "AFSR"}
    unexpected = set(status.raw) - allowed
    assert not unexpected, (
        f"raw must not contain any key outside the six CMSIS register names {sorted(allowed)}; "
        f"found unexpected key(s) {sorted(unexpected)}"
    )


def test_faults_n12_b_raw_key_set_is_exactly_the_six_cmsis_names():
    status = decode(make_words())
    expected = {"CFSR", "HFSR", "DFSR", "MMFAR", "BFAR", "AFSR"}
    assert set(status.raw) == expected, (
        f"raw's key set must be exactly {sorted(expected)} (no fewer, no more, no different "
        f"spelling), got {sorted(status.raw)}"
    )


# ---------------------------------------------------------------------------
# Round 3 additions -- closing audit-list-1's three gaps.
#
# FAULTS-N5 (violation a): a test with a nonzero AFSR word that inspects
# .faults/.debug/.any_fault, not just .afsr, so that decoding AFSR bits into
# named entries turns the test red. Every round-2 AFSR test above (n5_a/b/c)
# only asserts on status.afsr itself and never looks at .faults/.debug.
#
# FAULTS-N11 (violation b, two instances from the audit): a test that
# compares raw BY KEY against the exact words passed in. Round-2's n11_b
# compares sorted(status.raw.values()) against a sorted expected list, and
# n11_c only checks multiset membership (`... in status.raw.values()`);
# neither can see a value truncated at the source key, or two keys whose
# values are swapped with each other, because both defects still leave the
# same six words present somewhere in the value multiset.
# ---------------------------------------------------------------------------


_N5_AFSR_WORDS_MANY_BITS = [
    pytest.param((1 << 1) | (1 << 30) | (1 << 31), id="bits-1-30-31"),
    pytest.param(0xFFFFFFFF, id="all-ones"),
    pytest.param(
        (1 << 0) | (1 << 1) | (1 << 8) | (1 << 9) | (1 << 16) | (1 << 17) | (1 << 24) | (1 << 25) | (1 << 30) | (1 << 31),
        id="many-bits-mirroring-cfsr-hfsr-positions",
    ),
]


@pytest.mark.parametrize("afsr_value", _N5_AFSR_WORDS_MANY_BITS)
def test_faults_n5_d_no_afsr_bits_decoded_into_faults_or_debug(afsr_value):
    # Every other register is zero (make_words()'s defaults for cfsr/hfsr/
    # dfsr/mmfar/bfar are all 0); only AFSR carries a nonzero, many-bit word,
    # including bits 1, 30 and 31 (which coincide with HFSR's VECTTBL/
    # FORCED/DEBUGEVT bit positions) and an all-ones word. No source defines
    # AFSR's bits, so none of them may turn into a named entry anywhere.
    status = decode(make_words(afsr=afsr_value))
    assert status.faults == [], (
        "AFSR is implementation-defined content with no source-stated bit meanings, so its bits "
        f"must never be decoded into .faults; with afsr={afsr_value:#x} and every other register "
        f"zero, expected an empty .faults, got {[ab.name for ab in status.faults]}"
    )
    assert status.debug == [], (
        f"AFSR bits must never be decoded into .debug either; with afsr={afsr_value:#x} and every "
        f"other register zero, expected an empty .debug, got {[ab.name for ab in status.debug]}"
    )
    assert status.any_fault is False, (
        f"with CFSR/HFSR/DFSR all zero and only afsr={afsr_value:#x} nonzero, any_fault must be "
        "False: AFSR bits are never treated as fault bits"
    )


def test_faults_n11_e_raw_values_match_by_key_not_by_membership():
    # A CFSR word with bits set only above bit 7 (PRECISERR=9, INVSTATE=17,
    # DIVBYZERO=25): raw["CFSR"] truncated to the low byte (mmfsr) would read
    # as 0 here, not this word. Distinct, nonzero MMFAR/BFAR words catch a
    # swap between those two keys; distinct HFSR/DFSR/AFSR words rule out any
    # other cross-assignment. Every comparison below is BY KEY, never by
    # multiset membership.
    cfsr_word = (1 << 9) | (1 << 17) | (1 << 25)
    hfsr_word = 1 << 30
    dfsr_word = 1 << 3
    mmfar_word = 0x50005000
    bfar_word = 0x60006000
    afsr_word = 0x70007000
    words = {
        CFSR: cfsr_word,
        HFSR: hfsr_word,
        DFSR: dfsr_word,
        MMFAR: mmfar_word,
        BFAR: bfar_word,
        AFSR: afsr_word,
    }
    status = decode(words)
    assert status.raw["CFSR"] == cfsr_word, (
        f"raw['CFSR'] must equal the full CFSR word {cfsr_word:#x} (bits 9, 17 and 25, all above "
        f"the low byte); a value truncated to the low byte (mmfsr) would read as 0 here, got "
        f"{status.raw['CFSR']!r}"
    )
    assert status.raw["HFSR"] == hfsr_word, f"raw['HFSR'] must equal the raw HFSR word {hfsr_word:#x}, got {status.raw['HFSR']!r}"
    assert status.raw["DFSR"] == dfsr_word, f"raw['DFSR'] must equal the raw DFSR word {dfsr_word:#x}, got {status.raw['DFSR']!r}"
    assert status.raw["MMFAR"] == mmfar_word, (
        f"raw['MMFAR'] must equal the raw MMFAR word {mmfar_word:#x}, not BFAR's word "
        f"({bfar_word:#x}); got {status.raw['MMFAR']!r}"
    )
    assert status.raw["BFAR"] == bfar_word, (
        f"raw['BFAR'] must equal the raw BFAR word {bfar_word:#x}, not MMFAR's word "
        f"({mmfar_word:#x}); got {status.raw['BFAR']!r}"
    )
    assert status.raw["AFSR"] == afsr_word, f"raw['AFSR'] must equal the raw AFSR word {afsr_word:#x}, got {status.raw['AFSR']!r}"
