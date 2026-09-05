"""Blind tests for FAULTS-N13 (src/stlink_mcp/faults.py), written from contract.md alone.

Author (role B) has not seen faults.py's implementation bodies, only its stubs (signatures,
docstrings, public constants), contract.md's FAULTS-N13 clause (the last clause in the contract,
promoted from a behaviour ruling), and the reference bit table used to build test words. This file
tests ONLY FAULTS-N13; it does not re-test any other clause.

FAULTS-N13: decode(words) does not modify the dict it is given and carries no state between
calls: after the call the input dict has the same keys and values as before, and two calls with
equal inputs return equal results whatever was decoded in between.

violations tested:
  (a) the input dict gains a key or has a value changed by the call
  (b) a result depends on an earlier call's input
"""
from __future__ import annotations

import copy

import pytest

from stlink_mcp.faults import (
    AFSR,
    BFAR,
    CFSR,
    DFSR,
    HFSR,
    MMFAR,
    decode,
)


def _result_signature(status):
    """Normalize a FaultStatus into a fully comparable, order-independent tuple.

    contract.md's general note: no source specifies the order of entries within
    FaultStatus.faults or FaultStatus.debug, so those two are compared as sorted tuples of
    (name, category, meaning) rather than as ordered lists. Every other observable field
    (mmfar, bfar, afsr, raw, any_fault, diagnosis) is compared directly.
    """

    def norm(bits):
        return tuple(sorted((ab.name, ab.category, ab.meaning) for ab in bits))

    return (
        norm(status.faults),
        norm(status.debug),
        status.mmfar,
        status.bfar,
        status.afsr,
        tuple(sorted(status.raw.items())),
        status.any_fault,
        status.diagnosis,
    )


def _expected_raw(words):
    """The FaultStatus.raw dict decode() must produce for a given input, computed directly from
    the contract rather than from any other decode() call: FAULTS-N12 fixes the six raw keys to
    exactly "CFSR", "HFSR", "DFSR", "MMFAR", "BFAR", "AFSR"; FAULTS-N11 says each value is the
    same register word decode() actually used; FAULTS-N10 says a missing address defaults to 0.
    This is an independent oracle built from the words a test itself handed to decode(), so a
    defect that answers from stale state (an earlier call's cached result) cannot satisfy it by
    coincidence the way it can satisfy an equality check against another decode() call.
    """
    return {
        "CFSR": words.get(CFSR, 0),
        "HFSR": words.get(HFSR, 0),
        "DFSR": words.get(DFSR, 0),
        "MMFAR": words.get(MMFAR, 0),
        "BFAR": words.get(BFAR, 0),
        "AFSR": words.get(AFSR, 0),
    }


# ---------------------------------------------------------------------------
# Input dicts used across both violations. Each is built fresh by its own
# function so no test can share (and accidentally mutate) another test's
# dict, and each test builds its own inputs rather than relying on
# module-level shared state.
# ---------------------------------------------------------------------------


def _all_six_many_bits():
    """All six addresses present; several bits set in each relevant word."""
    cfsr_word = (
        (1 << 0)  # IACCVIOL
        | (1 << 7)  # MMARVALID
        | (1 << 9)  # PRECISERR
        | (1 << 15)  # BFARVALID
        | (1 << 20)  # STKOF
        | (1 << 25)  # DIVBYZERO
    )
    hfsr_word = (1 << 1) | (1 << 30) | (1 << 31)  # VECTTBL, FORCED, DEBUGEVT
    dfsr_word = (1 << 0) | (1 << 1) | (1 << 4)  # HALTED, BKPT, EXTERNAL
    return {
        CFSR: cfsr_word,
        HFSR: hfsr_word,
        DFSR: dfsr_word,
        MMFAR: 0x2000ABCD,
        BFAR: 0x300BEEF0,
        AFSR: 0xFFFFFFFF,
    }


def _all_six_clean():
    """All six addresses present, all zero (a clean read)."""
    return {CFSR: 0, HFSR: 0, DFSR: 0, MMFAR: 0, BFAR: 0, AFSR: 0}


def _subset_missing_most():
    """Only CFSR and MMFAR present; HFSR, DFSR, BFAR, AFSR are entirely absent."""
    return {CFSR: (1 << 7) | (1 << 4), MMFAR: 0x11112222}  # MMARVALID + MSTKERR


def _subset_missing_all_but_one():
    """Only AFSR present; every other address is entirely absent."""
    return {AFSR: 0x12345678}


def _subset_missing_debug_and_addresses():
    """CFSR and HFSR present with bits set; DFSR/MMFAR/BFAR/AFSR entirely absent."""
    return {CFSR: (1 << 8) | (1 << 17), HFSR: 1 << 31}  # IBUSERR, INVSTATE, DEBUGEVT


def _empty_dict():
    """No addresses at all."""
    return {}


ALL_INPUT_BUILDERS = [
    pytest.param(_all_six_many_bits, id="all-six-many-bits"),
    pytest.param(_all_six_clean, id="all-six-clean"),
    pytest.param(_subset_missing_most, id="subset-missing-most"),
    pytest.param(_subset_missing_all_but_one, id="subset-missing-all-but-one"),
    pytest.param(_subset_missing_debug_and_addresses, id="subset-missing-debug-and-addresses"),
    pytest.param(_empty_dict, id="empty-dict"),
]


# ---------------------------------------------------------------------------
# FAULTS-N13 violation (a): the input dict gains a key or has a value changed
# by the call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build_words", ALL_INPUT_BUILDERS)
def test_faults_n13_a_input_dict_unchanged_after_decode(build_words):
    words = build_words()
    before = copy.deepcopy(words)
    decode(words)
    assert words == before, (
        f"decode() must not modify the dict it is given; before={before!r}, after={words!r}"
    )


@pytest.mark.parametrize("build_words", ALL_INPUT_BUILDERS)
def test_faults_n13_a_input_dict_key_set_unchanged_after_decode(build_words):
    # Separate from the value-equality test above: a missing address (per FAULTS-N10) defaults
    # to 0 in the RESULT, but that default must never be written back into the caller's dict. A
    # dict that started with a key missing must still have it missing afterward, not merely
    # zero-valued.
    words = build_words()
    keys_before = set(words.keys())
    decode(words)
    assert set(words.keys()) == keys_before, (
        f"decode() must not insert or remove keys in the input dict, even to backfill a "
        f"FAULTS-N10 default of 0 for a missing address; keys before={sorted(keys_before)}, "
        f"after={sorted(words.keys())}"
    )


def test_faults_n13_a_input_dict_survives_a_second_unrelated_call_in_between():
    # The dict under test is decoded, then a large, unrelated call happens, then the ORIGINAL
    # dict object is inspected again (not re-decoded): it must still read exactly as it did
    # originally.
    words = _subset_missing_most()
    before = copy.deepcopy(words)
    decode(words)
    decode(_all_six_many_bits())
    assert words == before, (
        f"the input dict from an earlier call must remain untouched even after a later, "
        f"unrelated call to decode(); before={before!r}, after={words!r}"
    )


def test_faults_n13_a_values_with_many_bits_set_are_not_altered():
    # Values that already have several bits set (rather than a single tidy bit) are the case
    # most likely to reveal an in-place |= or similar accidental mutation of the caller's word.
    words = {
        CFSR: 0xFFFFFFFF,
        HFSR: 0xFFFFFFFF,
        DFSR: 0xFFFFFFFF,
        MMFAR: 0xFFFFFFFF,
        BFAR: 0xFFFFFFFF,
        AFSR: 0xFFFFFFFF,
    }
    before = dict(words)
    decode(words)
    assert words == before, (
        f"decode() must not alter register words that already have many bits set; "
        f"before={before!r}, after={words!r}"
    )


# ---------------------------------------------------------------------------
# FAULTS-N13 violation (b): a result depends on an earlier call's input.
# ---------------------------------------------------------------------------


def _distinctive_throwaway():
    """A deliberately unusual, self-contained input used only to PRIME state at the start of
    every (b) test, before that test's own X, Y sequence runs. Its purpose is isolation: whatever
    ran immediately before this test (another parametrized case in this file, run in whatever
    order the test runner chose) must not be the reason this test passes or fails. None of its
    six words is reused as a "fixed" value by any pair below, so priming with it can never itself
    manufacture a false match against a test's real X or Y.
    """
    return {
        CFSR: 0x5A5A0001,
        HFSR: 0x5A5A0002,
        DFSR: 0x5A5A0003,
        MMFAR: 0x5A5A0004,
        BFAR: 0x5A5A0005,
        AFSR: 0x5A5A0006,
    }


def _distinct_pairs():
    """A handful of (X, Y) input pairs, used to check that no state leaks from one
    decode() call into the next."""
    return [
        pytest.param(_all_six_many_bits(), _all_six_clean(), id="many-bits-then-clean"),
        pytest.param(_subset_missing_most(), _subset_missing_all_but_one(), id="subset-then-subset"),
        pytest.param(_empty_dict(), _all_six_many_bits(), id="empty-then-many-bits"),
        pytest.param(_subset_missing_debug_and_addresses(), _all_six_clean(), id="subset-debug-then-clean"),
    ]


@pytest.mark.parametrize("x_words,y_words", _distinct_pairs())
def test_faults_n13_b_repeating_the_first_call_after_a_different_call_matches(x_words, y_words):
    # Prime a neutral state of this test's own first, so leftover state from whatever test ran
    # immediately before it cannot be why this test passes or fails.
    decode(_distinctive_throwaway())
    # decode(X), then decode(Y), then decode(X) again: the two X results must be identical in
    # every observable field, regardless of what Y was. Capture Y's own result too, so a wrong
    # answer for Y is visible here rather than only checking X.
    x1 = decode(copy.deepcopy(x_words))
    y_result = decode(copy.deepcopy(y_words))
    x2 = decode(copy.deepcopy(x_words))
    # A further reference for X, obtained after a fresh re-prime and after the whole X,Y,X
    # sequence above has already run. Comparing x1 to x2 alone can be satisfied by a
    # state-leaking decode() that simply always returns "the last real result computed for this
    # input" — x1 and x2 would then agree with each other but both still be wrong. Re-priming and
    # decoding X again forces an independent computation to compare against.
    decode(_distinctive_throwaway())
    x_fresh = decode(copy.deepcopy(x_words))
    assert _result_signature(x1) == _result_signature(x2), (
        f"two decode() calls on equal inputs (X) must return equal results whatever was "
        f"decoded in between; first={_result_signature(x1)!r}, second={_result_signature(x2)!r}"
    )
    assert _result_signature(x2) == _result_signature(x_fresh), (
        "decode(X) run right after decode(Y) must match an independently re-primed decode(X); "
        f"after-Y={_result_signature(x2)!r}, fresh={_result_signature(x_fresh)!r}"
    )
    # Direct oracle checks against the contract, independent of any other decode() call.
    assert x1.raw == _expected_raw(x_words), (
        "the first decode(X) must report raw reflecting X's OWN words per FAULTS-N11/N10/N12; "
        f"got={x1.raw!r}, expected={_expected_raw(x_words)!r}"
    )
    assert x2.raw == _expected_raw(x_words), (
        "decode(X), run again after decode(Y), must report raw reflecting X's OWN words per "
        f"FAULTS-N11/N10/N12, not a value carried over from Y's call; "
        f"got={x2.raw!r}, expected={_expected_raw(x_words)!r}"
    )
    assert y_result.raw == _expected_raw(y_words), (
        "decode(Y) must report raw reflecting Y's OWN words per FAULTS-N11/N10/N12, not a value "
        f"inherited from X's earlier call; got={y_result.raw!r}, expected={_expected_raw(y_words)!r}"
    )


@pytest.mark.parametrize("x_words,y_words", _distinct_pairs())
def test_faults_n13_b_second_input_result_independent_of_call_order(x_words, y_words):
    # Prime a neutral state of this test's own first, so leftover state from whatever test ran
    # immediately before it cannot be why this test passes or fails.
    decode(_distinctive_throwaway())
    # decode(X), then decode(Y) right after it. Capture X's own result too, so a wrong answer for
    # X is visible here rather than only checking Y.
    x_result = decode(copy.deepcopy(x_words))
    y_after_x = decode(copy.deepcopy(y_words))
    # decode(Y) alone (fresh) must equal decode(Y) run right after decode(X): Y's result must
    # not depend on whatever X put into decode() beforehand. The fresh reference is obtained only
    # now, after a fresh re-prime and after the X,Y sequence above has already run — taking it
    # beforehand risks it being the exact stale object a state-leaking decode() hands back for
    # both decode(X) and the later decode(Y), which would make the two sides of this equality
    # agree regardless of the defect.
    decode(_distinctive_throwaway())
    y_fresh = decode(copy.deepcopy(y_words))
    assert _result_signature(y_fresh) == _result_signature(y_after_x), (
        f"decode(Y) must produce the same result whether or not decode(X) ran first; "
        f"fresh={_result_signature(y_fresh)!r}, after-X={_result_signature(y_after_x)!r}"
    )
    # Direct oracle checks against the contract, independent of any other decode() call.
    assert y_after_x.raw == _expected_raw(y_words), (
        "decode(Y), run right after decode(X), must report raw reflecting Y's OWN words per "
        f"FAULTS-N11/N10/N12, not a value carried over from X's call; "
        f"got={y_after_x.raw!r}, expected={_expected_raw(y_words)!r}"
    )
    assert x_result.raw == _expected_raw(x_words), (
        "decode(X) must report raw reflecting X's OWN words per FAULTS-N11/N10/N12, not a value "
        f"inherited from any earlier call; got={x_result.raw!r}, expected={_expected_raw(x_words)!r}"
    )


def test_faults_n13_b_many_repeated_calls_with_varying_inputs_do_not_drift():
    # Prime a neutral state of this test's own first, so leftover state from whatever test ran
    # immediately before it cannot be why this test passes or fails.
    decode(_distinctive_throwaway())
    # A longer sequence: alternate between two distinct inputs several times and confirm each
    # one keeps producing the same result on every visit, not just immediately after a single
    # interleaving call.
    x_words = _all_six_many_bits()
    y_words = _subset_missing_most()
    x_results = [decode(copy.deepcopy(x_words)) for _ in range(4)]
    interleaved = [
        decode(copy.deepcopy(y_words if i % 2 == 0 else x_words)) for i in range(4)
    ]
    # A further reference for both X and Y, obtained after a fresh re-prime and after the whole
    # sequence above has already run, so the drift check below cannot be satisfied merely by
    # every captured result being the same stale object handed back from a state-leaking decode().
    decode(_distinctive_throwaway())
    x_fresh = decode(copy.deepcopy(x_words))
    decode(_distinctive_throwaway())
    y_fresh = decode(copy.deepcopy(y_words))

    x_signatures = {_result_signature(r) for r in x_results}
    assert len(x_signatures) == 1, (
        f"repeated decode() calls on the same input (X) must all return the same result; "
        f"got {len(x_signatures)} distinct signatures across {len(x_results)} calls"
    )
    y_signatures = {_result_signature(r) for i, r in enumerate(interleaved) if i % 2 == 0}
    assert len(y_signatures) == 1, (
        "repeated decode() calls on the same input (Y), interleaved with calls on a different "
        f"input (X), must all return the same result; got {len(y_signatures)} distinct signatures"
    )
    assert _result_signature(x_fresh) in x_signatures, (
        "an independently re-primed decode(X), computed after the whole interleaved sequence, "
        f"must match the signature seen during it; fresh={_result_signature(x_fresh)!r}, "
        f"repeated={x_signatures!r}"
    )
    assert _result_signature(y_fresh) in y_signatures, (
        "an independently re-primed decode(Y), computed after the whole interleaved sequence, "
        f"must match the signature seen during it; fresh={_result_signature(y_fresh)!r}, "
        f"repeated={y_signatures!r}"
    )

    # Direct oracle checks against the contract, independent of any other decode() call: every
    # captured result for X must reflect X's own words, and every captured result for Y (interleaved
    # or otherwise) must reflect Y's own words, regardless of what ran immediately before it.
    for i, r in enumerate(x_results):
        assert r.raw == _expected_raw(x_words), (
            f"repeated decode(X) call #{i} must report raw reflecting X's own words per "
            f"FAULTS-N11/N10/N12, not a value inherited from any other call; "
            f"got={r.raw!r}, expected={_expected_raw(x_words)!r}"
        )
    for i, r in enumerate(interleaved):
        expected_words = y_words if i % 2 == 0 else x_words
        assert r.raw == _expected_raw(expected_words), (
            f"interleaved decode() call #{i} must report raw reflecting its OWN words per "
            f"FAULTS-N11/N10/N12, not a value carried over from the previous call; "
            f"got={r.raw!r}, expected={_expected_raw(expected_words)!r}"
        )


def test_faults_n13_b_reversed_call_order_gives_equal_results_for_each_input():
    # Prime a neutral state of this test's own first, so leftover state from whatever test ran
    # immediately before it cannot be why this test passes or fails.
    decode(_distinctive_throwaway())
    # Decode X then Y (forward order), and separately decode Y then X (reversed order), using
    # fresh copies each time. Whichever one is decoded first, each input's own result must be
    # the same either way.
    x_words = _all_six_many_bits()
    y_words = _all_six_clean()

    x_result_forward = decode(copy.deepcopy(x_words))
    y_result_forward = decode(copy.deepcopy(y_words))

    y_result_reversed = decode(copy.deepcopy(y_words))
    x_result_reversed = decode(copy.deepcopy(x_words))

    # Further references for X and Y, each obtained after its own fresh re-prime and after the
    # whole forward/reversed sequence above has already run. The forward-vs-reversed comparisons
    # below can be satisfied by a state-leaking decode() that always hands back the same stale
    # object for a given input; an independently re-primed computation cannot be that object.
    decode(_distinctive_throwaway())
    x_fresh = decode(copy.deepcopy(x_words))
    decode(_distinctive_throwaway())
    y_fresh = decode(copy.deepcopy(y_words))

    assert _result_signature(x_result_forward) == _result_signature(x_result_reversed), (
        f"X's result must be the same whether X is decoded before or after Y; "
        f"forward={_result_signature(x_result_forward)!r}, "
        f"reversed={_result_signature(x_result_reversed)!r}"
    )
    assert _result_signature(y_result_forward) == _result_signature(y_result_reversed), (
        f"Y's result must be the same whether Y is decoded before or after X; "
        f"forward={_result_signature(y_result_forward)!r}, "
        f"reversed={_result_signature(y_result_reversed)!r}"
    )
    assert _result_signature(x_result_reversed) == _result_signature(x_fresh), (
        "X's result must match an independently re-primed decode(X) taken after the whole "
        f"sequence; reversed={_result_signature(x_result_reversed)!r}, fresh={_result_signature(x_fresh)!r}"
    )
    assert _result_signature(y_result_reversed) == _result_signature(y_fresh), (
        "Y's result must match an independently re-primed decode(Y) taken after the whole "
        f"sequence; reversed={_result_signature(y_result_reversed)!r}, fresh={_result_signature(y_fresh)!r}"
    )

    # Direct oracle checks against the contract, independent of any other decode() call.
    assert x_result_forward.raw == _expected_raw(x_words), (
        "decode(X), decoded before Y, must report raw reflecting X's own words per "
        f"FAULTS-N11/N10/N12; got={x_result_forward.raw!r}, expected={_expected_raw(x_words)!r}"
    )
    assert x_result_reversed.raw == _expected_raw(x_words), (
        "decode(X), decoded after Y, must report raw reflecting X's own words per "
        f"FAULTS-N11/N10/N12, not a value carried over from Y's call; "
        f"got={x_result_reversed.raw!r}, expected={_expected_raw(x_words)!r}"
    )
    assert y_result_forward.raw == _expected_raw(y_words), (
        "decode(Y), decoded after X, must report raw reflecting Y's own words per "
        f"FAULTS-N11/N10/N12, not a value carried over from X's call; "
        f"got={y_result_forward.raw!r}, expected={_expected_raw(y_words)!r}"
    )
    assert y_result_reversed.raw == _expected_raw(y_words), (
        "decode(Y), decoded before X, must report raw reflecting Y's own words per "
        f"FAULTS-N11/N10/N12; got={y_result_reversed.raw!r}, expected={_expected_raw(y_words)!r}"
    )


# ---------------------------------------------------------------------------
# FAULTS-N13 violation (b), targeted: X, Y pairs that hold exactly ONE register word IDENTICAL
# between X and Y while every other word differs, covering each of the six registers in turn as
# the fixed one. The pairs above (_distinct_pairs()) all happen to vary CFSR between X and Y, so
# a unit that reuses an earlier call's whole result whenever CFSR (or any other single register)
# matches its previous value, while ignoring the other five words, would never be exercised by
# them: a CFSR mismatch always forces a fresh computation. These pairs remove that escape by
# construction.
# ---------------------------------------------------------------------------


def _fixed_word_pairs():
    """(b) pairs where one register word is an IDENTICAL, explicit, nonzero value in both X and
    Y, while every other word differs. One pair per register, that register held fixed."""
    fixed_cfsr = (1 << 0) | (1 << 9)  # IACCVIOL, PRECISERR
    fixed_hfsr = 1 << 30  # FORCED
    fixed_dfsr = (1 << 1) | (1 << 2)  # BKPT, DWTTRAP
    fixed_mmfar = 0x77777777
    fixed_bfar = 0xABCDEF01
    fixed_afsr = 0xC0FFEE00
    return [
        pytest.param(
            {CFSR: fixed_cfsr, HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0x10000001, BFAR: 0x20000001, AFSR: 0x30000001},
            {CFSR: fixed_cfsr, HFSR: (1 << 30) | (1 << 31), DFSR: (1 << 2) | (1 << 3),
             MMFAR: 0x10000002, BFAR: 0x20000002, AFSR: 0x30000002},
            id="fixed-cfsr-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 0, HFSR: fixed_hfsr, DFSR: 1 << 0,
             MMFAR: 0x11000001, BFAR: 0x22000001, AFSR: 0x33000001},
            {CFSR: (1 << 8) | (1 << 16), HFSR: fixed_hfsr, DFSR: (1 << 1) | (1 << 4),
             MMFAR: 0x11000002, BFAR: 0x22000002, AFSR: 0x33000002},
            id="fixed-hfsr-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 0, HFSR: 1 << 1, DFSR: fixed_dfsr,
             MMFAR: 0x44000001, BFAR: 0x55000001, AFSR: 0x66000001},
            {CFSR: (1 << 9) | (1 << 17), HFSR: 1 << 31, DFSR: fixed_dfsr,
             MMFAR: 0x44000002, BFAR: 0x55000002, AFSR: 0x66000002},
            id="fixed-dfsr-others-vary",
        ),
        pytest.param(
            {CFSR: (1 << 0) | (1 << 7), HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: fixed_mmfar, BFAR: 0x88000001, AFSR: 0x99000001},
            {CFSR: (1 << 9) | (1 << 15), HFSR: 1 << 30, DFSR: 1 << 3,
             MMFAR: fixed_mmfar, BFAR: 0x88000002, AFSR: 0x99000002},
            id="fixed-mmfar-others-vary",
        ),
        pytest.param(
            {CFSR: (1 << 1) | (1 << 15), HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0xAA000001, BFAR: fixed_bfar, AFSR: 0xBB000001},
            {CFSR: (1 << 10) | (1 << 24), HFSR: 1 << 31, DFSR: 1 << 4,
             MMFAR: 0xAA000002, BFAR: fixed_bfar, AFSR: 0xBB000002},
            id="fixed-bfar-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 3, HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0xCC000001, BFAR: 0xDD000001, AFSR: fixed_afsr},
            {CFSR: (1 << 11) | (1 << 25), HFSR: (1 << 30) | (1 << 31), DFSR: 1 << 2,
             MMFAR: 0xCC000002, BFAR: 0xDD000002, AFSR: fixed_afsr},
            id="fixed-afsr-others-vary",
        ),
    ]


def _fixed_word_defaulted_pairs():
    """Like _fixed_word_pairs(), but the identical value shared by X and Y for the fixed register
    is 0, reached two different ways: explicitly present as 0 in one dict, entirely absent
    (defaulting to 0 per FAULTS-N10) in the other. Per FAULTS-N10 a missing address counts as 0,
    so these two are the same input as far as decode() is concerned; a unit whose stale-result
    reuse keys on the raw dict entry rather than the resolved value could tell them apart even
    though the contract says a caller must not be able to."""
    return [
        pytest.param(
            {CFSR: 0, HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0xE1000001, BFAR: 0xE2000001, AFSR: 0xE3000001},
            {HFSR: (1 << 30) | (1 << 31), DFSR: 1 << 3,
             MMFAR: 0xE1000002, BFAR: 0xE2000002, AFSR: 0xE3000002},
            id="fixed-cfsr-defaulted-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 0, HFSR: 0, DFSR: 1 << 0,
             MMFAR: 0xE4000001, BFAR: 0xE5000001, AFSR: 0xE6000001},
            {CFSR: (1 << 9) | (1 << 16), DFSR: 1 << 4,
             MMFAR: 0xE4000002, BFAR: 0xE5000002, AFSR: 0xE6000002},
            id="fixed-hfsr-defaulted-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 0, HFSR: 1 << 1, DFSR: 0,
             MMFAR: 0xE7000001, BFAR: 0xE8000001, AFSR: 0xE9000001},
            {CFSR: (1 << 10) | (1 << 17), HFSR: 1 << 31,
             MMFAR: 0xE7000002, BFAR: 0xE8000002, AFSR: 0xE9000002},
            id="fixed-dfsr-defaulted-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 0, HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0, BFAR: 0xEA000001, AFSR: 0xEB000001},
            {CFSR: (1 << 11) | (1 << 18), HFSR: 1 << 30, DFSR: 1 << 2,
             BFAR: 0xEA000002, AFSR: 0xEB000002},
            id="fixed-mmfar-defaulted-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 1, HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0xEC000001, BFAR: 0, AFSR: 0xED000001},
            {CFSR: (1 << 12) | (1 << 19), HFSR: 1 << 31, DFSR: 1 << 3,
             MMFAR: 0xEC000002, AFSR: 0xED000002},
            id="fixed-bfar-defaulted-others-vary",
        ),
        pytest.param(
            {CFSR: 1 << 3, HFSR: 1 << 1, DFSR: 1 << 0,
             MMFAR: 0xEE000001, BFAR: 0xEF000001, AFSR: 0},
            {CFSR: (1 << 13) | (1 << 20), HFSR: 1 << 30, DFSR: 1 << 4,
             MMFAR: 0xEE000002, BFAR: 0xEF000002},
            id="fixed-afsr-defaulted-others-vary",
        ),
    ]


@pytest.mark.parametrize("x_words,y_words", _fixed_word_pairs())
def test_faults_n13_b_second_result_reflects_own_words_when_one_word_matches_the_first(x_words, y_words):
    # Prime a neutral state of this test's own first, so leftover state from whatever test ran
    # immediately before it cannot be why this test passes or fails.
    decode(_distinctive_throwaway())
    # decode(X), then decode(Y) right after it, even though X and Y share an identical value for
    # one register word. Capture X's own result too: a defect that answers X with a stale cached
    # result (rather than its own words) must be caught here, not just a wrong answer for Y.
    x_result = decode(copy.deepcopy(x_words))
    y_after_x = decode(copy.deepcopy(y_words))
    # The "fresh" reference for Y is obtained only now, AFTER a fresh re-prime and AFTER the X,Y
    # sequence above has already run to completion — not beforehand. A fresh reference taken
    # before X (as in an earlier round) can itself be the very object a state-leaking decode()
    # hands back for both decode(X) and the later decode(Y): both sides of the equality would
    # then be the same stale object and agree regardless of the defect. Re-priming and recomputing
    # afterward forces an independent computation that cannot be that same cached object.
    decode(_distinctive_throwaway())
    y_fresh = decode(copy.deepcopy(y_words))
    assert _result_signature(y_fresh) == _result_signature(y_after_x), (
        "decode(Y) must reflect Y's own words in every observable field even when one register "
        f"word is identical to the word just decoded for X; fresh={_result_signature(y_fresh)!r}, "
        f"after-X={_result_signature(y_after_x)!r}"
    )
    # Direct oracle checks against the contract (FAULTS-N11/N10/N12), independent of any other
    # decode() call: these can't be satisfied by two calls simply agreeing with each other.
    assert y_after_x.raw == _expected_raw(y_words), (
        "decode(Y), run right after decode(X), must report raw reflecting Y's OWN words per "
        f"FAULTS-N11/N10/N12, not a value carried over from X's call; "
        f"got={y_after_x.raw!r}, expected={_expected_raw(y_words)!r}"
    )
    assert x_result.raw == _expected_raw(x_words), (
        "decode(X) must report raw reflecting X's OWN words per FAULTS-N11/N10/N12, not a value "
        f"inherited from any earlier call; got={x_result.raw!r}, expected={_expected_raw(x_words)!r}"
    )


@pytest.mark.parametrize("x_words,y_words", _fixed_word_defaulted_pairs())
def test_faults_n13_b_second_result_reflects_own_words_when_the_matching_word_is_defaulted(x_words, y_words):
    # Prime a neutral state of this test's own first, so leftover state from whatever test ran
    # immediately before it cannot be why this test passes or fails.
    decode(_distinctive_throwaway())
    # decode(X), then decode(Y) right after it. Capture X's own result too, same reasoning as the
    # sibling test above: a wrong answer for X must be visible here, not just a wrong answer for Y.
    x_result = decode(copy.deepcopy(x_words))
    y_after_x = decode(copy.deepcopy(y_words))
    # The fresh reference for Y is obtained only now, after a fresh re-prime and after the X,Y
    # sequence has already completed — see the sibling test above for why taking it beforehand
    # lets a state-leaking decode() hand back the same stale object on both sides of the equality.
    decode(_distinctive_throwaway())
    y_fresh = decode(copy.deepcopy(y_words))
    assert _result_signature(y_fresh) == _result_signature(y_after_x), (
        "decode(Y) must reflect Y's own words even when the one register word matching X's is "
        "reached through the FAULTS-N10 missing-address default rather than an explicit value; "
        f"fresh={_result_signature(y_fresh)!r}, after-X={_result_signature(y_after_x)!r}"
    )
    # Direct oracle checks against the contract, independent of any other decode() call.
    assert y_after_x.raw == _expected_raw(y_words), (
        "decode(Y), run right after decode(X), must report raw reflecting Y's OWN words per "
        f"FAULTS-N11/N10/N12, not a value carried over from X's call; "
        f"got={y_after_x.raw!r}, expected={_expected_raw(y_words)!r}"
    )
    assert x_result.raw == _expected_raw(x_words), (
        "decode(X) must report raw reflecting X's OWN words per FAULTS-N11/N10/N12, not a value "
        f"inherited from any earlier call; got={x_result.raw!r}, expected={_expected_raw(x_words)!r}"
    )
