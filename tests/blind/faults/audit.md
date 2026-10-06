# Audit — `src/stlink_mcp/faults.py` (unit slug `faults`)

> This is the record of what the audit found during the run, not a statement of present coverage. The gaps in section 1 were closed afterwards by `test_faults_n5_d` and `test_faults_n11_e`, added before the suite was committed, and by the tests in `test_faults_n13.py`. Line numbers refer to `faults.py` as it was then.

Read: `src/stlink_mcp/faults.py`, `tests/blind/test_faults.py`, `tests/blind/faults/contract.md`.
No other test file was read. Findings below were checked by running the real suite
(`.venv/bin/python -m pytest tests/blind/test_faults.py -q`, 89 passed baseline) and by
injecting each candidate defect into a throwaway copy of the module under `/tmp` (shadowed
via `PYTHONPATH`, never touching the repo's unit or tests) and re-running the same 89 tests.

## 1. Needs with no failing test

**FAULTS-N5** — violation (a): AFSR's bits are decoded into `ActiveBit` fault entries in
addition to being passed through as `FaultStatus.afsr`.
Why unnoticed: `test_faults_n5_a_afsr_is_always_the_raw_register_word`,
`test_faults_n5_b_afsr_unaffected_by_bits_or_valid_flags_elsewhere` and
`test_faults_n5_c_afsr_never_none_even_on_clean_read` all call `make_words(afsr=...)` with
nonzero AFSR values but assert only on `status.afsr`; none inspects `status.faults` or
`status.debug` for extraneous entries. No other test in the suite sets a nonzero AFSR value
together with an assertion on the exact contents of `.faults` — the multi-bit checks
(`test_faults_n2_c_...`, `test_faults_n8_b_...`, etc.) always call `make_words` with the
default `afsr=0`, so a defect that only manifests when AFSR is nonzero never lines up with a
test that reads `.faults`. Confirmed empirically: adding a 32-entry synthetic bit table and
extending `faults` with `_extract(afsr, ..., "Auxiliary")` inside `decode()` left all 89 tests
green.

**FAULTS-N11** — violation (b), instance 1: `raw["CFSR"]` holds `mmfsr` (the CFSR word masked
to its low byte, bits 0-7) instead of the full `cfsr` word.
Why unnoticed: the only test that checks `raw`'s per-register values against the words that
were actually used, `test_faults_n11_b_raw_values_match_the_registers_actually_used`, uses a
CFSR test value of `1 << 4` (MSTKERR alone) — a bit that already lies inside the low byte, so
`mmfsr == cfsr` for that specific input and the two are indistinguishable.
`test_faults_n11_a_raw_has_one_entry_per_register` only checks the entry count (6);
`test_faults_n11_c_raw_mmfar_and_bfar_entries_are_unaffected_by_gating` uses a CFSR word of 0
(no fault bits set, via `extra_cfsr`'s default); `test_faults_n11_d_raw_values_fit_unsigned_32_bit_range`
sets CFSR to `0xFFFFFFFF`, whose masked byte (`0xFF`) still satisfies the 32-bit-range
assertion. Confirmed empirically: replacing `"CFSR": cfsr` with `"CFSR": mmfsr` in the `raw=`
literal left all 89 tests green.

**FAULTS-N11** — violation (b), instance 2 (same clause, different mechanism):
`raw["MMFAR"]` and `raw["BFAR"]` are swapped with each other (each still carries a real
register word — just the other register's).
Why unnoticed: every test that checks `raw`'s per-register values does so by set/multiset
membership rather than by key. `test_faults_n11_b_raw_values_match_the_registers_actually_used`
compares `sorted(status.raw.values())` against the sorted list of expected words, and
`test_faults_n11_c_raw_mmfar_and_bfar_entries_are_unaffected_by_gating` only checks
`mmfar_word in status.raw.values()` / `bfar_word in status.raw.values()`. Both assertions are
satisfied by any permutation of the six values across the six keys, so nothing ties a value to
its own key; `test_faults_n12_a/b` only check the key *set*, never which value sits under which
key. Confirmed empirically: swapping the two lines to `"MMFAR": bfar_raw, "BFAR": mmfar_raw`
(leaving `FaultStatus.mmfar`/`.bfar` themselves correctly computed from the un-swapped local
variables, so only the `raw` dict is wrong) left all 89 tests green. This is a distinct
instance of the same shape as the CFSR/mmfsr case above — the same clause is unreachable by two
different mechanisms (wrong per-key value from truncation, and correct values under swapped
keys) because no test does a keyed `status.raw["MMFAR"] == mmfar_raw` comparison anywhere.

All other clauses (FAULTS-N1, N2, N3, N4, N6, N7, N8, N9, N10, N12) were checked against the
seeded-defect run recorded in `ledger.json`: every seed for those clauses is `red-attributed`
(caught by at least one test carrying that clause's id), and independent review of the test
bodies found no comparable blind spot (parametrized single-bit tests tie name to bit directly;
the N4/N7/N9 gating and grouping tests use paired, distinguishing inputs rather than
memberships across registers). They are not reported here.

## 2. Behaviour with no need

- **FAULTS-O9.** `_diagnose()` always places the MMFAR sentence before the BFAR sentence when
  both are present, and both before the VECTTBL/DEBUGEVT sentences — a fixed ordering the
  contract does not require (FAULTS-N8 only requires substring presence). `src/stlink_mcp/faults.py:199-211`.
- **FAULTS-O10.** `_diagnose()` never emits the literal FORCED meaning string from
  `_HFSR_BITS` ("forced hard fault, escalated from a configurable fault whose handler is not
  enabled or itself faulted"); it substitutes either a ", escalated to a hard fault" suffix or
  a different standalone sentence ("forced hard fault with no configurable fault bits set").
  `src/stlink_mcp/faults.py:213-220`.
- **FAULTS-O11.** `_diagnose()` never mentions the raw AFSR value anywhere in its text, even
  when `FaultStatus.afsr`/`raw["AFSR"]` is nonzero. `src/stlink_mcp/faults.py:177-223` (whole
  function; no AFSR reference anywhere in it).
- **FAULTS-O12.** `decode()` reads `words` only through `.get()` and never mutates the caller's
  dict. `src/stlink_mcp/faults.py:138-143`.
- **FAULTS-O13.** `decode()` silently ignores any key in `words` other than the six documented
  addresses; an unexpected extra key (e.g. from a larger memory dump passed by mistake) is
  neither reported nor rejected. `src/stlink_mcp/faults.py:138-143`.
- **FAULTS-O14.** `ActiveBit` is declared `frozen=True` (immutable) while `FaultStatus` is a
  plain mutable dataclass. `src/stlink_mcp/faults.py:42,50`.
- **FAULTS-O15.** Within each category, `_extract()` always yields entries in ascending
  bit-position order (the order the `_MMFSR_BITS`/`_BFSR_BITS`/`_UFSR_BITS`/`_HFSR_BITS`/
  `_DFSR_BITS` tables are written in), and `.faults` is always assembled in MMFSR, then BFSR,
  then UFSR, then HFSR order — deterministic, though the contract explicitly disclaims any
  required order. `src/stlink_mcp/faults.py:73-128,149-153`.
- **FAULTS-O16.** `FaultStatus.raw`'s keys always iterate in the fixed insertion order CFSR,
  HFSR, DFSR, MMFAR, BFAR, AFSR, though FAULTS-N11/N12 only require the key *set*.
  `src/stlink_mcp/faults.py:166-173`.
