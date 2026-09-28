"""Cortex-M fault register decoding.

Pure logic, no hardware. Takes the raw words from the System Control Block
fault registers and produces a structured interpretation of what happened.

The registers are at fixed addresses in every Cortex-M with configurable
faults: Armv7-M and Armv8-M Mainline. Armv6-M (Cortex-M0, M0+, M1) and Armv8-M
Baseline (Cortex-M23) do not have them. This file used to say they read as zero
there, "which is the correct answer", and that was the wrong conclusion even if
the reading is right: a zero decodes as "no fault active", and those cores take
every fault as a HardFault that records nothing here, so a core sitting in its
HardFault handler would be reported clean. `has_fault_registers` lets the tool
refuse those cores instead.

  CFSR   0xE000ED28   Configurable Fault Status (MMFSR + BFSR + UFSR)
  HFSR   0xE000ED2C   HardFault Status
  DFSR   0xE000ED30   Debug Fault Status
  MMFAR  0xE000ED34   MemManage Fault Address
  BFAR   0xE000ED38   Bus Fault Address
  AFSR   0xE000ED3C   Auxiliary Fault Status (implementation-defined)

Reading 24 bytes (6 words) starting at CFSR covers all of them. The input to
decode() is the dict that Probe.read_memory returns for that region.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


# Fixed addresses in the Cortex-M System Control Block.
CFSR = 0xE000ED28
HFSR = 0xE000ED2C
DFSR = 0xE000ED30
MMFAR = 0xE000ED34
BFAR = 0xE000ED38
AFSR = 0xE000ED3C

FAULT_REGION_BASE = CFSR
FAULT_REGION_SIZE = 24  # bytes; 6 words, CFSR through AFSR inclusive

#: Every register decode() reads, under the name `FaultStatus.raw` uses.
REGISTERS: dict[str, int] = {
    "CFSR": CFSR, "HFSR": HFSR, "DFSR": DFSR,
    "MMFAR": MMFAR, "BFAR": BFAR, "AFSR": AFSR,
}


def missing_registers(words: Mapping[int, int]) -> list[str]:
    """Names of the registers a read did not return, in address order.

    decode() treats an absent word as zero, which keeps it total and is what
    the blind contract ratified (FAULTS-N10). For the tool it is the wrong
    reading: zero means "no fault bit set", so a register the read never
    returned would be reported as a clean one. The tool checks this first and
    refuses a partial read rather than decode it.
    """
    return [name for name, address in REGISTERS.items() if address not in words]


# Core names as the programmer prints them after "Device CPU", for example
# "Cortex-M55" (captured on an STM32N6) and "Cortex-M7". The digit lookahead keeps
# M3 from matching M33 or M35P, and M1 from matching a later M1x.
_NO_FAULT_REGISTERS = re.compile(r"Cortex-M(?:0|1|23)(?![0-9])")
_HAS_FAULT_REGISTERS = re.compile(r"Cortex-M(?:3|4|7|33|35P|52|55|85)(?![0-9])")
_MAY_BANK_BY_SECURITY = re.compile(r"Cortex-M(?:33|35P|52|55|85)(?![0-9])")


def has_fault_registers(cpu: str | None) -> bool | None:
    """Whether a core of this name has the registers decode() reads.

    False for Armv6-M (Cortex-M0, M0+, M1) and Armv8-M Baseline (Cortex-M23),
    True for Armv7-M and Armv8-M Mainline, and None for a name that is missing
    or not recognised here. None is reported as unknown rather than guessed
    either way, because on a core without the registers a clean-looking read
    is not evidence of anything.
    """
    if not cpu:
        return None
    if _NO_FAULT_REGISTERS.search(cpu):
        return False
    if _HAS_FAULT_REGISTERS.search(cpu):
        return True
    return None


def may_bank_by_security(cpu: str | None) -> bool:
    """Whether this is an Armv8-M Mainline core, which may implement the
    Security Extension. Where it does, MMFSR, UFSR and MMFAR are banked by
    security state and one read sees one bank."""
    return bool(cpu and _MAY_BANK_BY_SECURITY.search(cpu))

# Validity flags inside CFSR, used to gate the address registers.
_MMARVALID = 1 << 7   # MMFSR bit 7 = CFSR bit 7
_BFARVALID = 1 << 15  # BFSR bit 7 = CFSR bit 15


@dataclass(frozen=True)
class ActiveBit:
    """One set bit in a fault or debug status register."""
    category: str  # "MemManage", "BusFault", "UsageFault", "HardFault", "Debug"
    name: str      # ARM architectural name, e.g. "PRECISERR"
    meaning: str   # plain-English one-liner


@dataclass
class FaultStatus:
    """The complete decoded state of the fault registers."""
    faults: list[ActiveBit]
    debug: list[ActiveBit]
    mmfar: int | None
    bfar: int | None
    afsr: int
    raw: dict[str, int]

    @property
    def any_fault(self) -> bool:
        return bool(self.faults)

    @property
    def diagnosis(self) -> str:
        return _diagnose(self)


# Bit definitions: (bit_position_within_subregister, name, meaning).
# MMARVALID and BFARVALID are validity flags for the address registers, not
# faults, and are handled separately in decode().

_MMFSR_BITS: list[tuple[int, str, str]] = [
    (0, "IACCVIOL", "instruction access violation"),
    (1, "DACCVIOL", "data access violation"),
    (3, "MUNSTKERR", "unstacking for exception return faulted"),
    (4, "MSTKERR", "stacking for exception entry faulted"),
    (5, "MLSPERR", "MemManage fault during floating-point lazy state preservation"),
]

_BFSR_BITS: list[tuple[int, str, str]] = [
    (0, "IBUSERR", "instruction bus error on prefetch"),
    (1, "PRECISERR", "precise data bus error"),
    (2, "IMPRECISERR", "imprecise data bus error"),
    (3, "UNSTKERR", "bus fault on exception unstacking"),
    (4, "STKERR", "bus fault on exception stacking"),
    (5, "LSPERR", "bus fault during floating-point lazy state preservation"),
]

_UFSR_BITS: list[tuple[int, str, str]] = [
    (0, "UNDEFINSTR", "undefined instruction"),
    (1, "INVSTATE",
     "invalid execution state (Thumb bit clear on a branch target "
     "or function pointer)"),
    (2, "INVPC", "invalid exception return value loaded to PC"),
    (3, "NOCP",
     "coprocessor instruction with no coprocessor present or enabled"),
    (4, "STKOF", "stack overflow (Armv8-M only)"),
    (8, "UNALIGNED", "unaligned memory access"),
    (9, "DIVBYZERO", "integer divide by zero"),
]

_HFSR_BITS: list[tuple[int, str, str]] = [
    (1, "VECTTBL",
     "bus fault on vector table read during exception processing"),
    (30, "FORCED",
     "forced hard fault, escalated from a configurable fault whose "
     "handler is not enabled or itself faulted"),
    (31, "DEBUGEVT", "hard fault caused by a debug event"),
]

_DFSR_BITS: list[tuple[int, str, str]] = [
    (0, "HALTED", "halt requested by debugger"),
    (1, "BKPT", "breakpoint instruction or flash patch match"),
    (2, "DWTTRAP", "data watchpoint match"),
    (3, "VCATCH", "vector catch triggered"),
    (4, "EXTERNAL", "external debug request"),
]


def _extract(
    value: int, defs: list[tuple[int, str, str]], category: str,
) -> list[ActiveBit]:
    return [
        ActiveBit(category=category, name=name, meaning=meaning)
        for bit, name, meaning in defs
        if value & (1 << bit)
    ]


def decode(words: dict[int, int]) -> FaultStatus:
    """Decode a memory read of the fault register region into a FaultStatus.

    `words` is the dict returned by Probe.read_memory for the 24-byte region
    starting at CFSR (0xE000ED28). Missing addresses default to 0, which is
    safe: zero means no fault bit is set.
    """
    cfsr = words.get(CFSR, 0)
    hfsr = words.get(HFSR, 0)
    dfsr = words.get(DFSR, 0)
    mmfar_raw = words.get(MMFAR, 0)
    bfar_raw = words.get(BFAR, 0)
    afsr = words.get(AFSR, 0)

    mmfsr = cfsr & 0xFF
    bfsr = (cfsr >> 8) & 0xFF
    ufsr = (cfsr >> 16) & 0xFFFF

    faults: list[ActiveBit] = []
    faults.extend(_extract(mmfsr, _MMFSR_BITS, "MemManage"))
    faults.extend(_extract(bfsr, _BFSR_BITS, "BusFault"))
    faults.extend(_extract(ufsr, _UFSR_BITS, "UsageFault"))
    faults.extend(_extract(hfsr, _HFSR_BITS, "HardFault"))

    debug = _extract(dfsr, _DFSR_BITS, "Debug")

    mmfar = mmfar_raw if (cfsr & _MMARVALID) else None
    bfar = bfar_raw if (cfsr & _BFARVALID) else None

    return FaultStatus(
        faults=faults,
        debug=debug,
        mmfar=mmfar,
        bfar=bfar,
        afsr=afsr,
        raw={
            "CFSR": cfsr,
            "HFSR": hfsr,
            "DFSR": dfsr,
            "MMFAR": mmfar_raw,
            "BFAR": bfar_raw,
            "AFSR": afsr,
        },
    )


def _diagnose(status: FaultStatus) -> str:
    """One-sentence summary of the active faults, with addresses when known."""
    if not status.faults:
        return (
            "no fault is active; the registers are clean since the last "
            "reset or were cleared by software"
        )

    names = {f.name for f in status.faults}

    # Configurable fault root causes, with addresses where the hardware
    # recorded them.
    root_causes: list[str] = []
    for f in status.faults:
        if f.category == "HardFault":
            continue
        root_causes.append(f.meaning)

    # The fault address belongs to the register that recorded it, gated only by
    # the hardware's own VALID flag, never attributed to a particular bit: the
    # earlier per-bit rule dropped a valid MMFAR for stacking faults and claimed
    # one for IACCVIOL, which never records an address (AN209).
    if status.mmfar is not None:
        root_causes.append(f"MMFAR holds 0x{status.mmfar:08X}")
    if status.bfar is not None:
        root_causes.append(f"BFAR holds 0x{status.bfar:08X}")

    # HardFault-specific causes (standalone, not escalation context)
    if "VECTTBL" in names:
        root_causes.append(
            "bus fault on vector table read (the vector table address "
            "is most likely wrong)"
        )
    if "DEBUGEVT" in names:
        root_causes.append("hard fault from a debug event")

    summary = "; ".join(root_causes)

    if "FORCED" in names:
        if root_causes:
            summary += ", escalated to a hard fault"
        else:
            summary = (
                "forced hard fault with no configurable fault bits set"
            )

    return summary or "fault bits set in an unrecognized pattern"
