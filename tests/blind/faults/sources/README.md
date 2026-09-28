# Sources cited by the faults contract

The contract (`../contract.md`) cites two documents that were placed in the contract author's
and the writers' worktrees during the run. They are recorded here so every citation resolves.

- `cmsis-scb-fault-registers.md`: excerpt of Arm Limited's CMSIS_5 core headers `core_cm7.h`
  (Armv7-M) and `core_cm33.h` (Armv8-M), fetched 2026-09-03 from
  https://raw.githubusercontent.com/ARM-software/CMSIS_5/master/CMSIS/Core/Include/.
  Apache-2.0, redistributed here with Arm's copyright notice and the licence text,
  `LICENSE-Apache-2.0.txt`, which was fetched from https://www.apache.org/licenses/LICENSE-2.0.txt
  on 2026-09-28 (sha256 cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30).
  Bit positions and names of every SCB fault register field.
- `apnt209-cortex-m-fault-exceptions.md` (NOT redistributed): the text of Arm/Keil Application
  Note 209, "Using Cortex-M3/M4 Fault Exceptions", fetched 2026-09-03 from
  https://www.keil.com/appnotes/files/apnt209.pdf and converted with `pdftotext -layout`. Its
  redistribution terms are not known, so only its provenance is kept. Every fact the contract
  takes from it is quoted in the contract itself (the meaning of each fault status bit, and that
  MMFAR/BFAR are valid only when MMARVALID/BFARVALID are set).
AN209 pdf sha256: e2b3434371140fc8e1fcd7e629248499318bf94d2b9a023eb939094cf9946478

## Citations that do not resolve by path

The contract, the decision record and the ledger were written in the run's own worktrees, and
they are hashed (`contract_sha256` in `decisions.json`), so they are kept as written rather than
edited to match this repository:

- `docs/sources/<name>` is where these two files sat during the run. They are kept here
  instead, in `tests/blind/faults/sources/`.
- `CALLSITES.md` was a listing, generated for the run, of where `faults.decode` is called. It
  was not kept. There is one call site, `read_fault_status` in `src/stlink_mcp/server.py`, and
  the line numbers the rulings quote (`server.py:1092-1098`) are those of commit `9d769bf`, the
  code the run was made against.
