# Contract for `src/stlink_mcp/faults.py` (unit slug `faults`)

Scope check: rank-1 sources (`AGENTS.md`, `ROADMAP.md`, `README.md`, the CMSIS register doc, the
AN209 application note) and the rank-2 source (`CALLSITES.md`) state clear, checkable needs for
this unit. Not declining.

This unit is pure logic: it takes a `dict[int, int]` of register words already read off a target
and turns them into a structured, human-readable interpretation. It performs no hardware access,
no timing, no reset, no probe I/O. Every clause below is level **logic** for that reason; the
"## Levels" section repeats this per clause as asked, but the justification is the same every
time, so it is not re-derived per clause.

General note for whoever writes tests from this contract: no source specifies the **order** of
entries within `FaultStatus.faults` or `FaultStatus.debug`. Compare them as sets/multisets of
`(name, category)` or `(category, name, meaning-is-nonempty)`, not as ordered lists.

## Reference bit table (source: `docs/sources/cmsis-scb-fault-registers.md`, rank 1, cross-checked
against `docs/sources/apnt209-cortex-m-fault-exceptions.md`, rank 1, "Fault types" table and the
detailed per-register sections)

All bit positions are absolute positions within the named 32-bit register.

| Register | Bit | Name | AN209 "Handler" | AN209 "Status Register" | Notes |
| --- | --- | --- | --- | --- | --- |
| CFSR (MMFSR byte) | 0 | IACCVIOL | MemManage | MMFSR | |
| CFSR (MMFSR byte) | 1 | DACCVIOL | MemManage | MMFSR | |
| CFSR (MMFSR byte) | 3 | MUNSTKERR | MemManage | MMFSR | |
| CFSR (MMFSR byte) | 4 | MSTKERR | MemManage | MMFSR | |
| CFSR (MMFSR byte) | 5 | MLSPERR | MemManage | MMFSR | |
| CFSR (MMFSR byte) | 7 | MMARVALID | MemManage | MMFSR | gates `mmfar`, not a fault entry itself (see FAULTS-N4) |
| CFSR (BFSR byte) | 8 | IBUSERR | BusFault | BFSR | |
| CFSR (BFSR byte) | 9 | PRECISERR | BusFault | BFSR | |
| CFSR (BFSR byte) | 10 | IMPRECISERR | BusFault | BFSR | |
| CFSR (BFSR byte) | 11 | UNSTKERR | BusFault | BFSR | |
| CFSR (BFSR byte) | 12 | STKERR | BusFault | BFSR | |
| CFSR (BFSR byte) | 13 | LSPERR | BusFault | BFSR | |
| CFSR (BFSR byte) | 15 | BFARVALID | BusFault | BFSR | gates `bfar`, not a fault entry itself (see FAULTS-N4) |
| CFSR (UFSR halfword) | 16 | UNDEFINSTR | UsageFault | UFSR | |
| CFSR (UFSR halfword) | 17 | INVSTATE | UsageFault | UFSR | |
| CFSR (UFSR halfword) | 18 | INVPC | UsageFault | UFSR | |
| CFSR (UFSR halfword) | 19 | NOCP | UsageFault | UFSR | AN209's own summary table misspells this "NOCPC"; its detailed section and CMSIS both use NOCP |
| CFSR (UFSR halfword) | 20 | STKOF | UsageFault | UFSR | **Armv8-M / core_cm33.h only** — absent from core_cm7.h. See FAULTS-N6 |
| CFSR (UFSR halfword) | 24 | UNALIGNED | UsageFault | UFSR | |
| CFSR (UFSR halfword) | 25 | DIVBYZERO | UsageFault | UFSR | |
| HFSR | 1 | VECTTBL | HardFault | HFSR | |
| HFSR | 30 | FORCED | HardFault | HFSR | |
| HFSR | 31 | DEBUGEVT | HardFault | HFSR | named for debug use, but AN209's fault-type table places it under HardFault/HFSR, not under DFSR |
| DFSR | 0 | HALTED | (not covered by AN209) | DFSR | name and position from CMSIS only |
| DFSR | 1 | BKPT | (not covered by AN209) | DFSR | name and position from CMSIS only |
| DFSR | 2 | DWTTRAP | (not covered by AN209) | DFSR | name and position from CMSIS only |
| DFSR | 3 | VCATCH | (not covered by AN209) | DFSR | name and position from CMSIS only |
| DFSR | 4 | EXTERNAL | (not covered by AN209) | DFSR | name and position from CMSIS only |

All other bit positions in CFSR, HFSR and DFSR are reserved/undefined in both source documents.
No source states what decode() should do if a reserved bit is set (see Observed-not-intended O5).

## Clauses

**FAULTS-N1** (rank 1, `docs/sources/cmsis-scb-fault-registers.md` core_cm7.h/core_cm33.h offsets,
cross-checked against rank 1 `docs/sources/apnt209-cortex-m-fault-exceptions.md` "Status and
address registers" address table): The six public constants name the correct absolute Cortex-M
System Control Block addresses: `CFSR=0xE000ED28`, `HFSR=0xE000ED2C`, `DFSR=0xE000ED30`,
`MMFAR=0xE000ED34`, `BFAR=0xE000ED38`, `AFSR=0xE000ED3C`. `FAULT_REGION_BASE == CFSR` and
`FAULT_REGION_SIZE == 24` (six 32-bit words, contiguous, CFSR through AFSR). `decode()` is called,
per `CALLSITES.md`, with the dict `Probe.read_memory(FAULT_REGION_BASE, FAULT_REGION_SIZE)`
returns, i.e. keyed by the six addresses `FAULT_REGION_BASE, FAULT_REGION_BASE+4, ...,
FAULT_REGION_BASE+20`.
violations: (a) any constant not matching its documented address, (b) `FAULT_REGION_BASE` not
equal to `CFSR`, (c) `FAULT_REGION_SIZE` not equal to 24.

**FAULTS-N2** (rank 1, `ROADMAP.md` Phase 5 "decodes every set bit"; rank 2, `CALLSITES.md`'s
docstring for `read_fault_status`, "The result says whether a fault is active and, when one is,
what happened in plain English"): `decode(words)` decodes every set bit among the CFSR
(MMFSR+BFSR+UFSR) and HFSR bits in the reference bit table above into one `ActiveBit` per set bit
in `FaultStatus.faults`, carrying that bit's canonical name from the table. A word with only one
defined bit set decodes to exactly one entry for that bit. A word with several defined bits set
decodes to one entry per set bit.
violations: (a) a set bit missing from the output, (b) several set bits collapsed into fewer
entries than are set, (c) an entry present for a bit that is not set, (d) a bit reported under the
wrong name.

**FAULTS-N3** (rank 1, `docs/sources/cmsis-scb-fault-registers.md`, SCB_DFSR_* positions; rank 2,
`CALLSITES.md`, `detail["debug_halt_reason"]` built from `status.debug` independently of
`status.faults`): `decode(words)` decodes every set bit among the five DFSR bits in the reference
table into `FaultStatus.debug`, using the same one-entry-per-set-bit rule as FAULTS-N2. DFSR bits
never appear in `FaultStatus.faults`, and CFSR/HFSR bits never appear in `FaultStatus.debug` — the
two lists are populated from disjoint registers.
violations: (a) a DFSR bit landing in `.faults` instead of `.debug`, (b) a CFSR/HFSR bit landing in
`.debug`, (c) a set DFSR bit missing from `.debug`.

**FAULTS-N4** (rank 1, `docs/sources/apnt209-cortex-m-fault-exceptions.md`: "This field is valid
only when MMFSR.MMARVALID is set" / "...only when BFSR.BFARVALID is set"; rank 1, `AGENTS.md` N2,
"A missing fact is reported, never inferred... Prefer a loud gap to a quiet guess"):
`FaultStatus.mmfar` is the raw MMFAR register word when CFSR bit 7 (MMARVALID) is set, and `None`
when it is clear. `FaultStatus.bfar` is the raw BFAR register word when CFSR bit 15 (BFARVALID) is
set, and `None` when it is clear. This holds regardless of which specific MemManage/BusFault bit
is also set.
violations: (a) a numeric address returned when the relevant VALID bit is clear (this is exactly
the "quiet guess" N2 forbids), (b) `None` returned when the VALID bit is set (losing real
evidence), (c) MMARVALID gating `bfar` or BFARVALID gating `mmfar` (the two are swapped).

**FAULTS-N5** (rank 1, `docs/sources/apnt209-cortex-m-fault-exceptions.md`: "AFSR ... Auxiliary
Fault Status Register. Implementation defined content"): `FaultStatus.afsr` always carries the raw
AFSR register word as a plain integer (0 if the input `words` dict omits that address). It is
never decoded into named bits and never gated by a validity flag, because no source defines what
its bits mean.
violations: (a) `afsr` decoded into `ActiveBit` entries as if its bits were defined, (b) `afsr`
made `None`/optional or gated by a validity check that no source describes.

**FAULTS-N6** (rank 1, `docs/sources/cmsis-scb-fault-registers.md`, which is explicitly two
headers, core_cm7.h for Armv7-M and core_cm33.h for Armv8-M, "which adds STKOF" per
`docs/sources/SOURCES.md`'s own framing of that source; corroborated by this unit's module
docstring stating it covers "every Cortex-M with configurable faults (Armv7-M and Armv8-M)"):
`decode()` recognizes STKOF (CFSR/UFSR bit 20) as a defined UsageFault bit, in addition to the
bits common to both architectures, because the unit is not specific to one Cortex-M generation.
violations: (a) STKOF absent from the decoded output when set, (b) STKOF treated as an undefined/
reserved bit.

**FAULTS-N7** (rank 2, `CALLSITES.md`, `read_fault_status` docstring: "The result says whether a
fault is active"; rank 2, `CALLSITES.md`'s independent gating of `debug_halt_reason` on
`status.debug` regardless of `status.any_fault`): `FaultStatus.any_fault` is `True` exactly when
`FaultStatus.faults` is non-empty, and is unaffected by `FaultStatus.debug`. A target halted for a
breakpoint/watchpoint (DFSR bits set) with no CFSR/HFSR bits set reports `any_fault == False`.
violations: (a) `any_fault` `True` with an empty `.faults`, (b) `any_fault` `False` despite a
non-empty `.faults`, (c) `any_fault` toggled by `.debug` alone.

**FAULTS-N8** (rank 1, `ROADMAP.md` Phase 5: "reports a plain-English diagnosis... with the
faulting address when the hardware recorded it"; rank 2, `CALLSITES.md`, `"diagnosis":
status.diagnosis` read unconditionally, not gated on `any_fault`; rank 3, the `_diagnose`
docstring, "One-sentence summary of the active faults, with addresses when known"):
`FaultStatus.diagnosis` is always a non-empty string, including on a clean read (`any_fault ==
False`), where it states plainly that no fault is active. When `any_fault` is `True`, the text is
specific to the bits that are active rather than a generic placeholder: any two distinct sets of
active bits produce different text, and a state with several active bits produces text that
differs from each of its single-bit members' text. No source fixes whether that text uses the
CMSIS macro name, the status-byte name, or a prose paraphrase, so none of those is required. When
`mmfar` or `bfar` is not `None`, the diagnosis text includes that address.
Note: no source fixes the exact wording or the exact address format (hex vs decimal, case, width);
test by substring presence of the address's hex digits and by distinctness of the text, not by
exact string equality or by any particular naming form. (Re-worded 2026-09-03 on the arbiter's
step-4 ruling: the earlier "by bit name or category" parenthetical was stricter than its sources.)
violations: (a) `diagnosis` empty or absent on a clean read, (b) `diagnosis` identical for two
different active-fault states (a hardcoded placeholder), (c) a known `mmfar`/`bfar` address never
appearing in the text, (d) a multi-bit state whose text equals one of its single-bit members' text
(only the first fault reported).

**FAULTS-N9** (rank 1, `docs/sources/apnt209-cortex-m-fault-exceptions.md` "Fault types" table,
"Handler" and "Status Register" columns): `ActiveBit.category` groups bits by which status byte
defines them: all bits from the MMFSR byte of CFSR share one category value, all bits from the
BFSR byte share a second, all bits from the UFSR halfword share a third, and all HFSR bits share a
fourth — including DEBUGEVT, which AN209's own table places under the HardFault/HFSR family
despite the name suggesting a debug concern.
Note (weak part): AN209 offers two plausible literal spellings for these four values, its
"Handler" column ("HardFault", "MemManage", "BusFault", "UsageFault") or its "Status Register"
column ("HFSR", "MMFSR", "BFSR", "UFSR"), and no rank-1/2 source commits to one over the other; see
Observed-not-intended O1. The load-bearing, strongly-sourced part of this clause is the
differentiation property, not a specific string.
violations: (a) two bits from the same status byte (e.g. IACCVIOL and DACCVIOL) reported with
different category values, (b) two bits from different status bytes (e.g. IACCVIOL and IBUSERR)
reported with the same category value, (c) DEBUGEVT categorized outside the HardFault family.

**FAULTS-N10** (rank 3, `decode()` docstring: "Missing addresses default to 0, which is safe: zero
means no fault bit is set."; weak, since this specific defaulting behaviour is not independently
stated by any rank 1 or rank 2 source): `decode(words)` does not raise or misbehave when the input
dict omits one or more of the six addresses; a missing address is treated as register value 0 (no
bit set, and for MMFAR/BFAR/AFSR, value 0).
violations: (a) `decode()` raising `KeyError`/similar when a key is missing, (b) a missing address
treated as anything other than 0.

**FAULTS-N11** (rank 2, `CALLSITES.md`: `"raw_registers": {name: f"0x{val:08X}" for name, val in
status.raw.items()}`, every entry formatted as an unsigned 32-bit hex value; rank 3, the module
docstring's listing of the six register names): `FaultStatus.raw` is a dict with one entry per
register (CFSR, HFSR, DFSR, MMFAR, BFAR, AFSR), each value being the same integer used elsewhere
in the result (the register word, or 0 under the FAULTS-N10 default), and every value fits in an
unsigned 32-bit range (so it can be formatted `0x{val:08X}` without a `-` or a `ValueError`).
Note (weak part): the exact key strings (e.g. whether the CFSR entry's key is literally `"CFSR"`)
are only suggested by the module docstring's register listing (rank 3), not fixed by `CALLSITES.md`
or `ROADMAP.md`.
violations: (a) `raw` missing an entry for one of the six registers, (b) a value in `raw` that
disagrees with the value `decode()` actually used to compute `faults`/`debug`/`mmfar`/`bfar`/
`afsr` for that same register, (c) a negative value or a value outside 32-bit unsigned range.

**FAULTS-N12** (rank 1, `docs/sources/cmsis-scb-fault-registers.md`, the SCB struct member names in
`core_cm7.h`/`core_cm33.h`; corroborated by the AN209 "Status and address registers" table; promoted
from O3 by the owner's ruling of 2026-09-03): the keys of `FaultStatus.raw` are exactly the six CMSIS
register names `"CFSR"`, `"HFSR"`, `"DFSR"`, `"MMFAR"`, `"BFAR"`, `"AFSR"`.
violations: (a) a key that is not one of those six names, (b) a key set that is not exactly those
six.

## Observed, not intended

- **O1.** The literal string used for `ActiveBit.category` is not pinned by any rank-1/2 source.
  AN209's own "Fault types" table offers two different naming schemes (Handler names vs. Status
  Register names) for the same grouping, and neither `CALLSITES.md` nor the stub commits to
  either. FAULTS-N9 only requires the grouping to be internally consistent.
- **O2.** The category value used for DFSR-derived (`.debug`) `ActiveBit` entries is not named by
  any source. `CALLSITES.md`'s construction of `debug_halt_reason` doesn't even read `.category`
  for these entries (`{"bit": d.name, "meaning": d.meaning} for d in status.debug`), so nothing
  observed constrains its value beyond the dataclass requiring the field to be present.
- **O3.** (Promoted to FAULTS-N12 by the owner's ruling of 2026-09-03; the arbiter had escalated
  it with the CMSIS struct member names as a rank-1 source.)
- **O4.** The exact wording of `.diagnosis` and the exact text format of any embedded address
  (hex/decimal, case, zero-padding) are not specified beyond "plain English" and "with addresses
  when known"; see the note on FAULTS-N8.
- **O5.** No source states what `decode()` should do if a reserved/undefined bit position (e.g.
  CFSR bits 2, 6, 14, 21-23, 26-31) is set in the input word. Not a clause.
- **O6.** AN209's own "Fault types" summary table spells the no-coprocessor bit "NOCPC", which
  disagrees with both CMSIS's macro name and AN209's own detailed per-bit section, both of which
  say "NOCP". The reference bit table above resolves this in favour of NOCP, but no source
  explains the table's typo, so this resolution is this contract's judgement call rather than
  something stated outright.
- **O7.** `AGENTS.md` N9 ("nothing assumes a single implicit caller") and N1 (the
  verified/unverified/failed result envelope) are real needs for this project, but neither applies
  to this unit specifically: `faults.py` holds no per-caller state at all (N9 is satisfied
  trivially, not testably, by the unit having no state), and the verified/unverified/failed
  envelope is built by the caller (`server.py`), not by `decode()`, which returns a plain
  dataclass. Not clauses here.
- **O8.** The naming form used inside `.diagnosis` (CMSIS macro name, status-byte name, or a prose
  paraphrase) is not fixed by any source. Recorded on the arbiter's step-4 ruling, 2026-09-03.

## Levels

Every clause (FAULTS-N1 through FAULTS-N12) is level **logic**. None of their sentences name
timing, a peripheral, power, or hardware state — they describe what `decode()` computes from a
`dict[int, int]` of already-read register words, which is exactly the pure-logic surface the
module docstring describes ("Pure logic, no hardware."). The actual register read (`allow_reset`,
probe connection, etc.) is caller-side (`server.py`, rank 2) and out of scope for this unit's
contract.
