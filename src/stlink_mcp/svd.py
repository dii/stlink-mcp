"""Peripheral registers by name, from ST's SVD files in the user's STM32CubeCLT
install.

The files are read at run time from the install, the same rule as the device
database: ST's SVD files are Apache-2.0, but nothing parsed from them is kept
in this repository. STM32CubeCLT 1.22.0 has 235 of them.

Which file describes the part is not something the target says. The device
database names no SVD file, and one device ID covers several parts: 0x413 is
"STM32F405xx/F407xx/F415xx/F417xx", four files (measured 2026-10-02). So the
file comes from the caller, or from the board name the probe reports, or from
the part names in the device name; and where that leaves several files, a
register is used only when every one of them describes it the same way.

What the files say about reads. The STM32F407 and STM32H743 files mark no
register read as having a side effect, and the STM32N657 file marks 54 fields;
on an STM32H7, a programmer read of USART3's RDR returned the received byte and
cleared RXNE, which the H743 file does not mark (measured 2026-10-02). A read
the file marks is refused; any other read says the file may not mark it.
"""

from __future__ import annotations

import difflib
import functools
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SvdError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    width: int
    meanings: tuple[tuple[int, str], ...] = ()
    read_action: str | None = None


@dataclass(frozen=True)
class Register:
    peripheral: str
    name: str
    address: int
    size_bits: int
    description: str
    fields: tuple[Field, ...]
    read_action: str | None = None

    def layout(self) -> tuple[Any, ...]:
        """What must match for two files to describe the same register."""
        return (self.address, self.size_bits, tuple((f.name, f.offset, f.width) for f in self.fields))


def svd_dir(toolchain_root: Path) -> Path:
    return Path(toolchain_root) / "STMicroelectronics_CMSIS_SVD"


def _int(text: str | None, default: int | None = None) -> int | None:
    if text is None:
        return default
    text = text.strip().lower()
    if text.startswith("#"):
        return None if "x" in text else int(text[1:], 2)
    return int(text, 0)


def _text(element: ET.Element, tag: str) -> str | None:
    value = element.findtext(tag)
    return " ".join(value.split()) if value is not None else None


def _field(element: ET.Element) -> Field | None:
    name = _text(element, "name")
    offset, width = _int(element.findtext("bitOffset")), _int(element.findtext("bitWidth"))
    if offset is None:
        bit_range = element.findtext("bitRange")
        if bit_range:
            msb, lsb = (int(x) for x in bit_range.strip("[] ").split(":"))
            offset, width = lsb, msb - lsb + 1
        elif element.findtext("lsb") is not None:
            offset = _int(element.findtext("lsb"))
            width = _int(element.findtext("msb")) - offset + 1
    if name is None or offset is None:
        return None
    meanings: list[tuple[int, str]] = []
    for values in element.findall("enumeratedValues"):
        if (values.findtext("usage") or "read").strip() == "write":
            continue
        for value in values.findall("enumeratedValue"):
            # ST names values B_0x0, B_0x1, which only repeat the number; the
            # description says what it means ("hsi_ck selected as system clock").
            number = _int(value.findtext("value"))
            meaning = _text(value, "description") or _text(value, "name")
            if number is not None and meaning:
                meanings.append((number, meaning))
        break
    return Field(name, offset, width if width is not None else 1, tuple(meanings), _text(element, "readAction"))


class _Device:
    """One parsed SVD file: registers by peripheral and register name, upper case."""

    def __init__(self, path: Path) -> None:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise SvdError(f"{path} could not be read as an SVD file: {exc}") from exc
        default_size = _int(root.findtext("size"), 32)
        elements = {p.findtext("name"): p for p in root.iter("peripheral")}
        self.peripherals: dict[str, dict[str, Register]] = {}
        self.unread: set[str] = set()
        for name, element in elements.items():
            if not name:
                continue
            source = element
            if element.find("registers") is None and element.get("derivedFrom") in elements:
                source = elements[element.get("derivedFrom")]
            base = _int(element.findtext("baseAddress"), 0)
            size = _int(source.findtext("size"), default_size)
            registers: dict[str, Register] = {}
            block = source.find("registers")
            if block is not None:
                if block.find("cluster") is not None:
                    self.unread.add(name.upper())
                for reg in block.findall("register"):
                    reg_name = reg.findtext("name")
                    if not reg_name or reg.find("dim") is not None or "%s" in reg_name:
                        self.unread.add(name.upper())
                        continue
                    fields = tuple(f for f in (_field(e) for e in reg.iter("field")) if f is not None)
                    registers[reg_name.upper()] = Register(
                        name, reg_name, base + _int(reg.findtext("addressOffset"), 0),
                        _int(reg.findtext("size"), size), _text(reg, "description") or "",
                        fields, _text(reg, "readAction"),
                    )
            self.peripherals[name.upper()] = registers


@functools.lru_cache(maxsize=8)
def _device(path: str, mtime_ns: int) -> _Device:
    return _Device(Path(path))


def _load(path: Path) -> _Device:
    try:
        return _device(str(path), os.stat(path).st_mtime_ns)
    except OSError as exc:
        raise SvdError(f"Cannot read {path}: {exc.strerror}.") from exc


def find(path: Path, query: str) -> Register:
    """The register `query`, "PERIPHERAL.REGISTER", in one SVD file.

    STM32N6 files prefix register names with their peripheral (RCC_CR), the F4
    and H7 files do not (CR); "RCC.CR" finds either."""
    peripheral, sep, register = query.partition(".")
    if not sep or not peripheral or not register or "." in register:
        raise SvdError(f"{query!r} is not PERIPHERAL.REGISTER, such as RCC.CR.")
    device = _load(path)
    registers = device.peripherals.get(peripheral.upper())
    if registers is None:
        near = difflib.get_close_matches(peripheral.upper(), list(device.peripherals), n=5)
        raise SvdError(
            f"{path.name} has no peripheral {peripheral}."
            + (f" Close names: {', '.join(near)}." if near else "")
            + " Core registers such as SCB or NVIC are in ST's core files, named with svd, "
            "for example svd=\"Core/Cortex-M7\"."
        )
    found = registers.get(register.upper()) or registers.get(f"{peripheral}_{register}".upper())
    if found is None:
        near = difflib.get_close_matches(register.upper(), list(registers), n=5)
        unread = (
            f" {path.name} also describes some of {peripheral}'s registers as arrays or "
            "clusters, which this tool does not read; read those by address."
            if peripheral.upper() in device.unread else ""
        )
        raise SvdError(
            f"{path.name} has no register {register} in {peripheral}."
            + (f" Close names: {', '.join(near)}." if near else "") + unread
        )
    return found


def _part(stem: str) -> str | None:
    """"STM32H745_CM7" -> "H745"."""
    return stem[5:].split("_")[0].upper() if stem.upper().startswith("STM32") else None


def candidates(directory: Path, *, device_name: str | None, board: str | None,
               cpu: str | None) -> tuple[list[Path], str]:
    """The files that could describe the target, and how they were chosen."""
    files = sorted(directory.glob("*.svd"))
    if board:
        upper = board.upper()
        # NUCLEO-H743ZI and STM32H753I-EVAL2 both name their part: after a
        # separator, or straight after STM32.
        matched = [f for f in files if (part := _part(f.stem))
                   and re.search(rf"(?:(?<=STM32)|(?<![A-Z0-9])){re.escape(part)}", upper)]
        if matched and cpu and any("_CM" in f.stem.upper() for f in matched):
            core = "_CM" + re.sub(r"\D", "", cpu)[:2]
            matched = [f for f in matched if "_CM" not in f.stem.upper() or f.stem.upper().endswith(core)]
        if matched:
            return matched, f"the board name the probe reports, {board}"
    if device_name:
        names = device_name.split("/")
        stems: list[str] = []
        for token in names:
            token = token.strip().upper()
            token = token if token.startswith("STM32") else "STM32" + token
            stems.append(token.rstrip("X"))
        matched = [f for f in files if any(f.stem.upper().startswith(s) for s in stems)]
        if matched:
            return matched, f"the device name the probe reports, {device_name}"
    return [], ""


def resolve(directory: Path, query: str, *, svd: str | None, device_name: str | None,
            board: str | None, cpu: str | None) -> tuple[Register, str, str]:
    """(register, the file it came from, how that file was chosen)."""
    if svd:
        path = Path(svd)
        if not path.is_file():
            path = directory / (svd if svd.lower().endswith(".svd") else f"{svd}.svd")
        if not path.is_file():
            near = difflib.get_close_matches(Path(svd).stem.upper(), [f.stem.upper() for f in directory.glob("*.svd")], n=5)
            raise SvdError(f"No SVD file {svd} in {directory}." + (f" Close names: {', '.join(near)}." if near else ""))
        return find(path, query), path.name, "named by the caller"

    files, how = candidates(directory, device_name=device_name, board=board, cpu=cpu)
    if not files:
        raise SvdError(
            f"No SVD file could be chosen for this part from what the probe reports "
            f"(device {device_name or 'unknown'}, board {board or 'none'}). Name one with "
            f"svd, from {directory}."
        )
    found: dict[Path, Register] = {}
    errors: list[str] = []
    for path in files:
        try:
            found[path] = find(path, query)
        except SvdError as exc:
            errors.append(str(exc))
    names = ", ".join(f.stem for f in files)
    if not found:
        raise SvdError(errors[0] if len(files) == 1 else f"None of {names} has {query}. {errors[0]}")
    if len(found) < len(files) or len({r.layout() for r in found.values()}) > 1:
        raise SvdError(
            f"{query} is not described the same way in every SVD file that could be this "
            f"part ({names}, chosen from {how}). Name the part's file with svd."
        )
    path, register = next(iter(found.items()))
    if len(files) == 1:
        return register, path.name, f"chosen from {how}"
    return register, path.name, f"all {len(files)} files that could be this part agree on it ({names}), chosen from {how}"


def refuse_marked_read(register: Register, file_name: str) -> None:
    marked = [f"{f.name} ({f.read_action})" for f in register.fields if f.read_action]
    if register.read_action:
        marked.insert(0, f"the whole register ({register.read_action})")
    if marked:
        raise SvdError(
            f"{file_name} marks reading {register.peripheral}.{register.name} as changing "
            f"state: {', '.join(marked)}. It is not read by name; read the address if you mean to."
        )


def decode(register: Register, value: int) -> dict[str, Any]:
    """Each field's value, with the file's name for it where it gives one."""
    out: dict[str, Any] = {}
    for f in sorted(register.fields, key=lambda f: -f.offset):
        v = (value >> f.offset) & ((1 << f.width) - 1)
        meaning = dict(f.meanings).get(v)
        out[f.name] = {"value": v, "meaning": meaning} if meaning else v
    return out
