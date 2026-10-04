"""Variables by name: address, size and type from the ELF's DWARF, decoded from
bytes read off the target.

The type information comes from the debug sections through pyelftools rather
than from GDB, because GDB without a target gives a variable's layout only as
text for a person, and parsing that is the fragility this project already pays
for with the programmer. Measured 2026-10-02: a 36-byte struct with an enum,
bitfields, an array, a union, a pointer and a nested struct, read from an
STM32H7 without halting it, decoded in full, at -O0, -O1, -O2 and -Os.

Nothing here touches the target. A name that is not found, matches more than
one variable, or does not live at a fixed address raises SymbolError, so a read
by name never guesses which bytes were meant.
"""

from __future__ import annotations

import difflib
import functools
import os
import struct
from dataclasses import dataclass
from typing import Any

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

#: Type qualifiers and aliases that change nothing about how bytes decode.
_TRANSPARENT = (
    "DW_TAG_typedef", "DW_TAG_volatile_type", "DW_TAG_const_type",
    "DW_TAG_restrict_type", "DW_TAG_atomic_type",
)

# DW_ATE_* base type encodings.
_ADDRESS, _BOOLEAN, _FLOAT, _SIGNED, _SIGNED_CHAR, _UNSIGNED, _UNSIGNED_CHAR = 1, 2, 4, 5, 6, 7, 8
_CHARS = (_SIGNED_CHAR, _UNSIGNED_CHAR)
_DW_OP_ADDR = 0x03


class SymbolError(ValueError):
    pass


@dataclass(frozen=True)
class Variable:
    name: str
    address: int
    size: int
    type_name: str
    scope: str
    """Where it is declared: the source file, and the function for a static local."""
    type_die: Any


def _name(die: Any) -> str | None:
    attr = die.attributes.get("DW_AT_name")
    if attr is None and "DW_AT_specification" in die.attributes:
        return _name(die.get_DIE_from_attribute("DW_AT_specification"))
    return attr.value.decode(errors="replace") if attr else None


def _strip(die: Any) -> Any:
    while die is not None and die.tag in _TRANSPARENT:
        die = die.get_DIE_from_attribute("DW_AT_type") if "DW_AT_type" in die.attributes else None
    return die


def _array_counts(die: Any) -> list[int]:
    counts = []
    for sub in die.iter_children():
        if sub.tag != "DW_TAG_subrange_type":
            continue
        if "DW_AT_count" in sub.attributes:
            counts.append(sub.attributes["DW_AT_count"].value)
        elif "DW_AT_upper_bound" in sub.attributes:
            counts.append(sub.attributes["DW_AT_upper_bound"].value + 1)
        else:
            raise SymbolError("an array without a bound has no size to read")
    return counts


def _size(die: Any) -> int:
    t = _strip(die)
    if t is None:
        raise SymbolError("a void type has no size")
    if "DW_AT_byte_size" in t.attributes:
        return t.attributes["DW_AT_byte_size"].value
    if t.tag == "DW_TAG_array_type":
        n = 1
        for count in _array_counts(t):
            n *= count
        return n * _size(t.get_DIE_from_attribute("DW_AT_type"))
    raise SymbolError(f"no size for a {t.tag}")


def type_name(die: Any) -> str:
    """A C-like name for a type, keeping the typedef a programmer wrote."""
    if die is None:
        return "void"
    if die.tag == "DW_TAG_typedef":
        return _name(die) or "typedef"
    if die.tag in ("DW_TAG_volatile_type", "DW_TAG_const_type"):
        inner = die.get_DIE_from_attribute("DW_AT_type") if "DW_AT_type" in die.attributes else None
        return f"{die.tag[7:-5]} {type_name(inner)}"
    if die.tag == "DW_TAG_pointer_type":
        inner = die.get_DIE_from_attribute("DW_AT_type") if "DW_AT_type" in die.attributes else None
        return f"{type_name(inner)} *"
    if die.tag == "DW_TAG_array_type":
        dims = "".join(f"[{n}]" for n in _array_counts(die))
        return f"{type_name(die.get_DIE_from_attribute('DW_AT_type'))}{dims}"
    kind = {"DW_TAG_structure_type": "struct ", "DW_TAG_union_type": "union ",
            "DW_TAG_enumeration_type": "enum "}.get(die.tag, "")
    return f"{kind}{_name(die) or '(anonymous)'}"


class _Index:
    """Every variable with a fixed address in one ELF, by name."""

    def __init__(self, path: str) -> None:
        self._file = open(path, "rb")  # noqa: SIM115 - the DIEs read from it lazily
        self.elf = ELFFile(self._file)
        if not self.elf.has_dwarf_info():
            raise SymbolError(f"{path} has no debug information; build it with -g to read variables by name.")
        self.by_name: dict[str, list[tuple[Any, int, str]]] = {}
        self.unplaced: dict[str, str] = {}
        for cu in self.elf.get_dwarf_info().iter_CUs():
            top = cu.get_top_DIE()
            unit = _name(top) or "?"
            self._walk(top, unit, None)

    def _walk(self, die: Any, unit: str, function: str | None) -> None:
        for child in die.iter_children():
            if child.tag == "DW_TAG_subprogram":
                self._walk(child, unit, _name(child))
            elif child.tag == "DW_TAG_lexical_block":
                self._walk(child, unit, function)
            elif child.tag == "DW_TAG_variable":
                name = _name(child)
                if not name or "DW_AT_declaration" in child.attributes:
                    continue
                scope = f"{unit}, in {function}()" if function else unit
                location = child.attributes.get("DW_AT_location")
                # An expression block (DW_FORM_exprloc, or DW_FORM_block* before DWARF 4); a
                # location list means the variable moves, so it has no one address.
                expr = location.value if location is not None and isinstance(location.value, list) else None
                if expr and expr[0] == _DW_OP_ADDR and len(expr) == 5:
                    self.by_name.setdefault(name, []).append((child, int.from_bytes(bytes(expr[1:5]), "little"), scope))
                elif function is None or location is not None:
                    self.unplaced.setdefault(name, scope)

    def first_code(self, length: int) -> tuple[int, bytes] | None:
        """The start of the first loadable segment that holds code."""
        for segment in self.elf.iter_segments():
            header = segment.header
            if header.p_type == "PT_LOAD" and header.p_flags & 1 and header.p_filesz:
                return header.p_paddr, segment.data()[:length]
        return None


@functools.lru_cache(maxsize=4)
def _index(path: str, mtime_ns: int, size: int) -> _Index:
    return _Index(path)


def _open(elf_path: str) -> _Index:
    try:
        stat = os.stat(elf_path)
    except OSError as exc:
        raise SymbolError(f"Cannot read {elf_path}: {exc.strerror}.") from exc
    try:
        return _index(os.path.abspath(elf_path), stat.st_mtime_ns, stat.st_size)
    except SymbolError:
        raise
    except Exception as exc:  # noqa: BLE001 - pyelftools raises several kinds
        raise SymbolError(f"{elf_path} could not be read as an ELF with DWARF: {exc}") from exc


def find_variable(elf_path: str, name: str) -> Variable:
    index = _open(elf_path)
    found = index.by_name.get(name, [])
    places = {address: (die, scope) for die, address, scope in found}
    if not places:
        if name in index.unplaced:
            raise SymbolError(
                f"{name} ({index.unplaced[name]}) has no fixed address: it lives in a "
                "register or on the stack, or the compiler optimised it away, so it "
                "cannot be read from memory by name."
            )
        near = difflib.get_close_matches(name, list(index.by_name), n=5)
        raise SymbolError(
            f"No variable named {name} in {elf_path}."
            + (f" Close names: {', '.join(near)}." if near else "")
        )
    if len(places) > 1:
        listing = "; ".join(f"0x{a:08X} in {scope}" for a, (_, scope) in sorted(places.items()))
        raise SymbolError(
            f"{name} names {len(places)} variables: {listing}. Read the one you mean by address."
        )
    address, (die, scope) = next(iter(places.items()))
    tdie = die.get_DIE_from_attribute("DW_AT_type")
    return Variable(name, address, _size(tdie), type_name(tdie), scope, tdie)


def first_code(elf_path: str, length: int = 64) -> tuple[int, bytes] | None:
    return _open(elf_path).first_code(length)


@dataclass(frozen=True)
class Function:
    name: str
    start: int
    size: int
    file: str | None
    """The source file the symbol table names for a static function; None for a
    global one, which the table does not tie to a file."""


def functions(elf_path: str) -> tuple[Function, ...]:
    """Every function in the ELF's symbol table, with its address range. Needs
    no debug information."""
    try:
        stat = os.stat(elf_path)
    except OSError as exc:
        raise SymbolError(f"Cannot read {elf_path}: {exc.strerror}.") from exc
    return _functions(os.path.abspath(elf_path), stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=4)
def _functions(path: str, mtime_ns: int, size: int) -> tuple[Function, ...]:
    found: list[Function] = []
    try:
        with open(path, "rb") as f:
            table = ELFFile(f).get_section_by_name(".symtab")
            if table is None:
                raise SymbolError(f"{path} has no symbol table; it was stripped.")
            file = None
            for sym in table.iter_symbols():
                kind, bind = sym["st_info"]["type"], sym["st_info"]["bind"]
                if kind == "STT_FILE":
                    file = sym.name
                elif kind == "STT_FUNC" and sym.name and sym["st_size"]:
                    found.append(Function(sym.name, sym["st_value"] & ~1, sym["st_size"],
                                          file if bind == "STB_LOCAL" else None))
    except SymbolError:
        raise
    except Exception as exc:  # noqa: BLE001 - pyelftools raises several kinds
        raise SymbolError(f"{path} could not be read as an ELF: {exc}") from exc
    return tuple(found)


@functools.lru_cache(maxsize=16)
def inlined_at(elf_path: str, function: str) -> tuple[int, ...]:
    """Where the compiler copied `function` into its callers: each inlined
    copy's entry address, from DW_TAG_inlined_subroutine.

    GDB's breakpoint on a function name goes on its out-of-line copy only: for
    an STM32N6 image whose `delay` was inlined into its one caller, at an
    address nothing called, and run_until reported "not reached" while the loop
    ran (2026-10-04). Empty for an ELF without DWARF."""
    sites: list[int] = []
    try:
        with open(elf_path, "rb") as f:
            elf = ELFFile(f)
            if not elf.has_dwarf_info():
                return ()
            for cu in elf.get_dwarf_info().iter_CUs():
                for die in cu.iter_DIEs():
                    if die.tag != "DW_TAG_inlined_subroutine" or "DW_AT_abstract_origin" not in die.attributes:
                        continue
                    origin = die.get_DIE_from_attribute("DW_AT_abstract_origin")
                    while "DW_AT_name" not in origin.attributes and "DW_AT_abstract_origin" in origin.attributes:
                        origin = origin.get_DIE_from_attribute("DW_AT_abstract_origin")
                    if _name(origin) != function:
                        continue
                    entry = die.attributes.get("DW_AT_entry_pc")
                    low = die.attributes.get("DW_AT_low_pc")
                    if entry is not None and entry.form == "DW_FORM_addr":
                        sites.append(entry.value)
                    elif low is not None:
                        sites.append(low.value)
    except (OSError, ELFError, KeyError, ValueError):
        return ()
    return tuple(sorted(set(sites)))


def _int(data: bytes, signed: bool) -> int:
    return int.from_bytes(data, "little", signed=signed)


def _text(data: bytes) -> str:
    end = data.find(b"\0")
    raw = data if end < 0 else data[:end]
    return raw.decode("latin-1").encode("unicode_escape").decode("ascii")


def _is_plain_char(die: Any) -> bool:
    """An array of `char` is text; one of `uint8_t` or `int8_t`, which are typedefs
    of the same base types, is numbers. So only qualifiers are looked through."""
    while die is not None and die.tag in ("DW_TAG_volatile_type", "DW_TAG_const_type"):
        die = die.get_DIE_from_attribute("DW_AT_type") if "DW_AT_type" in die.attributes else None
    return die is not None and die.tag == "DW_TAG_base_type" and _name(die) == "char" \
        and die.attributes["DW_AT_encoding"].value in _CHARS


def _bitfield(member: Any, data: bytes, base: int, unit_size: int) -> int:
    bits = member.attributes["DW_AT_bit_size"].value
    base_type = _strip(member.get_DIE_from_attribute("DW_AT_type"))
    signed = base_type is not None and base_type.attributes.get("DW_AT_encoding") is not None \
        and base_type.attributes["DW_AT_encoding"].value in (_SIGNED, _SIGNED_CHAR)
    if "DW_AT_data_bit_offset" in member.attributes:      # what GCC 14 emits for DWARF 5
        start = member.attributes["DW_AT_data_bit_offset"].value
        word = _int(data[base:base + unit_size], False)
    else:                                                 # for DWARF 3 and 4: from the unit's top bit
        loc = member.attributes.get("DW_AT_data_member_location")
        offset = loc.value if loc is not None else 0
        storage = member.attributes["DW_AT_byte_size"].value
        start = storage * 8 - member.attributes["DW_AT_bit_offset"].value - bits
        word = _int(data[base + offset:base + offset + storage], False)
    value = (word >> start) & ((1 << bits) - 1)
    if signed and value >> (bits - 1):
        value -= 1 << bits
    return value


def decode(type_die: Any, data: bytes, offset: int = 0) -> Any:
    """The value of a variable of `type_die` held in `data` at `offset`."""
    t = _strip(type_die)
    if t is None:
        return None
    if t.tag == "DW_TAG_base_type":
        n, enc = t.attributes["DW_AT_byte_size"].value, t.attributes["DW_AT_encoding"].value
        raw = data[offset:offset + n]
        if enc == _FLOAT and n in (4, 8):
            return struct.unpack("<f" if n == 4 else "<d", raw)[0]
        if enc == _BOOLEAN:
            return bool(_int(raw, False))
        return _int(raw, enc in (_SIGNED, _SIGNED_CHAR))
    if t.tag == "DW_TAG_enumeration_type":
        n = t.attributes["DW_AT_byte_size"].value
        value = _int(data[offset:offset + n], False)
        names = {c.attributes["DW_AT_const_value"].value: _name(c) for c in t.iter_children()
                 if c.tag == "DW_TAG_enumerator"}
        return f"{names[value]} ({value})" if value in names else f"{value} (no enumerator has this value)"
    if t.tag in ("DW_TAG_pointer_type", "DW_TAG_reference_type"):
        n = t.attributes.get("DW_AT_byte_size")
        return f"0x{_int(data[offset:offset + (n.value if n else 4)], False):08X}"
    if t.tag == "DW_TAG_array_type":
        element = t.get_DIE_from_attribute("DW_AT_type")
        counts, size = _array_counts(t), _size(element)
        if len(counts) == 1 and _is_plain_char(element):
            return _text(data[offset:offset + counts[0]])

        def nest(dims: list[int], at: int) -> list[Any]:
            if len(dims) == 1:
                return [decode(element, data, at + i * size) for i in range(dims[0])]
            stride = size
            for d in dims[1:]:
                stride *= d
            return [nest(dims[1:], at + i * stride) for i in range(dims[0])]
        return nest(counts, offset)
    if t.tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_class_type"):
        out: dict[str, Any] = {}
        unit_size = _size(t)
        for member in t.iter_children():
            if member.tag != "DW_TAG_member":
                continue
            key = _name(member) or f"(anonymous {len(out)})"
            if "DW_AT_bit_size" in member.attributes:
                out[key] = _bitfield(member, data, offset, unit_size)
                continue
            loc = member.attributes.get("DW_AT_data_member_location")
            moff = loc.value if loc is not None and isinstance(loc.value, int) else 0
            out[key] = decode(member.get_DIE_from_attribute("DW_AT_type"), data, offset + moff)
        return out
    if t.tag == "DW_TAG_subroutine_type":
        return None
    return f"({t.tag[7:]} not decoded) {data[offset:offset + _size(t)].hex()}"
