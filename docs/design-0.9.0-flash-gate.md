# Design for stlink-mcp 0.9.0, version 4: writing an ELF or HEX file on a part whose flash base is not measured

Status: proposed, not built. Version 4 of 2026-11-08, corrected the same day at three sentences the verifier of version 4 (its verifier's notes) found left over from version 3 (lines on the gate's reach, the step number of the window test, and what a wrong base can admit). Version 4 makes the four edits the verifier of version 3 asked for (the verifier's notes on version 3, D1 to D5): the external-loader refusal becomes a step of C1 (D2); the `assumed` refusal and the after-write results join C4's list and FF6 matches (D3); A6 is marked unverified and R6 gains the U5, L0 and L1 families and the data-EEPROM case (D1, D5); the test list gains the window edges, the measured-base bypass and the external-loader refusal (D4). Version 3 of 2026-11-08. Version 2 answered the red-team report (RT1 to RT10); its verifier found RT1 still open, because the Cortex-M code region version 2 used as the bound holds RAM on STM32 parts (the F4's CCM at 0x10000000, the H7's ITCM at 0x00000000) and the F4's OTP area and option bytes, and listed six more defects (N1 to N6). This version answers those. The earlier versions are kept in the author's working notes, unchanged. Each changed item names what it answers.

## Purpose and audience

As version 2, with the second property stated more exactly (D5): the tool never reports `verified` for a write that did not persist at the addresses the file named. A write to RAM fails that property; a write to a part's data EEPROM, which is non-volatile and lands where the file said, does not, and R7 records the one way such a write can happen through a wrong base.

## What the tool does today

As version 2, which corrected version 1 on the tests, the full FF6 text and the findings dates. One more fact (N2): `check_consistency` in `quirks.py` runs on every FamilyQuirks, the built-in table and the vendor seeding included, not only on board-file entries; the measured STM32N6 has `flash_base = 0x70000000` with an external loader, and a board file may set `external_loader` with a base such as 0x90000000.

## The change

C1 (RT1, N1). For an ELF or HEX file, the write proceeds when all of these hold, tested in this order:

1. `quirks.flash_base` is not None (RT2). A None base refuses with "No flash base is recorded for <part>; pass a .bin with `address`", which names no provenance.
2. (D2) `quirks.external_loader` is None. A family whose flash is behind an external loader (the STM32N6 today) refuses ELF and HEX files with "This part's flash is behind an external loader, and the addresses in <file> cannot be checked against it; write a .bin with `address`". Today that path refuses only because the N6's connect banner carries no NVM size line (docs/findings.md, the N6 external flash entry); this step makes the refusal independent of the banner, so a future family with a loader and a size line is not admitted with a range built from an internal-flash size it does not have.
3. The base's provenance is `measured`; or it is `vendor-db` or `user-stated` and the base lies in the main-flash window, 0x08000000 to 0x0800FFFF inclusive. This is the same rule the vendor seeding already applies as a string prefix (`_FLASH_PREFIX = "0x0800"` in `vendordb.py`), stated as a number and applied to a board file's value as well. An `assumed` base refuses as today. A `user-stated` base outside the window refuses with "The flash base stated in your board file, 0x10000000, lies outside the window this tool writes ELF or HEX files to on a part it has not measured, 0x08000000 to 0x0800FFFF; pass a .bin with `address`".
4. The connect reported an NVM size.
5. Every run of the file lies in `[flash_base, flash_base + nvm_size)`, the present range check, unchanged in logic.

Why the window and not a region (N1): with the base at most 0x0800FFFF and the largest internal flash on the F4 and H7 families being 2 MB, the furthest address this gate can admit on those families is 0x0820FFFF; for other families the reach is the window plus whatever size the connect reports, which A6 and R6 cover. On the parts whose manuals are on disk, nothing in 0x08000000 to 0x0820FFFF is RAM, OTP or option bytes: RM0090 (F405/F407) puts CCM at 0x10000000, system memory at 0x1FFF0000 to 0x1FFF77FF, the OTP area at 0x1FFF7800 to 0x1FFF7A0F and the option bytes at 0x1FFFC000 to 0x1FFFC00F; RM0433 (H742/H743) puts ITCM at 0x00000000, system flash at 0x1FF00000 to 0x1FF1FFFF and flash bank 2 ending at 0x081FFFFF, and its OTP area's address was not found in the text copy. The U5 and the rest of the range are not checked (no manuals on disk); R6 records this.

C2. A .bin with no `address` is unchanged: it requires a measured base.

C3 (RT4). The range check's arithmetic is unchanged. Its refusal text names the base's provenance and the range, and it carries the fields of C4.

C4 (RT4, N-RT4, D3). Two fields, `flash_base_provenance` and `flash_range_checked`, appear on every ELF or HEX result produced after the connect, and on no result produced before it:

- every write result, `verified`, `unverified` (a readback that failed) or `failed` (a readback that differed), and the refusal of a run outside the range: `flash_base_provenance` is the base's provenance word; `flash_range_checked` is `{"low": "0x08000000", "high": "0x080FFFFF", "size_from": "the connect's NVM size"}`;
- the no-NVM-size refusal: the provenance word, and `{"low": "0x08000000", "high": null, "size_from": "none: the connect gave no NVM size"}`;
- the `assumed` refusal, the out-of-window `user-stated` refusal and the external-loader refusal: the provenance word, and `flash_range_checked` `null`;
- the None-base refusal: both fields present and `null`;
- refusals made before the connect (file type, empty file, `address` given with an ELF or HEX): neither field, since nothing about the part is known yet.

No caveat sentence anywhere; `next_step` keeps the FF9 boot text; no `note` key is added.

C5, C6. As version 2.

C7 (RT7, N3). FF2 becomes "It refuses to choose a write address from a flash base nobody measured: a .bin without `address` needs a measured base." FF6 becomes "Every run of bytes an ELF or HEX file writes lies inside the part's flash, from its recorded base for the connect's NVM size, where the base is measured, or comes from the vendor database or a board file and lies in the main-flash window 0x08000000 to 0x0800FFFF; a base that is absent, assumed, outside that window or behind an external loader is refused, as is a part whose connect gives no size, and so is `address` with such a file, since the file says where; every result after the connect, refusal or write, carries the base's provenance and the range checked, null where there is none." There is no FF10: version 2's start-up check is dropped (N2, N3), because a board file has no size field to check against, because `check_consistency` also validates the built-in and seeded entries where bases outside the window are correct, and because a board file with a base outside the window is still a valid file for `run_firmware` and the reads. The typo is caught at the first ELF or HEX flash call, by step 3 of C1, with the message above.

C8. Removed (see C7).

C9 (RT8, N-RT8). `_beside` clamps the spans it reads to `[flash_base, flash_base + nvm_size)` only when both are known and every run lies inside that range; otherwise (a .bin written at an explicit `address` outside it, a None base, no NVM size) it behaves as today, unclamped 128 KB blocks, and a failed read is reported "not checked" as today. On the measured parts nothing changes: the H7's and the F407's flash sizes are whole multiples of 128 KB and the N6 has no NVM size.

## Prior art (RT10)

As version 2, with one correction to its closing inference: the field's tools accept RAM writes without comment, which is why this version confines an unmeasured base to the main-flash window rather than to a region or to the programmer's judgment.

## Why the measured base protected less than it looked, and what this version relies on

The write address of an ELF or HEX run comes from the file, never from the base; the base decides only whether to refuse. What a wrong base can do is admit a file whose addresses are not the part's main flash, RAM above all. This version prevents the RAM case, and every case the checked manuals show, by bounding where an unmeasured base may be (the window) and how far it may reach (the NVM size), and it relies on no behaviour of the programmer. Inside the window, a wrong base (a bootloader offset, 0x08008000) can refuse a correct file linked at 0x08000000, or admit a run just past the real end of flash, which then reads back wrong or fails to read and is reported `failed` or `unverified`; the result names the provenance and the range in both cases.

## Assumptions, each with how it is established

A1 (N6). Dropped as a dependency and as a bench step. What the programmer does with a run outside flash does not bear on this design, and the measurement version 2 proposed could not be made safely without knowing what the F407 board carries on its external bus. If the author wants the fact for the record, it is a separate measurement with its own plan.

A2. ST's device database gives the base the programmer itself uses. Exercised against the faked programmer only.

A3 (N4, N5). A user-stated base cannot redirect an ELF write (from the code; confirmed by the red-team's reviewer A). Bench cases on the F407 through a board file for device ID 0x413 (a user entry wins over the built-in table and makes the base `user-stated`): the correct base 0x08000000, an ELF and a HEX written and read back identical, `flash_base_provenance` reported `user-stated`; a wrong base inside the window, 0x08008000, where a file linked at 0x08000000 is refused and the refusal names the provenance and the range 0x08008000 to 0x08107FFF; a base outside the window, 0x10000000, where the first ELF flash call refuses with the window message and the server started normally. The author recorded on 2026-10-03 that nothing on the F407 board needs keeping, so these writes need no sector save.

A4. The connect's NVM size is the internal flash size where the part has one. A smaller size gives a false refusal; a larger one admits a run past real flash, which reads back wrong or fails to read.

A5. The readback at the file's addresses reads the flash the write went to: .bin writes on the H7 and the F407 on 2026-10-01, ELF and HEX writes on the measured parts on 2026-10-04 (docs/findings.md).

A6 (D1, unverified). The gate's furthest reachable address is 0x0800FFFF plus the largest NVM size the programmer reports. For the F4 and H7 families that is 2 MB, so 0x0820FFFF, from their manuals. The STM32U5 range is believed, from memory and not from a manual on disk, to include 4 MB parts, which would extend the reach to 0x0840FFFF; nothing is known to sit there on those parts, and that is exactly what R6 asks the builder to check before release. A run admitted past real memory reads back wrong or fails to read and is reported `failed` or `unverified`.

## Risks accepted

R1. As version 2's first sentence: a file linked to the 0x0C000000 TrustZone alias is refused as outside the window and the range, a false refusal with a clear message. A board file cannot state that alias as the base for ELF or HEX writes (outside the window); such parts need a measured contribution or a .bin with `address`.

R2. Parts whose internal flash is not at 0x0800xxxx (the code's comment names WB0x and WL3x, unchecked against their manuals) get no vendor-db base, and a board file's base for them lies outside the window, so they keep refusing ELF and HEX files until measured. This is the cost of N1's lesson and is accepted.

R3, R4. As version 2.

R5 (N4, N5, restated). A user-stated base inside the window but at the wrong place, a bootloader offset being the likely case, refuses a correct file or admits a run past real flash; both are reported, neither is a false `verified`. Nothing about 0x00000000 (the loader rejects zero) or system memory (outside the window) remains in this risk.

R6 (N1, D1, D5). The window argument rests on the F4 and H7 manuals on disk (RM0090 and RM0433). Not checked, with what memory says about each and no manual to confirm it: the U5 (OTP near 0x0BFA0000, outside the window; parts up to 4 MB), the L0 and L1 (data EEPROM at 0x08080000, inside the window's reach, see R7), the G4, L4, F7 and G0. Before release, the builder files each family manual that can be fetched and grep-checks it for RAM, OTP, option bytes and EEPROM inside 0x08000000 to 0x0840FFFF, records the result in docs/findings.md, and the README names the families checked and the EEPROM case.

R7 (new, D5). On the L0 and L1 families, data EEPROM sits at 0x08080000 (from memory; to be confirmed in R6). With the correct base 0x08000000 and the connect's flash size the range ends below it. With a wrong base inside the window, 0x08008000 on a 512 KB part, the range reaches into the EEPROM, and an ELF carrying an EEPROM section is written there and reads back identical, `verified`. That write is non-volatile and lands where the file said, so the second property holds, but the README sentence about board files must not claim that a typo cannot change where a write goes; it says instead that a typo inside the window can only reach non-volatile memory the file itself addresses. Accepted.

## Out of scope

As version 2.

## Text that changes (RT6, N1)

As version 2's list, with these differences: no start-up check and no FF10; the "Describing your own board" sentence becomes "a board file's `flash_base` is used for ELF and HEX writes only when it lies in the main-flash window 0x08000000 to 0x0800FFFF, where no STM32 this project has checked keeps RAM, option bytes or OTP, so a typo cannot send a write to RAM or to memory that cannot be rewritten; on the L0 and L1 it can reach the data EEPROM; the result names the base as user-stated"; the README names the families checked (R6).

## Tests (RT5)

Against the faked programmer, from the restated needs (D4 added the last three): an ELF on a `vendor-db` base in the window is written and verified by readback; an ELF on a `user-stated` base in the window is written; a `user-stated` base outside the window refuses with the window message and the fields of C4; an `assumed` base refuses with the fields; a None base refuses with the None message and null fields; a run outside the range refuses with the provenance and range in text and fields; the no-NVM-size refusal carries the fields with `high` null; `address` with an ELF is still refused and carries no fields; a readback that fails and one that differs both carry the fields; `_beside` on a 64 KB flash with an in-range run reads nothing past the end, and on an out-of-range .bin behaves as today; the window edges, a `user-stated` base of 0x0800FFFF admitted and 0x08010000 refused; a `measured` base outside the window (a test-only FAMILIES entry, since no real one has internal flash there) admitted, showing the window applies to unmeasured bases only; a family with an `external_loader` and a connect banner that does carry an NVM size refused by step 2 of C1 with the fields. Then one seeded defect per changed need (FF2, FF6), each caught, before the author's read.

## Bench plan before release

1. A3's three board-file cases on the F407.
2. The three measured parts through `tools/hwcheck --flash`, unchanged behaviour.
3. R6's manual checks for the families filed by then.
4. The faked-programmer tests and the seeded-defect check, all green.
