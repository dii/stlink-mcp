#!/usr/bin/env python3
"""Survey STM32CubeProgrammer's device databases.

Every figure quoted in ROADMAP.md's Phase 2 comes from this script, and that is
the point of it existing rather than the numbers being pasted in from a shell
one-liner. The first version of those figures was produced by an ad-hoc regex
and was wrong: it reported "103 of 104 carry a named SRAM region" when the
answer is 104 of 104. The file it missed writes its address as "0x20004000U",
with a trailing U, so a strict hex match rejected it. The published number was a
measurement of the regex, not of the data.

So this parses the XML properly, prints the ambiguities rather than hiding them,
and can be re-run against any CubeProgrammer install:

    python3 tools/survey_device_db.py [path-to-Data_Base]
"""

from __future__ import annotations

import collections
import pathlib
import sys
import xml.etree.ElementTree as ET

DEFAULT_DB = "/opt/st/stm32cubeclt_1.22.0/STM32CubeProgrammer/Data_Base"


# NOTE for whoever writes the real seeder: this script compares addresses as
# STRINGS, which is safe here because it only asks whether they differ. A seeder
# has to parse them, and ST's own formatting is not consistent: at least one
# database writes an address as "0x20004000U" with a trailing C-style suffix,
# and at least two write a size as bare hex with no "0x" prefix. Both shapes are
# in the output below. A strict int(x, 16) rejects the first and misreads
# nothing in the second only by luck.


def survey(db_dir: pathlib.Path) -> dict:
    files = sorted(db_dir.glob("STM32_Prog_DB_0x*.xml"))
    by_id: dict[str, list[str]] = collections.defaultdict(list)
    sram_present = flash_present = 0
    sram_ambiguous: list[tuple[str, list]] = []
    sram_addr_ambiguous: list[tuple[str, list]] = []
    flash_ambiguous: list[tuple[str, list]] = []
    series: set[str] = set()
    unparsable: list[str] = []

    for path in files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            unparsable.append(path.name)
            continue

        device_id = (root.findtext(".//DeviceID") or "?").lower()
        by_id[device_id].append(path.name)
        series.add(root.findtext(".//Series") or "?")

        srams = {
            (p.get("address"), p.get("size"))
            for p in root.iter("Parameters")
            if p.get("name") == "SRAM" and p.get("address") and p.get("size")
        }
        if srams:
            sram_present += 1
        # Split the two ambiguities apart, because only one of them matters for
        # seeding. ram_load_base needs the ADDRESS. A device offering one
        # address with several sizes is a die or package variant and is not
        # ambiguous for our purposes; a device offering several addresses is,
        # and picking one is a guess. Quoting the combined count would repeat
        # the error this script exists to stop: a number that measures
        # something other than the question being asked.
        if len(srams) > 1:
            sram_ambiguous.append((path.name, sorted(srams)))
        if len({address for address, _ in srams}) > 1:
            sram_addr_ambiguous.append(
                (path.name, sorted({address for address, _ in srams}))
            )

        flashes = {
            p.get("address")
            for p in root.iter("Parameters")
            if "flash" in (p.get("name") or "").lower()
            and (p.get("address") or "").startswith("0x0800")
        }
        if flashes:
            flash_present += 1
        if len(flashes) > 1:
            flash_ambiguous.append((path.name, sorted(flashes)))

    return {
        "files": len(files),
        "unparsable": unparsable,
        "distinct_device_ids": len(by_id),
        "duplicate_ids": {k: v for k, v in by_id.items() if len(v) > 1},
        "series": len(series),
        "sram_present": sram_present,
        "sram_ambiguous": sram_ambiguous,
        "sram_addr_ambiguous": sram_addr_ambiguous,
        "flash_present": flash_present,
        "flash_ambiguous": flash_ambiguous,
    }


def main() -> int:
    db_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
    if not db_dir.is_dir():
        print(f"No device database directory at {db_dir}", file=sys.stderr)
        return 1

    r = survey(db_dir)
    print(f"database dir            {db_dir}")
    print(f"files                   {r['files']}")
    print(f"distinct device IDs     {r['distinct_device_ids']}")
    print(f"duplicate device IDs    {r['duplicate_ids'] or 'none'}")
    print(f"distinct series         {r['series']}")
    print(f"with a named SRAM       {r['sram_present']}")
    print(f"  >1 (address,size)     {len(r['sram_ambiguous'])}  (size differs = die variant, harmless for a base address)")
    print(f"  >1 ADDRESS            {len(r['sram_addr_ambiguous'])}  <-- the ones seeding cannot resolve")
    for name, values in r["sram_addr_ambiguous"]:
        print(f"    {name}: {values}")
    print(f"with 0x0800 flash base  {r['flash_present']}")
    print(f"  AMBIGUOUS (>1 base)   {len(r['flash_ambiguous'])}")
    if r["unparsable"]:
        print(f"unparsable              {r['unparsable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
