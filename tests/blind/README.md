# Blind tests

`src/stlink_mcp/faults.py` is also tested by a suite written blind: from a
contract, by writers who did not see the implementation. Its first run found two
defects the ordinary suite agreed with (docs/findings.md, "The MemManage bit map
was wrong, and 52 tests agreed with it").

What is here, for the `faults` unit:

- `faults/contract.md` and `faults/contract.json`: the needs the unit has to
  meet, FAULTS-N1 to FAULTS-N13, each with its source. They were written from
  Arm's register definitions and application note rather than from the code.
  The `observed_not_intended` entries are behaviour the code has that no source
  asks for.
- `test_faults.py` and `test_faults_n13.py`: the tests, each named for the one
  clause it checks. They run with the rest of the suite.
- `faults/seeds/`: defects injected into `faults.py` by find and replace, one
  per file, each naming the clause it violates.
- `faults/ledger.json`: for each seed, which tests it turned red, and the audit
  and arbiter results.
- `faults/decisions.json` and `faults/arbiter-rulings-n13.json`: rulings on
  clauses and seeds, by the arbiter and by the owner, with reasons.
- `faults/audit.md`: needs with no failing test, and behaviour with no need,
  found by reading the tests against the contract.
- `faults/sources/`: the documents the contract cites, with their provenance and
  licences, and a note on the citations that do not resolve by path.

The procedure itself, who may see what and when, is not published in this
repository; these files are its record for this unit. Line numbers in them refer
to the code as it was during the run, not as it is now.

If you change `faults.py`, check that the text each seed finds still occurs
exactly once, so every seed still injects the defect it describes. It does as of
2026-09-28, after the classification helpers were added beside `decode()`.
