"""Registers whose read changes the target's state, by peripheral block.

A debugger read is not passive on every register. Reading a UART's receive
register takes the character out of it and clears the flag the firmware waits
on, so the firmware never sees that character; reading an ADC's data register
clears "conversion done". ST's SVD files carry a field for this, readAction,
and 14 of the 233 files in STM32CubeCLT 1.22.0 use it at all; the STM32H743
and STM32F407 files mark nothing, and no file marks a UART, SPI, I2C or ADC
data register (counted 2026-10-03). The CMSIS headers say only read-only or
read-write.

So the list here is keyed by peripheral block, not by device. ST reuses a few
dozen blocks across the STM32 range and a block's registers keep their names,
so one entry for a block reaches every part with it: the eight entries below
match registers in 227 of the 233 files (2026-10-03). A block
is named by the SVD file's groupName, and a register by its name without the
peripheral's prefix ("RTC_TR" is TR).

Every entry says how it was established, with the labels quirks.py uses:
`measured` names the part, the peripheral and the date; `assumed` says what is
missing. A match on a part nobody measured is reported as a match by block and
name, which is what it is. Users add entries for their own parts in the board
file, as `user-stated`. ST's own marks are read from the user's install at run
time, never kept here, and spread by block in the same way.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .quirks import ASSUMED, MEASURED, USER_STATED, VENDOR_DB, BoardConfigError


@dataclass(frozen=True)
class ReadEffect:
    block: str
    """A regular expression on the SVD groupName, matched whole."""
    registers: tuple[str, ...]
    """Register names without the peripheral's prefix."""
    effect: str
    """What a read does, said so an agent can tell whether it matters."""
    provenance: str
    established: str
    """How it is known: measured where and when, or what is missing."""

    def matches(self, group: str | None, name: str) -> bool:
        return group is not None and re.fullmatch(self.block, group) is not None and name in self.registers


KNOWN: tuple[ReadEffect, ...] = (
    ReadEffect(
        r"USART|UART|LPUART", ("RDR",),
        "takes the oldest received character out of the receiver and clears RXNE, so the "
        "firmware never sees that character",
        MEASURED, "STM32H743 USART3, 2026-10-01 and 2026-10-03",
    ),
    ReadEffect(
        r"USART|UART", ("DR",),
        "takes the received character out of the receiver and clears RXNE, so the firmware "
        "never sees it; after a read of SR it also clears IDLE and the error flags",
        MEASURED, "STM32F407 UART5, 2026-10-03: RXNE, IDLE and FE cleared",
    ),
    ReadEffect(
        r"SPI|I2S", ("DR",),
        "takes the received data out of the receiver and clears RXNE",
        MEASURED, "STM32F407 SPI1, 2026-10-03",
    ),
    ReadEffect(
        r"SPI", ("RXDR",),
        "takes received data out of the receive FIFO",
        ASSUMED, "not measured: an STM32H743's SPI4, set up as master, did not complete a frame "
        "to measure with (2026-10-03); assumed from the older block's DR",
    ),
    ReadEffect(
        r"I2C", ("RXDR", "DR"),
        "takes the received byte out of the receiver and clears RXNE",
        ASSUMED, "not measured: receiving needs a second device on the bus, and none was wired "
        "(2026-10-03); assumed from the UART and SPI receivers",
    ),
    ReadEffect(
        r"ADC", ("DR",),
        "clears EOC, which the firmware may be waiting for to take the conversion",
        MEASURED, "STM32H743 ADC3 and STM32F407 ADC1, 2026-10-03",
    ),
    ReadEffect(
        r"RTC", ("SSR",),
        "holds TR and DR at their values until DR is read, across debugger connections, so "
        "the firmware's next reading of the time is stale",
        MEASURED, "STM32H743 RTC, 2026-10-03: TR held for 5 s after an SSR read, until DR was "
        "read; with BYPSHAD clear, its reset value",
    ),
    ReadEffect(
        r"RTC", ("TR",),
        "holds DR at its value until DR is read, so the firmware's next reading of the date "
        "can be stale",
        ASSUMED, "not measured: it shows only across midnight; assumed from SSR's measured hold "
        "of TR (2026-10-03)",
    ),
)

_user: tuple[ReadEffect, ...] = ()


def set_user_effects(effects: tuple[ReadEffect, ...]) -> None:
    global _user
    _user = effects


def base_name(name: str, peripheral: str, group: str | None) -> str:
    """The register's name without its peripheral's prefix: RTC_TR is TR."""
    upper = name.upper()
    for prefix in (peripheral, group or "", re.sub(r"\d+$", "", peripheral)):
        if prefix and upper.startswith(prefix.upper() + "_"):
            return upper[len(prefix) + 1:]
    return upper


def known(group: str | None, name: str) -> list[ReadEffect]:
    """The listed and user-stated effects of reading this register."""
    return [e for e in (*KNOWN, *_user) if e.matches(group, name)]


def effects_of(*, peripheral: str, name: str, group: str | None, own_mark: str | None,
               file_name: str, vendor: dict[tuple[str, str], tuple[str, str]]) -> list[ReadEffect]:
    """Everything known about reading one register: this list and the user's,
    the file's own readAction, or another file's for the same block."""
    base = base_name(name, peripheral, group)
    found = known(group, base)
    if own_mark:
        found.append(vendor_mark(file_name, own_mark, same_file=True))
    elif (group or "", base) in vendor:
        other, mark = vendor[(group or "", base)]
        found.append(vendor_mark(other, mark, same_file=False))
    return found


def vendor_mark(file_name: str, action: str, *, same_file: bool) -> ReadEffect:
    """ST's own readAction, from this file or another file with the same block."""
    where = f"ST's readAction in {file_name}" + ("" if same_file else ", another part's file, for the same block")
    return ReadEffect(".*", (), f"changes state, as ST's SVD file marks it ({action})", VENDOR_DB, where)


#: The Cortex-M address map's Peripheral and Device regions. A read anywhere
#: else, RAM, flash or the core's own registers, changes nothing.
DEVICE_REGIONS: tuple[tuple[int, int], ...] = ((0x40000000, 0x60000000), (0xA0000000, 0xE0000000))


def in_device_region(start: int, length: int) -> bool:
    end = start + length
    return any(start < high and end > low for low, high in DEVICE_REGIONS)


def describe(effect: ReadEffect) -> dict[str, str]:
    return {"effect": effect.effect, "provenance": effect.provenance, "established": effect.established}


_USER_KEYS = {"block", "registers", "effect"}


def load_user_effects(path: Path) -> tuple[ReadEffect, ...]:
    """The board file's [[read_effects]] entries, each `user-stated`:

        [[read_effects]]
        block = "USART"           # the SVD groupName, a regular expression
        registers = ["RDR"]       # names without the peripheral's prefix
        effect = "takes the received character and clears RXNE"

    Refused whole on anything else in an entry, as the board tables are: a
    setting the user believes they made must not be silently ignored."""
    try:
        raw = tomllib.loads(path.read_text())
    except OSError as exc:
        raise BoardConfigError(f"Cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BoardConfigError(f"{path} is not valid TOML: {exc}") from exc
    entries = raw.get("read_effects", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise BoardConfigError(f"{path}: read_effects must be written as [[read_effects]] tables.")
    out: list[ReadEffect] = []
    for number, entry in enumerate(entries, 1):
        where = f"{path}: read_effects entry {number}"
        unknown = set(entry) - _USER_KEYS
        if unknown or not _USER_KEYS <= set(entry):
            raise BoardConfigError(
                f"{where} has {', '.join(sorted(set(entry)))}; it takes exactly block, registers and "
                "effect. Everything in a board file is user-stated, so it cannot carry a provenance."
            )
        block, registers, effect = entry["block"], entry["registers"], entry["effect"]
        if not (isinstance(block, str) and isinstance(effect, str) and isinstance(registers, list)
                and registers and all(isinstance(r, str) for r in registers)):
            raise BoardConfigError(f"{where}: block and effect are strings, registers a list of names.")
        try:
            re.compile(block)
        except re.error as exc:
            raise BoardConfigError(f"{where}: block {block!r} is not a regular expression: {exc}") from exc
        out.append(ReadEffect(block, tuple(r.upper() for r in registers), effect, USER_STATED,
                              f"stated in {path}"))
    return tuple(out)
