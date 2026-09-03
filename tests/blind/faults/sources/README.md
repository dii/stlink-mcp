# Sources cited by the faults contract

The contract (`../contract.md`) cites two documents that were placed in the contract author's
and the writers' worktrees during the run. They are recorded here so every citation resolves.

- `cmsis-scb-fault-registers.md`: excerpt of Arm Limited's CMSIS_5 core headers `core_cm7.h`
  (Armv7-M) and `core_cm33.h` (Armv8-M), fetched 2026-09-03 from
  https://raw.githubusercontent.com/ARM-software/CMSIS_5/master/CMSIS/Core/Include/.
  Apache-2.0, redistributed here with that attribution. Bit positions and names of every SCB
  fault register field.
- `apnt209-cortex-m-fault-exceptions.md` (NOT redistributed): the text of Arm/Keil Application
  Note 209, "Using Cortex-M3/M4 Fault Exceptions", fetched 2026-09-03 from
  https://www.keil.com/appnotes/files/apnt209.pdf and converted with `pdftotext -layout`. Its
  redistribution terms are not known, so only its provenance is kept. Every fact the contract
  takes from it is quoted in the contract itself (the meaning of each fault status bit, and that
  MMFAR/BFAR are valid only when MMARVALID/BFARVALID are set).
AN209 pdf sha256: e2b3434371140fc8e1fcd7e629248499318bf94d2b9a023eb939094cf9946478
