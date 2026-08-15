"""Per-family target quirks.

Kept as data rather than scattered through the code, because the whole reason
this project exists is that these details are non-obvious, easy to lose, and
produce confidently wrong behaviour when wrong. A new family should be one entry
here, not a patch to the tool logic.

Adding a family: only fill in what you have actually verified on hardware. A
wrong value here is worse than a missing one, because a missing one fails loudly
and a wrong one is exactly the failure mode this project is trying to remove.
"""

from __future__ import annotations

from dataclasses import dataclass


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

    requires_signing: bool = False
    """Whether the boot ROM refuses an unsigned image.

    Note this applies to the flash path only. An image loaded straight into RAM
    by a debugger bypasses the boot ROM and needs no signature.
    """

    debug_closes_on_run: bool = False
    """Whether the debug access port cannot be RE-acquired once firmware has run.

    The precise behaviour matters, and a first reading of it was too pessimistic.
    A *new* connection fails once firmware has started, and no reset clears it;
    only removing power does. But an *already established* GDB server connection
    keeps working indefinitely, including repeated load-and-run cycles.

    So this flag does not mean "one run per power cycle". It means the server must
    be kept alive rather than torn down between runs, which is what
    runner.acquire_server does.

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


#: Families verified on real hardware. Everything else falls back to DEFAULT.
FAMILIES: dict[str, FamilyQuirks] = {
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
            "Once firmware has run, AP1 cannot be re-acquired. Re-measured "
            "2026-08-15 with nothing holding the probe, because an earlier run of "
            "this test was invalid (a controller still held it): a hotplug connect "
            "fails, a RESETTING connect fails, and a freshly started GDB server "
            "fails with 'Failed to connect to device', while the same tooling "
            "reaches a NUCLEO-H743ZI on the same hub. Whether removing power "
            "restores it is the one part not yet re-measured. An already "
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
    notes=(
        "Fallback for families not yet characterised here. Access port 0 and no "
        "signing is right for most STM32 parts with internal flash. RAM execution "
        "and external-flash programming are not offered, because the addresses "
        "are unknown rather than assumed."
    ),
)


#: Device IDs as reported by STM32_Programmer_CLI, mapped to a family key.
DEVICE_ID_TO_FAMILY: dict[int, str] = {
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
