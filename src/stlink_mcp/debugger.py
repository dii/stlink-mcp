"""Stopping the target where an agent asks, and reading where it is.

The GDB side of `run_until` and of `core_registers`' call stack, over a GDB/MI
session attached to the probe's GDB server. Each fact used here was measured on
an STM32H7, an STM32F407 and an STM32N6 on 2026-10-02, before it was built
(docs/v1-plan.md has the measurements):

- ST-LINK_gdbserver reads the Debug Fault Status Register at every stop and
  clears it, so GDB reads 0 or 1 there afterwards. The server's log at level
  16 records the value it read: 0x3 at a breakpoint, 0x5 at a watchpoint. That
  log line is the evidence a stop is verified on.
- GDB reports a breakpoint stop as breakpoint-hit, but a watchpoint stop as a
  bare SIGTRAP, because the server's stop reply names no reason.
- A watchpoint stops after the writing instruction, never on it: one more
  instruction on the F407 and the N6, two to four on the H7. Where a function
  wrote and returned, LR still held the address after the call to it.
- The STM32F4's breakpoint unit is revision 1, which covers only addresses
  below 0x20000000. GDB and the server accepted a hardware breakpoint in RAM
  there and it never fired, so the tool checks the unit itself.
- Disconnecting GDB leaves a halted core halted and the server serving;
  detaching resumes it; stopping the server resumes it.

And on 2026-10-03, on the H7 and the F407: the server accepts a watchpoint of
any length and, for any but 1, 2 or 4 bytes, watches only the first byte,
saying so only in its log, and one at an address not aligned to its size never
fired; and its memory map marks everything but flash read-write, so GDB asks
for a software breakpoint at any other address.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .gdbmi import MiError, Session
from .read_effects import in_device_region

FP_CTRL, DWT_CTRL = 0xE0002000, 0xE0001000
DFSR_HALTED, DFSR_BKPT, DFSR_DWTTRAP, DFSR_VCATCH, DFSR_EXTERNAL = 0x1, 0x2, 0x4, 0x8, 0x10

#: How deep a call stack is reported. Deeper frames are named in the result as
#: cut, never silently dropped.
MAX_FRAMES = 8


class DebugError(RuntimeError):
    pass


def dfsr_names(value: int) -> list[str]:
    names = [n for bit, n in ((DFSR_HALTED, "HALTED"), (DFSR_BKPT, "BKPT"), (DFSR_DWTTRAP, "DWTTRAP"),
                              (DFSR_VCATCH, "VCATCH"), (DFSR_EXTERNAL, "EXTERNAL")) if value & bit]
    return names or ["none"]


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_word(session: Session, address: int) -> int | None:
    try:
        record = session.command(f"-data-read-memory-bytes 0x{address:08X} 4")
    except MiError:
        return None
    memory = record.results.get("memory") or []
    if not memory or "contents" not in memory[0]:
        return None
    return int.from_bytes(bytes.fromhex(memory[0]["contents"]), "little")


def read_bytes(session: Session, address: int, length: int) -> bytes | None:
    try:
        record = session.command(f"-data-read-memory-bytes 0x{address:08X} {length}")
    except MiError:
        return None
    memory = record.results.get("memory") or []
    return bytes.fromhex(memory[0]["contents"]) if memory and "contents" in memory[0] else None


def evaluate(session: Session, expression: str) -> str | None:
    try:
        return session.command(f"-data-evaluate-expression {_quote(expression)}").results.get("value")
    except MiError:
        return None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.match(r"\s*(0x[0-9a-fA-F]+|\d+)", value)
    return int(match.group(1), 0) if match else None


@dataclass
class Comparators:
    breakpoint_unit_revision: int
    breakpoints: int
    watchpoints: int

    def covers(self, address: int) -> bool:
        """Revision 1 compares only the code region, below 0x20000000; revision 2
        any address (ARMv7-M and ARMv8-M architecture manuals; on the F407 a
        hardware breakpoint at 0x20000042 never fired)."""
        return self.breakpoint_unit_revision >= 1 or address < 0x20000000


def comparators(session: Session) -> Comparators | None:
    fp, dwt = read_word(session, FP_CTRL), read_word(session, DWT_CTRL)
    if fp is None:
        return None
    return Comparators(fp >> 28, ((fp >> 8) & 0x70) | ((fp >> 4) & 0xF), (dwt or 0) >> 28)


_REGION = re.compile(r"^\s*\d+\s+y\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(.*)$", re.M)


def writable(session: Session, address: int) -> bool | None:
    """Whether GDB's memory map, from the server, marks `address` read-write.
    GDB asks for a software breakpoint there and a hardware one elsewhere, and
    reports both as type "breakpoint", so the type cannot say which. None if
    unknown. ST-LINK_gdbserver marks everything but flash read-write, ROM and
    addresses with nothing behind them included (H7 and F407, 2026-10-03), so
    this says which kind GDB will ask for, not that there is RAM."""
    try:
        text = session.command('-interpreter-exec console "info mem"').text
    except MiError:
        return None
    for low, high, attrs in _REGION.findall(text):
        if int(low, 16) <= address < int(high, 16):
            return attrs.split()[:1] == ["rw"]
    return None


def frames(session: Session, depth: int = MAX_FRAMES) -> tuple[list[dict[str, Any]], bool]:
    """The call stack, innermost first, with each frame's arguments and locals,
    and whether it was cut at `depth`. It ends at the first frame GDB cannot
    place in the ELF: below the reset handler that is a made-up "<signal handler
    called>" frame from LR's reset value on the H7, or the boot ROM on the N6."""
    try:
        stack = session.command(f"-stack-list-frames 0 {depth}").results.get("stack") or []
    except MiError:
        return [], False
    out: list[dict[str, Any]] = []
    for item in stack[:depth]:
        frame = item.get("frame", item)
        address = _int(frame.get("addr"))
        if address in (None, 0xFFFFFFFF) or "file" not in frame:
            break
        level = frame.get("level", "0")
        entry: dict[str, Any] = {
            "function": frame.get("func"),
            "file": frame.get("file"),
            "line": _int(frame.get("line")),
            "address": f"0x{address:08X}",
        }
        try:
            variables = session.command(
                f"-stack-list-variables --thread 1 --frame {level} --all-values"
            ).results.get("variables") or []
        except MiError:
            variables = []
        arguments = {v["name"]: v.get("value") for v in variables if v.get("arg")}
        local = {v["name"]: v.get("value") for v in variables if not v.get("arg")}
        if arguments:
            entry["arguments"] = arguments
        if local:
            entry["locals"] = local
        out.append(entry)
    return out, len(stack) > depth


_CORE = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12",
         "sp", "lr", "pc", "xpsr"]


def registers(session: Session) -> dict[str, int]:
    names = session.command("-data-list-register-names").results.get("register-names") or []
    wanted = {i: n for i, n in enumerate(names) if n in _CORE}
    values = session.command(
        "-data-list-register-values x " + " ".join(str(i) for i in wanted)
    ).results.get("register-values") or []
    return {wanted[int(v["number"])].upper(): int(v["value"], 16) for v in values if int(v["number"]) in wanted}


def call_before(session: Session, lr: int) -> str | None:
    """The function called by the instruction before LR, when it is a BL. After
    a watchpoint stop it is only a candidate for the writer: on the H7 and the
    F407 it was the writer when the write was the last thing a function did,
    and named a function that wrote nothing when the caller wrote, whether the
    stop was before LR or inside a call just made (2026-10-03)."""
    try:
        insns = session.command(f"-data-disassemble -s 0x{(lr & ~1) - 4:08X} -e 0x{lr & ~1:08X} -- 0")
    except MiError:
        return None
    for insn in insns.results.get("asm_insns") or []:
        text = insn.get("inst", "") if isinstance(insn, dict) else ""
        match = re.match(r"bl\s+0x[0-9a-f]+\s+<([^>+]+)", text)
        if match:
            return match.group(1)
    return None


@dataclass
class Stop:
    reached: bool
    """Stopped by itself within the time, rather than interrupted."""
    seconds: float
    pc: int | None
    gdb_reason: str | None
    dfsr: list[int]
    """What the server logged at this stop: one value per stop, normally one."""
    frames: list[dict[str, Any]] = field(default_factory=list)
    frames_cut: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def run_until(session: Session, server: Any, *, location: str | None, watch: str | None,
              seconds: float, read_effects: Any = None) -> Stop:
    """Insert one breakpoint or watchpoint, continue, and wait at most `seconds`.

    Leaves the core halted, at the stop or where it was interrupted, with the
    breakpoint or watchpoint removed. Refuses, before continuing, anything the
    part cannot do: a hardware breakpoint its unit does not cover, a watch the
    server would narrow to one byte or that is not aligned to its size, a watch
    on a register `read_effects(address, size)` says changes state when read,
    since GDB reads a watched address when it sets the watchpoint, or a
    watchpoint GDB could only do in software, by single-stepping."""
    units = comparators(session)
    detail: dict[str, Any] = {}
    target_address: int | None = None
    try:
        if location is not None:
            spec = f"*{location}" if re.fullmatch(r"0x[0-9a-fA-F]+", location) else location
            try:
                bkpt = session.command(f"-break-insert {_quote(spec)}").results.get("bkpt", {})
            except MiError as exc:
                raise DebugError(f"GDB could not place {location}: {exc}") from exc
            if bkpt.get("addr") in (None, "<MULTIPLE>", "<PENDING>"):
                raise DebugError(
                    f"{location} is not one address in this ELF ({bkpt.get('addr')}); "
                    "give a file:line or an address."
                )
            target_address = int(bkpt["addr"], 16)
            hardware = writable(session, target_address) is not True
            detail["location"] = {"address": f"0x{target_address:08X}", "function": bkpt.get("func"),
                                  "file": bkpt.get("file"), "line": _int(bkpt.get("line"))}
            detail["breakpoint"] = "hardware" if hardware else "software"
            if hardware and units is not None and units.breakpoints == 0:
                raise DebugError(
                    f"{location} at 0x{target_address:08X} needs a hardware breakpoint, and this part "
                    "reports no breakpoint comparators."
                )
            if hardware and units is not None and not units.covers(target_address):
                raise DebugError(
                    f"{location} at 0x{target_address:08X} needs a hardware breakpoint, and this part's "
                    f"breakpoint unit (revision {units.breakpoint_unit_revision + 1}) covers only "
                    "addresses below 0x20000000. On an STM32F407 GDB and the server accepted one "
                    "there without a word and it never fired."
                )
        else:
            expression = f"*(unsigned int *){watch}" if re.fullmatch(r"0x[0-9a-fA-F]+", watch or "") else watch
            size = _int(evaluate(session, f"sizeof({expression})"))
            target_address = _int(evaluate(session, f"&({expression})"))
            at = f" at 0x{target_address:08X}" if target_address is not None else ""
            if size is not None and size not in (1, 2, 4):
                raise DebugError(
                    f"{watch} is {size} bytes{at}; a watchpoint covers 1, 2 or 4 bytes. ST's GDB server "
                    "accepts any other length and watches only the first byte, so a write to the rest "
                    "would not stop the core. Watch one member or element of it."
                )
            if size is not None and (target_address or 0) % size:
                raise DebugError(
                    f"{watch} is {size} bytes{at}, which is not aligned to its size. ST's GDB server "
                    "accepted such a watch and it never stopped the core, though one of its bytes was "
                    "written. Watch an aligned 1, 2 or 4 bytes that hold it."
                )
            peripheral = target_address is None or in_device_region(target_address, size or 4)
            known = read_effects(target_address, size or 4) if read_effects and target_address is not None else []
            if peripheral and known:
                first = known[0]
                raise DebugError(
                    f"{watch} is {first['register']}, and reading it {first['effect']} ({first['provenance']}: "
                    f"{first['established']}). GDB reads a watched address when it sets the watchpoint, "
                    "three times on an STM32H743 (2026-10-03), so it is not watched."
                )
            try:
                session.command(f"-break-watch {_quote(expression)}")
            except MiError as exc:
                raise DebugError(f"GDB could not watch {watch}: {exc}") from exc
            table = session.command("-break-list").results.get("BreakpointTable", {}).get("body", [])
            kinds = [row.get("bkpt", row).get("type") for row in table]
            if "hw watchpoint" not in kinds:
                raise DebugError(
                    f"{watch} would be a software watchpoint, which GDB does by single-stepping "
                    "the program; refused. A hardware watchpoint covers an aligned 1, 2 or 4 bytes."
                )
            # A peripheral register may change when read, so this tool reads its
            # value no further than GDB itself did in setting the watchpoint.
            detail["watched"] = {"expression": watch,
                                 "address": f"0x{target_address:08X}" if target_address is not None else None}
            if peripheral:
                detail["watched"]["value_note"] = (
                    "Not read before or after the stop: the address is a peripheral's, or not known. "
                    "GDB read it when it set the watchpoint, as it does any watched address; no "
                    "effect of reading it is known, which does not mean it has none."
                )
            else:
                detail["watched"]["value_before"] = evaluate(session, expression)
            if units is not None and units.watchpoints == 0:
                raise DebugError("This part reports no watchpoint comparators.")

        offset = server.log_size()
        started = time.monotonic()
        try:
            session.resume()
            stop = session.wait_stopped(seconds)
        except MiError as exc:
            raise DebugError(f"GDB did not run the program: {exc}") from exc
        reached = stop is not None
        if not reached:
            session.command("-exec-interrupt")
            stop = session.wait_stopped(10)
        elapsed = time.monotonic() - started
        if stop is None:
            raise DebugError(f"The target did not stop when interrupted after {seconds:g} s.")
        frame = stop.results.get("frame", {})
        pc = _int(frame.get("addr"))
        listed, cut = frames(session)
        result = Stop(reached, elapsed, pc, stop.results.get("reason") or stop.results.get("signal-name"),
                      server.halt_reasons_since(offset), listed, cut, detail)
        if watch is not None:
            if "value_before" in detail["watched"]:
                detail["watched"]["value_now"] = evaluate(session, expression)
            lr = registers(session).get("LR")
            if lr is not None:
                detail["lr"] = f"0x{lr:08X}"
                callee = call_before(session, lr)
                # Still inside that call, it has not returned, so it is no candidate.
                if callee and not (listed and listed[0].get("function") == callee):
                    detail["returned_from"] = callee
        if location is not None:
            detail["at_location"] = pc == target_address
        return result
    finally:
        try:
            session.command("-break-delete")
        except MiError:
            pass
