"""Per-family target quirks.

Kept as data rather than scattered through the code, because the whole reason
this project exists is that these details are non-obvious, easy to lose, and
produce confidently wrong behaviour when wrong. A new family should be one entry
here, not a patch to the tool logic.

Adding a family by hand: fill in what you have verified on hardware, and leave
the rest out. A wrong value here is worse than a missing one, because a missing
one fails loudly and a wrong one is exactly the failure mode this project is
trying to remove.

A value that did NOT come off a board is allowed, but only if it says so. Every
field carries a provenance, one of `measured`, `vendor-db`, `user-stated` or
`assumed`, and an entry written here defaults to `measured` because that is what
a hand-written entry is. A seeded or user-supplied entry sets its own default,
and the tools treat the difference as real: see `provenance_of` below and N6 in
AGENTS.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields

#: Where a value came from. Not a quality ranking: `measured` means somebody ran
#: it on a board, and the other three mean nobody has.
MEASURED = "measured"
VENDOR_DB = "vendor-db"
USER_STATED = "user-stated"
ASSUMED = "assumed"
"""Nobody measured it and no source states it: it is this project's default.

The roadmap named three provenances and the fourth appeared as soon as the model
was applied to real data. `DEFAULT` carries access port 0 and no signing, and
both are assumptions about the STM32 range rather than facts about any part. The
first version of this code labelled them `measured`, which is precisely the lie
N6 exists to stop, produced by the mechanism built to stop it.

It matters most for access port. A seeded entry's port is `assumed`, never
measured, and that is the signal the scan needs: an assumed port must not be
trusted the way a measured one is.
"""
PROVENANCES = frozenset({MEASURED, VENDOR_DB, USER_STATED, ASSUMED})


class ProvenanceError(ValueError):
    """An entry labelled its own values with something that is not a provenance,
    or labelled a field that does not exist."""


@dataclass(frozen=True)
class FamilyQuirks:
    """What differs between STM32 families for programming and running."""

    name: str

    access_port: int
    """Debug access port. Nearly every STM32 uses 0.

    The STM32N6 uses 1, and getting this wrong does not produce a clean error:
    ST-LINK_gdbserver reports "Failed to halt target" and
    STM32_Programmer_CLI's -s reports "Start operation achieved successfully"
    while leaving the PC in the boot ROM. That pair of lies is the single most
    expensive thing this table prevents.
    """

    ram_load_base: int | None = None
    """Where a RAM-executed image is placed, for parts with no internal flash."""

    vector_table_addr: int | None = None
    """Address of the loaded image's vector table.

    Word 0 is the initial stack pointer and word 1 the reset vector. Taking SP
    and PC from here is more reliable than trusting a debugger's idea of the
    entry point, and it is immune to the image growing.
    """

    flash_base: int | None = None
    """Base address of the memory a standalone image is programmed into."""

    external_loader: str | None = None
    """External loader file name, for parts whose boot memory is off-chip.

    Resolved against STM32CubeProgrammer's ExternalLoader directory.
    """

    requires_signing: bool | None = None
    """Whether the boot ROM refuses an unsigned image. None means nobody knows.

    Note this applies to the flash path only. An image loaded straight into RAM
    by a debugger bypasses the boot ROM and needs no signature.

    Tri-state rather than a bool defaulting to False, because "we checked and it
    does not need signing" and "nobody has said" are different facts and the
    provenance model cannot tell them apart otherwise: a bool that defaults to
    False is reported with a provenance for a claim the entry never made. The
    vendor device database says nothing about signing, so every seeded entry
    leaves this None.
    """

    debug_closes_on_run: bool | None = None
    """Whether firmware typically leaves the debug access port unreachable.

    READ THIS BEFORE TRUSTING THE FLAG. It sits in a table of family quirks and
    it is not one. It describes what the IMAGE does, not what the silicon does,
    and it is here only because on the STM32N6 the overwhelmingly common image
    links a vendor template that causes it. A user whose image does not is
    inconvenienced by a warning that does not apply to them, which is the lesser
    of the two errors available and still an error.

    On the STM32N6 the cause is a single line at the end of ST's
    `system_stm32n6xx_fsbl.c` SystemInit:

        RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;   /* deactivate SYSCFG clock */

    SystemInit enables the SYSCFG clock early so it can write INITSVTORCR, then
    switches it off again on the way out. That clock is what keeps debug access
    port 1 alive, so gating it takes the debug port with it. Keep it enabled and
    the port survives: measured over four consecutive load-and-run cycles with
    no power cycle, against the stock image closing it on the very next run of
    the same board.

    So the honest reading of this flag is "images for this family usually close
    the debug port, and here is what to change". It is NOT "this part cannot be
    re-acquired". Everything else was eliminated by bisection: the boot ROM
    handoff, the startup assembly, TrustZone/SAU, SCB_NS->CPACR, INITSVTORCR,
    the PWR supply and VDDIOxCCCR compensation block, SCB->VTOR, the RNG, XSPI
    and TIM2 resets, the GPIOG clock disable, VREFBUF, and SCB->CPACR.

    While an image DOES gate that clock, the behaviour is exactly as bad as it
    looks: a new connection fails, no reset clears it, and only removing power
    gets the port back. That is why the server is held rather than torn down
    between runs, which is what runner.acquire_server does, and why releasing it
    is a one-way door until the board is power cycled.

    So the flag drives the right behaviour for an unpatched image. It is the
    explanation that changed, not the workaround: this is a cost imposed by the
    image and removable in the image, not a limit of the part.

    Holding the server is necessary and NOT sufficient. Every load must also be
    preceded by an explicit reset, because the server's own `--halt` resets the
    target once, when the server starts. Without that, each image after the first
    inherits the previous run's live state: configured peripherals, enabled NVIC
    lines, and an external device still asserting its interrupt. It then starts at
    Reset_Handler with that interrupt pending, vectors through a handler pointer
    in .bss that has not been zeroed yet, and faults during start-up. The symptom
    is a board that prints its first couple of bytes and stops, which reads as a
    firmware bug and cost an evening as one. `runner._GDB_SCRIPT` carries the
    reset; do not remove it on the grounds that `--halt` already does it.
    """

    notes: str = ""

    provenance: Mapping[str, str] = field(default_factory=dict)
    """Per-field override of where a value came from, keyed by field name.

    Only fields that differ from `default_provenance` need an entry, which keeps
    a hand-written table readable: a fully measured entry names nothing here.
    """

    default_provenance: str = MEASURED
    """Provenance for every field not named in `provenance`.

    It defaults to `measured` because that is what a hand-authored entry in this
    file is, and the contribution rule above says so. A seeder or a user-supplied
    entry must set it, and then any individual field it did measure can be
    promoted through `provenance`.
    """

    def __post_init__(self) -> None:
        """Reject a mislabelled entry at construction rather than at use.

        The typo check is the one that earns its place. A provenance key naming
        a field that does not exist looks harmless and is not: the real field
        then silently falls back to `default_provenance`, so a value nobody
        measured gets reported as measured. That is the exact harm N6 exists to
        prevent, arriving through the mechanism meant to prevent it.
        """
        known = {f.name for f in fields(self)}
        for key, value in self.provenance.items():
            if key not in known:
                raise ProvenanceError(
                    f"{self.name}: provenance names {key!r}, which is not a field "
                    f"of FamilyQuirks. Known fields: {', '.join(sorted(known))}."
                )
            if value not in PROVENANCES:
                raise ProvenanceError(
                    f"{self.name}: provenance for {key!r} is {value!r}, which is "
                    f"not one of {', '.join(sorted(PROVENANCES))}."
                )
        if self.default_provenance not in PROVENANCES:
            raise ProvenanceError(
                f"{self.name}: default_provenance is {self.default_provenance!r}, "
                f"which is not one of {', '.join(sorted(PROVENANCES))}."
            )

    def provenance_of(self, field_name: str) -> str:
        return self.provenance.get(field_name, self.default_provenance)

    def is_measured(self, field_name: str) -> bool:
        """Whether somebody has actually run this value on a board.

        The question the tools ask, rather than which of the three labels it
        carries, because `vendor-db` and `user-stated` differ in where the guess
        came from and not in whether it is one.
        """
        return self.provenance_of(field_name) == MEASURED

    def provenance_summary(self) -> dict[str, str]:
        """Provenance of every field that holds a value, for reporting.

        Fields that are None are omitted: there is no provenance for an absent
        fact, and listing them would suggest the table knows something it does
        not.
        """
        return {
            f.name: self.provenance_of(f.name)
            for f in fields(self)
            if f.name not in ("name", "notes", "provenance", "default_provenance")
            and getattr(self, f.name) is not None
        }


#: Families verified on real hardware. Everything else falls back to DEFAULT.
FAMILIES: dict[str, FamilyQuirks] = {
    "STM32H7": FamilyQuirks(
        name="STM32H7",
        access_port=0,
        ram_load_base=0x24000000,
        vector_table_addr=0x24000000,
        flash_base=0x08000000,
        requires_signing=False,
        debug_closes_on_run=False,
        notes=(
            "The ordinary shape, and it is in this table mainly to prove the "
            "table describes a family rather than describing the STM32N6. It "
            "inverts the N6 on every axis: internal flash instead of none, core "
            "debug on AP0 instead of AP1, no signing, no external loader, and a "
            "debug port that stays open while firmware runs. "
            "Note the vector table sits AT the RAM load base here, while on the "
            "N6 it is 0x400 into it because the signing header is padded to that "
            "alignment. The two fields are genuinely independent, which is only "
            "visible once a second family exists. "
            "Verified on NUCLEO-H743ZI (device 0x450, Cortex-M7, Rev V, "
            "ST-LINK/V3E firmware V3J7M2) on 2026-08-15. Identification and "
            "memory reads succeed on AP0 with a hotplug connect, repeatedly and "
            "with firmware running. 0x08000000 holds a plain Cortex-M vector "
            "table. On that board the resident image's initial SP was 0x20020000, "
            "the top of the 128 KB DTCM, with a reset handler at 0x0807FA99, and "
            "THAT PAIR IS A PROPERTY OF WHATEVER IMAGE IS FLASHED, not of the "
            "family. The H753I board below has SP 0x24004000, in AXI SRAM "
            "instead, from a different vendor image. Neither number belongs in "
            "the fields above and neither is checked against; they are recorded "
            "only as what a healthy vector table looked like. This table has "
            "already carried one image property mistaken for a silicon one, and "
            "it cost two days (see debug_closes_on_run). "
            "RAM execution was verified by running "
            "a purpose-built image from AXI SRAM that increments a counter and "
            "touches no peripheral: read over the debugger, the counter advanced "
            "between two samples while the PC sat in its delay loop. That is "
            "direct observation of the core rather than console output, which is "
            "the right evidence here since a bare RAM image has no console until "
            "someone configures one. "
            "AXI SRAM was chosen over DTCM at 0x20000000 and SRAM1-4 from "
            "0x30000000 because it is the largest contiguous region (512 KB) and "
            "is reachable by every bus master. DTCM is faster and would also "
            "work for a small image; it is not recorded because it was not tried. "
            "CONFIRMED ON A SECOND BOARD AND A DIFFERENT PART, 2026-08-16: an "
            "STM32H753I-EVAL2, which the probe calls STM32H753I-EV, on ST-LINK/V3E "
            "firmware V3J3M2. This is what turns the entry from a description of "
            "one board into a description of a family, and it was tested the way "
            "that claim deserves: the NUCLEO-H743ZI's own beacon image was loaded "
            "UNMODIFIED, driven entirely by the values in this entry, AP0 and a "
            "vector table at 0x24000000. It ran. The counter advanced across three "
            "samples (0xC10003EB, 0xC100043A, 0xC1000488, all 0xC0FFEE00 plus a "
            "rising count), and hotplug connects kept succeeding throughout, which "
            "is debug_closes_on_run=False measured a second time. "
            "Note the programmer cannot tell these two parts apart at all: ST's own "
            "STM32_Prog_DB_0x450.xml names 0x450 'STM32H7xx' in series 'STM32H7', so "
            "an H743 and an H753 arrive here as the same device ID by design. That "
            "is the granularity of this table. It is keyed on what the tool can "
            "actually report, not on the part number printed on the chip."
        ),
    ),
    "STM32N6": FamilyQuirks(
        name="STM32N6",
        access_port=1,
        ram_load_base=0x34180000,
        vector_table_addr=0x34180400,
        flash_base=0x70000000,
        external_loader="MX25UM51245G_STM32N6570-NUCLEO.stldr",
        requires_signing=True,
        debug_closes_on_run=True,
        notes=(
            "No internal flash. The boot ROM reads a signed FSBL out of external "
            "XSPI flash and loads it into AXISRAM2 at the vector table address. "
            "Core debug is on access port 1, and the trap is that AP0 still "
            "reports device ID, name, CPU and revision perfectly well while being "
            "unable to halt the core, so identifying the target does not tell you "
            "the port is right. The debug port is only open in DEV boot mode "
            "(BOOT1 high); in the standalone boot configuration a connect fails "
            "with 'Unable to get core ID', which is expected and not a fault. "
            "ST'S OWN SystemInit CLOSES AP1, and it is one line. At the end of "
            "system_stm32n6xx_fsbl.c: `RCC->APB4ENCR2 = RCC_APB4ENCR2_SYSCFGENC;`. "
            "SystemInit turns the SYSCFG clock on early to write INITSVTORCR and "
            "turns it off again on the way out; that clock keeps AP1 alive, so "
            "gating it takes the debug port too. Remove that one write and the "
            "port survives indefinitely. Measured 2026-08-16 by bisection over "
            "SystemInit, ending in a back-to-back A/B on ONE board: four "
            "consecutive load-and-run cycles with the patched image all left AP1 "
            "open, then a single run of the stock image closed it. Eliminated "
            "along the way, each by its own build: the ROM handoff (a bare image "
            "with no startup chain leaves AP1 open), the startup assembly, "
            "TrustZone/SAU, SCB_NS->CPACR, INITSVTORCR, the PWR supply-valid and "
            "VDDIOxCCCR compensation block, SCB->VTOR, the RNG/XSPI/TIM2 resets, "
            "the GPIOG clock disable, VREFBUF and SCB->CPACR. "
            "Once a stock image HAS run, nothing short of removing power gets AP1 "
            "back: hotplug, NORMAL, HWRSTPULSE and a freshly started GDB server "
            "all fail, and UR is not usable on this part at all since it fails on "
            "a healthy board too. An "
            "already "
            "established GDB server connection keeps working across repeated "
            "load-and-run cycles, but ONLY with an explicit reset before each "
            "load: --halt resets once at server start, and without that reset "
            "every later image inherits the previous run's live interrupt state "
            "and faults during start-up. Measured, including a negative result: it is "
            "NOT caused by the SAU configuration in ST's FSBL SystemInit, since "
            "removing those writes changes nothing (that code never enables the SAU "
            "in the first place). Verified on NUCLEO-N657X0-Q (MB1940C)."
        ),
    ),
}

DEFAULT = FamilyQuirks(
    name="generic",
    access_port=0,
    requires_signing=False,
    default_provenance=ASSUMED,
    notes=(
        "Fallback for families not yet characterised here. Access port 0 and no "
        "signing is right for most STM32 parts with internal flash. RAM execution "
        "and external-flash programming are not offered, because the addresses "
        "are unknown rather than assumed."
    ),
)


#: Device IDs as reported by STM32_Programmer_CLI, mapped to a family key.
DEVICE_ID_TO_FAMILY: dict[int, str] = {
    0x450: "STM32H7",
    0x486: "STM32N6",
}


def for_device_id(device_id: int | None) -> FamilyQuirks:
    """Quirks for a device ID, or the generic fallback if it is unknown."""
    if device_id is None:
        return DEFAULT
    return FAMILIES.get(DEVICE_ID_TO_FAMILY.get(device_id, ""), DEFAULT)


# There was a for_name() lookup here. Nothing called it: a target is identified
# by the device ID the probe reports, never by a name someone types, so
# for_device_id above is the only entry point the code has ever needed. It was
# removed rather than kept as convenience, because an exported lookup implies a
# supported way in, and this one led nowhere.
