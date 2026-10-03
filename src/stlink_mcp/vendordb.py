"""Seeding the quirks table from STMicroelectronics' own device databases.

STM32CubeProgrammer ships one XML per device ID under `Data_Base/`, and two of
the four fields this project needs are in there: where SRAM starts and where
internal flash starts. That is the difference between refusing to run firmware on
almost every STM32 and refusing only where something is genuinely unknown.

Nothing here produces a `measured` value. Every entry comes out labelled
`vendor-db`, except the access port, which is not in the databases at all and
comes out `assumed`. What the label changes is real: `flash_firmware` will not
write to an unmeasured base unless the caller names the address, and
`board_state` reports an unmeasured access port as a warning.

THREE THINGS THE DATA DOES NOT DO, all measured rather than assumed, and all
handled here by refusing rather than picking:

1. One device ID appears in two files. `0x485` is declared by both
   `STM32_Prog_DB_0x485.xml` and `..._0x485_swv.xml`. A glob into a dict would
   resolve that by file order, which is nobody's decision. They agree on the
   fields used here, so the collision is currently harmless, and it is checked
   rather than trusted.

2. SRAM is not always one region. 14 of the 103 device IDs offer more than one
   base address. Ten are the same memory seen twice, once at `0x2xxxxxxx` and
   once at `0x3xxxxxxx`, which is the TrustZone non-secure and secure view and
   differs by exactly `0x10000000`; the non-secure view is taken. The other four
   are separate banks and a real choice, so they are refused.

3. ST's own formatting is inconsistent. At least one file writes an address as
   `"0x20004000U"` with a trailing C suffix, and at least two write a size as
   bare hex with no `0x`. A strict `int(x, 16)` rejects the first and this
   project has already published one wrong count because of it.

The reverse mapping is also worth stating, because the one part this project
knows best contradicts the obvious reading. For the STM32N6 the database gives
SRAM at `0x34180400`, which is that family's VECTOR TABLE address and not its RAM
load base of `0x34180000`; the 0x400 is the signing header. Measured across every
SRAM base in the set, exactly one carries a 0x400 offset and it is the N6, with
106 of 119 bases perfectly round. So a seeded entry sets the load base and the
vector table to the same address, which is right for every part with no header,
and the N6 never reaches this code because it is characterised by hand.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .quirks import ASSUMED, VENDOR_DB, FamilyQuirks, check_consistency

#: The TrustZone secure alias of a memory sits exactly this far above it.
_SECURE_ALIAS_OFFSET = 0x10000000

#: Where internal flash is on the parts this can seed. Deliberately narrow: the
#: WB0x and WL3x families put theirs at 0x10040000 and are left with no flash
#: base rather than given a guessed one.
_FLASH_PREFIX = "0x0800"


class VendorDbError(RuntimeError):
    """The device database directory could not be used at all."""


@dataclass(frozen=True)
class SeedResult:
    """What a parse of the databases produced, INCLUDING what it could not use.

    The three fields exist separately because they are three different facts and
    a caller needs all of them. An earlier version returned the entries plus a
    dict of refusal reasons, and the reasons died at the first caller, which
    reduced them to a count: so a device deliberately refused for ambiguity was
    indistinguishable at runtime from a device ST has never heard of. Worse, an
    unparseable file was stashed under a sentinel key and then deleted three
    lines later, inside the function whose own docstring says a silently smaller
    set of supported parts is the same failure as a silently wrong address.
    """

    entries: dict[int, FamilyQuirks] = field(default_factory=dict)
    refusals: dict[int, str] = field(default_factory=dict)
    """Device IDs the databases describe and this refused to use, and why. Meant
    to be reachable from the tool that hits one, not just counted."""
    unreadable: dict[str, str] = field(default_factory=dict)
    """Files that matched the glob and could not be read or parsed, by name."""


def parse_hex(value: str | None) -> int | None:
    """Parse an address or size as ST actually writes them, or None.

    Tolerant of a trailing C-style integer suffix and of a bare hex string with
    no `0x`, both of which appear in the shipped databases. Being strict here is
    what produced this project's "103 of 104" miscount: the number measured the
    parser rather than the data.
    """
    if not value:
        return None
    text = value.strip().rstrip("uUlL")
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except ValueError:
        return None


def _sram_bases(root: ET.Element) -> set[int]:
    return {
        base
        for element in root.iter("Parameters")
        if element.get("name") == "SRAM"
        for base in (parse_hex(element.get("address")),)
        if base is not None
    }


def _flash_base(root: ET.Element) -> int | None:
    bases = {
        base
        for element in root.iter("Parameters")
        if "flash" in (element.get("name") or "").lower()
        and (element.get("address") or "").lower().startswith(_FLASH_PREFIX)
        for base in (parse_hex(element.get("address")),)
        if base is not None
    }
    # Measured unambiguous across all 104 files, so more than one here means the
    # data changed and the assumption needs re-checking, not averaging.
    return bases.pop() if len(bases) == 1 else None


def resolve_sram_base(bases: set[int]) -> tuple[int | None, str | None]:
    """The one SRAM base to seed, or None and the reason there isn't one."""
    if not bases:
        return None, "the database names no SRAM region"
    if len(bases) == 1:
        return next(iter(bases)), None

    ordered = sorted(bases)
    if len(ordered) == 2 and ordered[1] - ordered[0] == _SECURE_ALIAS_OFFSET:
        # Same physical memory, non-secure and secure views. The non-secure one
        # is the conventional address and the one every example links against.
        return ordered[0], None

    return None, (
        "the database names "
        + ", ".join(f"0x{b:08X}" for b in ordered)
        + " as SRAM, which are separate banks rather than one region seen twice, "
        "so choosing between them would be a guess"
    )


def seed_from_directory(db_dir: Path) -> SeedResult:
    """Parse every device database into a SeedResult.

    Everything that could not be used is reported rather than dropped, because a
    silently smaller set of supported parts is the same failure as a silently
    wrong address: the caller is entitled to know that a device ID was seen and
    skipped, and why.
    """
    files = sorted(db_dir.glob("STM32_Prog_DB_0x*.xml"))
    if not files:
        raise VendorDbError(
            f"No device databases under {db_dir}. This is normally "
            "STM32CubeProgrammer/Data_Base inside a CubeCLT install."
        )

    result = SeedResult()
    seeded, skipped, unreadable = result.entries, result.refusals, result.unreadable
    seen_files: dict[int, str] = {}
    resolved: dict[int, tuple[int | None, int | None]] = {}

    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            unreadable[path.name] = f"not parseable XML: {exc}"
            continue
        except OSError as exc:
            # NOT merged with the clause above, and not left to propagate. An
            # unreadable file raises PermissionError, which is an OSError and not
            # an ET.ParseError, so the narrower catch let it escape through
            # load_vendor_seed - which catches only ToolchainError and
            # VendorDbError - and out of main(), killing the server with a
            # traceback. That directly contradicted load_vendor_seed's own
            # promise that failing to seed is never fatal.
            unreadable[path.name] = f"{type(exc).__name__}: {exc}"
            continue

        device_id = parse_hex(root.findtext(".//DeviceID"))
        if device_id is None:
            continue
        series = (root.findtext(".//Series") or "").strip() or f"device {device_id:#x}"

        base, why_not = resolve_sram_base(_sram_bases(root))
        flash = _flash_base(root)

        if device_id in seen_files:
            # Two files, one device ID. Compare what each file RESOLVED TO,
            # rather than inferring disagreement from whether an entry exists.
            #
            # The first version compared against `seeded.get(device_id)`, which
            # is absent whenever the first file was unusable for any reason. So
            # a clean first file followed by an ambiguous second one deleted the
            # good entry and recorded "which disagree about the addresses to
            # use" - a comparison the code had not performed. Reproduced with
            # two synthetic databases sharing one device ID.
            first = resolved.get(device_id)
            if first == (base, flash):
                continue  # genuinely the same, nothing to decide
            if base is None:
                # This file is unusable and the other one was fine. That is not a
                # disagreement, and the usable entry stays.
                continue
            if first is not None and first[0] is None:
                # The other way round: the first file was unusable, this one is
                # not, so take this one.
                resolved[device_id] = (base, flash)
                seen_files[device_id] = path.name
                skipped.pop(device_id, None)
            else:
                seeded.pop(device_id, None)
                skipped[device_id] = (
                    f"declared by both {seen_files[device_id]} and {path.name} "
                    f"with different addresses "
                    f"(SRAM {_show(first[0] if first else None)} against "
                    f"{_show(base)}, flash {_show(first[1] if first else None)} "
                    f"against {_show(flash)}), so choosing between the files "
                    "would be a guess"
                )
                continue
        else:
            seen_files[device_id] = path.name
            resolved[device_id] = (base, flash)

        if base is None:
            skipped[device_id] = why_not or "no usable SRAM base"
            continue

        entry = FamilyQuirks(
            name=series,
            access_port=0,
            ram_load_base=base,
            # Same address for both. Right for every part with no signing header,
            # and the one part in the set that has one is characterised by hand
            # and never reaches here.
            vector_table_addr=base,
            flash_base=flash,
            notes=(
                f"Seeded from STMicroelectronics' own device database for "
                f"{device_id:#x} ({series}), not measured by anyone. The SRAM "
                f"base and the flash base come from that file; the vector table "
                f"is assumed to sit at the RAM base, which is true for a part "
                f"with no signing header. The access port is NOT in the "
                f"database: 0 is this project's assumption for the STM32 range "
                f"and board_state says so. Signing, external loaders and "
                f"whether an image closes the debug port are not in the database "
                f"either and are left unknown. Confirm anything here on a board "
                f"before relying on it."
            ),
            default_provenance=VENDOR_DB,
            provenance={"access_port": ASSUMED},
            agent_note=(
                "Seeded from ST's device database, not measured on a board: the addresses "
                "come from that file and access port 0 is assumed. Confirm them on a board "
                "before relying on them."
            ),
        )

        problems = check_consistency(entry)
        if problems:
            skipped[device_id] = "; ".join(problems)
            continue

        seeded[device_id] = entry

    return result


def _show(value: int | None) -> str:
    return "none" if value is None else f"0x{value:08X}"
