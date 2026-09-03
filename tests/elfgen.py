"""A minimal ELF32 little-endian image for tests: one PT_LOAD segment at a chosen
physical address. Built by hand from the System V ELF32 layout so the tests do not
depend on a toolchain, and small enough to read in one sitting."""

import struct


def minimal_elf32(load_paddr: int, size: int = 0x100, *, ei_class: int = 1, ei_data: int = 1) -> bytes:
    ehdr_size, phdr_size = 52, 32
    ident = b"\x7fELF" + bytes([ei_class, ei_data, 1, 0]) + b"\x00" * 8
    e_phoff = ehdr_size
    ehdr = ident + struct.pack("<HHIIIIIHHHHHH",
                               2, 40, 1, load_paddr, e_phoff, 0, 0x05000000,
                               ehdr_size, phdr_size, 1, 0, 0, 0)
    p_offset = ehdr_size + phdr_size
    phdr = struct.pack("<IIIIIIII", 1, p_offset, load_paddr, load_paddr, size, size, 5, 4)
    return ehdr + phdr + bytes(size)
