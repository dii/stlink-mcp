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


def elf32_segments(*segments: tuple[int, int] | tuple[int, int, int]) -> bytes:
    """An ELF32 little-endian image with one PT_LOAD per (paddr, filesz[, memsz]).

    For images a single segment cannot describe: a RAM vector table beside a
    segment linked for flash, or a zero-filesz segment that writes nothing."""
    ehdr_size, phdr_size = 52, 32
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\x00" * 8
    ehdr = ident + struct.pack("<HHIIIIIHHHHHH",
                               2, 40, 1, segments[0][0], ehdr_size, 0, 0x05000000,
                               ehdr_size, phdr_size, len(segments), 0, 0, 0)
    offset = ehdr_size + phdr_size * len(segments)
    phdrs, body = b"", b""
    for segment in segments:
        paddr, filesz = segment[0], segment[1]
        memsz = segment[2] if len(segment) > 2 else filesz
        phdrs += struct.pack("<IIIIIIII", 1, offset + len(body), paddr, paddr,
                             filesz, memsz, 5, 4)
        body += bytes(filesz)
    return ehdr + phdrs + body
