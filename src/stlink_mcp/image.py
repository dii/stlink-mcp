"""What an ELF says about where it wants to be loaded.

Pure logic, no hardware. `run_firmware` hands GDB an ELF and says `load`, and GDB
writes every loadable segment to the address the ELF itself carries. If that
address is flash, the GDB server programs flash, which is a persistent write this
tool promises not to make on the run path, and the core is then started from
whatever bytes sit at the family's RAM vector table. So before any load, the
image's loadable segments are read here. One of them must cover the vector table
address the family records, and every one that writes anything must lie inside
the SRAM region. Cortex-M images are always ELF32 little-endian, which is all
this reads.

The second rule came later than the first. The first version checked only that
some segment covered the vector table, so an image with its vector table in RAM
and another segment linked for flash passed, and GDB then programmed that
segment: the RT1 failure, closed for one image shape and open for the next.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

_PT_LOAD = 1

#: The Cortex-M architectural SRAM region, as a half-open range. No STM32 maps
#: flash here: internal flash is in the code region below it (0x08000000, or
#: 0x10040000 on STM32WB0x and WL3x, aliased at 0x00000000 when booting from it)
#: and external flash behind XSPI, OCTOSPI or FMC is at 0x60000000 and above. A
#: load confined to this window therefore cannot program flash. RAM outside it
#: (ITCM at 0x00000000, external SDRAM) is refused too, because from the image
#: alone this cannot be told apart from flash.
SRAM_REGION = (0x20000000, 0x40000000)


class ImageError(ValueError):
    """The file is not an ELF32 image this tool can reason about."""


@dataclass(frozen=True)
class Segment:
    """One PT_LOAD program header: where it is written and how much of it."""
    paddr: int
    vaddr: int
    filesz: int
    memsz: int

    def covers(self, address: int) -> bool:
        """True when `address` lies inside the bytes this segment writes.

        A segment with no file contents writes nothing, so it covers nothing. It
        used to count as covering its first address, which let a .bss-only
        segment stand in for a vector table the image does not contain."""
        return self.paddr <= address < self.paddr + self.filesz

    def outside_sram(self) -> bool:
        """True when this segment writes any byte outside SRAM_REGION."""
        low, high = SRAM_REGION
        return self.filesz > 0 and not (low <= self.paddr and self.paddr + self.filesz <= high)


def load_segments(data: bytes) -> list[Segment]:
    """The PT_LOAD segments of an ELF32 little-endian image, in file order.

    Raises ImageError for anything that is not such an image: no ELF magic,
    ELF64, big-endian, or program headers that do not fit in the file.
    """
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise ImageError("not an ELF file (no ELF magic)")
    if data[4] != 1:
        raise ImageError("not a 32-bit ELF (Cortex-M images are ELF32)")
    if data[5] != 1:
        raise ImageError("not a little-endian ELF (Cortex-M images are)")
    e_phoff, = struct.unpack_from("<I", data, 0x1C)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 0x2A)
    if e_phentsize < 32 or e_phoff + e_phnum * e_phentsize > len(data):
        raise ImageError("program headers do not fit in the file; truncated or not an ELF")
    segments: list[Segment] = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, _p_offset, p_vaddr, p_paddr, p_filesz, p_memsz = struct.unpack_from("<IIIIII", data, off)
        if p_type == _PT_LOAD:
            segments.append(Segment(paddr=p_paddr, vaddr=p_vaddr, filesz=p_filesz, memsz=p_memsz))
    return segments


def check_ram_image(path: Path, vector_table_addr: int) -> str | None:
    """None when a loadable segment covers the family's vector table address and
    every segment that writes anything lies in SRAM_REGION, else the reason the
    image must not be loaded, in plain words."""
    try:
        segments = load_segments(path.read_bytes())
    except OSError as exc:
        return f"Cannot read {path}: {exc}"
    except ImageError as exc:
        return f"{path.name}: {exc}. run_firmware loads ELF32 images only."
    if not segments:
        return f"{path.name} has no loadable segments; there is nothing to run."
    if not any(s.covers(vector_table_addr) for s in segments):
        starts = ", ".join(f"0x{s.paddr:08X}" for s in segments[:4])
        return (
            f"{path.name} is not linked for this family's RAM: no loadable segment "
            f"covers the vector table address 0x{vector_table_addr:08X}; its segments "
            f"start at {starts}. A GDB load writes each segment to the address in the "
            "image, so a flash-linked build (typically 0x08000000) would be programmed "
            "into flash and the core then started from whatever sits at the RAM vector "
            "table. Link the image for RAM with its vector table at that address, or "
            "use flash_firmware for a flash image."
        )
    outside = [s for s in segments if s.outside_sram()]
    if outside:
        listed = ", ".join(f"0x{s.paddr:08X} ({s.filesz:#x} bytes)" for s in outside[:4])
        return (
            f"{path.name} has its vector table in RAM but also a loadable segment "
            f"outside the SRAM region 0x{SRAM_REGION[0]:08X}-0x{SRAM_REGION[1] - 1:08X}: "
            f"{listed}. A GDB load writes every segment to the address in the image, "
            "and outside that region on an STM32 the address can be flash, so this "
            "refuses rather than risk programming it. Link every loaded section into "
            "RAM. If that segment really is RAM, such as ITCM at 0x00000000 or "
            "external SDRAM, this tool cannot tell it from flash."
        )
    return None
